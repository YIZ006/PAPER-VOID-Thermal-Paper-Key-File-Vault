"""
PAPER-VOID Hybrid Paper-Key Vault (Streaming AES-256-GCM + 1-Tile Thermal Key Card).

Encrypts files of arbitrary size (MB to multi-GB) gradually in 1 MB chunks using
AES-256-GCM (low RAM footprint, atomic .part output, safe cancellation), while
rendering a single ~10 cm thermal Key Card (384 px wide, 203 DPI) containing:
- 1 PV-RIBBON v1 optical tile (6x6 px, RS(158, 86)x2 ECC) holding the 97-byte key packet
- Human-readable Base32 + CRC-16 recovery code (anti-thermal-paper-fading insurance)
- Vault ID, filename hint, SHA-256 prefix, and 2FA indicator
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
import struct
import threading
import time
from typing import Callable, Final, Optional
import zlib

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import numpy as np
from PIL import Image, ImageDraw, ImageFont

try:
    from . import codec
except ImportError:
    import codec

# ---------------------------------------------------------------------------
# Binary Format Constants
# ---------------------------------------------------------------------------

VAULT_MAGIC: Final[bytes] = b"PVV1"
KEY_PACKET_MAGIC: Final[bytes] = b"PVK1"

FLAG_PASSPHRASE_2FA: Final[int] = 0x01

DEFAULT_CHUNK_SIZE: Final[int] = 1024 * 1024  # 1 MB streaming chunks
MIN_CHUNK_SIZE: Final[int] = 64 * 1024
MAX_CHUNK_SIZE: Final[int] = 16 * 1024 * 1024

# Header: magic(4) flags(1) reserved(3) vault_id(8) base_nonce(12) salt(16) chunk_size(4) orig_size(8) meta_len(4)
VAULT_HEADER_STRUCT: Final[struct.Struct] = struct.Struct(">4sB3s8s12s16sIQI")
CHUNK_LEN_STRUCT: Final[struct.Struct] = struct.Struct(">I")

# Key packet inside 1 PV-RIBBON v1 tile (97 bytes <= 156 bytes max tile payload)
# magic(4) flags(1) vault_id(8) paper_key(32) salt(16) orig_sha256(32) crc32(4) = 97 bytes
KEY_PACKET_BODY_STRUCT: Final[struct.Struct] = struct.Struct(">4sB8s32s16s32s")
KEY_PACKET_CRC_STRUCT: Final[struct.Struct] = struct.Struct(">I")
KEY_PACKET_SIZE: Final[int] = KEY_PACKET_BODY_STRUCT.size + KEY_PACKET_CRC_STRUCT.size  # 97

assert KEY_PACKET_SIZE == 97 <= codec.TILE_PAYLOAD_BYTES


class VaultError(RuntimeError):
    """Base error for Paper-Key Vault operations."""


class VaultCancelledError(VaultError):
    """Raised when streaming encryption or decryption is cancelled cleanly."""


class VaultAuthError(VaultError):
    """Raised on wrong key, wrong 2FA passphrase, or tampered .pvvault file."""


@dataclass(frozen=True)
class PaperKeyRecord:
    """97-byte optical key payload stored inside 1 PV-RIBBON v1 tile."""

    flags: int
    vault_id: bytes  # 8 bytes
    paper_key: bytes  # 32 bytes
    salt: bytes  # 16 bytes
    orig_sha256: bytes  # 32 bytes

    @property
    def has_passphrase(self) -> bool:
        return bool(self.flags & FLAG_PASSPHRASE_2FA)

    @property
    def vault_id_hex(self) -> str:
        return self.vault_id.hex().upper()

    @property
    def orig_sha256_hex(self) -> str:
        return self.orig_sha256.hex()

    @property
    def recovery_code(self) -> str:
        return format_recovery_code(self.paper_key)


@dataclass(frozen=True)
class VaultLockResult:
    """Result returned after streaming encryption + Key Card generation in RAM."""

    vault_path: str
    filename: str
    orig_size: int
    vault_size: int
    chunks_written: int
    elapsed_s: float
    key_record: PaperKeyRecord
    card_image: np.ndarray  # (H, 384) uint8 in RAM
    card_length_cm: float


@dataclass(frozen=True)
class VaultUnlockResult:
    """Result returned after streaming decryption + SHA-256 verification."""

    output_path: str
    original_filename: str
    orig_size: int
    sha256_hex: str
    vault_id_hex: str
    elapsed_s: float


@dataclass(frozen=True)
class VaultHeaderInfo:
    """Unencrypted public header info inspected from a .pvvault file."""

    vault_id: bytes
    flags: int
    chunk_size: int
    orig_size: int
    salt: bytes
    base_nonce: bytes
    meta_len: int

    @property
    def has_passphrase(self) -> bool:
        return bool(self.flags & FLAG_PASSPHRASE_2FA)

    @property
    def vault_id_hex(self) -> str:
        return self.vault_id.hex().upper()


# ---------------------------------------------------------------------------
# Key Derivation, Nonce Math, & Base32 Recovery Code
# ---------------------------------------------------------------------------


def _is_cancelled(cancel_event: Optional[threading.Event]) -> bool:
    return bool(cancel_event is not None and cancel_event.is_set())


def _derive_data_key(
    paper_key: bytes,
    salt: bytes,
    vault_id: bytes,
    flags: int,
    passphrase: str = "",
) -> bytes:
    if len(paper_key) != 32:
        raise ValueError("paper_key must be 32 bytes")
    if flags & FLAG_PASSPHRASE_2FA:
        if not passphrase:
            raise VaultAuthError("Vault yêu cầu mật khẩu lớp 2 (2FA Passphrase).")
        return hashlib.scrypt(
            paper_key + b"|" + passphrase.encode("utf-8"),
            salt=b"PVV1-2FA|" + salt + vault_id,
            n=16384,
            r=8,
            p=1,
            dklen=32,
        )
    return hashlib.sha256(b"PVV1-KEY|" + salt + vault_id + paper_key).digest()


def _derive_nonce(base_nonce: bytes, counter: int) -> bytes:
    """Derive a unique 96-bit GCM nonce for metadata (counter=0) or chunk (counter>=1)."""
    tail_int = int.from_bytes(base_nonce[4:12], "big")
    mixed = (tail_int ^ (counter & 0xFFFFFFFFFFFFFFFF)) & 0xFFFFFFFFFFFFFFFF
    return base_nonce[:4] + mixed.to_bytes(8, "big")


def _crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def format_recovery_code(paper_key: bytes) -> str:
    """Encode 32-byte AES paper key + 2-byte CRC-16 as 55 Base32 chars (11 groups of 5)."""
    if len(paper_key) != 32:
        raise ValueError(f"Expected 32-byte paper_key, got {len(paper_key)}")
    chk = struct.pack(">H", _crc16_ccitt(paper_key))
    raw = base64.b32encode(paper_key + chk).decode("ascii").rstrip("=")
    return "-".join(raw[i : i + 5] for i in range(0, len(raw), 5))


def parse_recovery_code(code_text: str) -> bytes:
    """Parse and CRC-16 verify a human-typed Base32 recovery code back to 32 bytes."""
    cleaned = "".join(ch for ch in (code_text or "").upper() if ch.isalnum())
    if not cleaned:
        raise VaultAuthError("Mã cứu hộ Base32 đang trống.")
    pad_len = (-len(cleaned)) % 8
    try:
        decoded = base64.b32decode(cleaned + ("=" * pad_len), casefold=True)
    except Exception as exc:
        raise VaultAuthError(f"Mã Base32 không hợp lệ (kiểm tra lại ký tự gõ nhầm): {exc}") from exc
    if len(decoded) != 34:
        raise VaultAuthError(
            f"Độ dài mã Base32 sai (nhận {len(cleaned)} ký tự, cần 55 ký tự / 11 cụm 5 ký tự)."
        )
    key_bytes = decoded[:32]
    (stored_crc,) = struct.unpack(">H", decoded[32:34])
    if _crc16_ccitt(key_bytes) != stored_crc:
        raise VaultAuthError("Mã cứu hộ Base32 sai checksum CRC-16 — vui lòng kiểm tra lại từng cụm.")
    return key_bytes


# ---------------------------------------------------------------------------
# 97-byte Optical Key Packet Serialization
# ---------------------------------------------------------------------------


def pack_key_record(record: PaperKeyRecord) -> bytes:
    body = KEY_PACKET_BODY_STRUCT.pack(
        KEY_PACKET_MAGIC,
        record.flags & 0xFF,
        record.vault_id,
        record.paper_key,
        record.salt,
        record.orig_sha256,
    )
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return body + KEY_PACKET_CRC_STRUCT.pack(crc)


def unpack_key_record(payload: bytes) -> Optional[PaperKeyRecord]:
    raw = bytes(payload)
    if len(raw) != KEY_PACKET_SIZE or raw[:4] != KEY_PACKET_MAGIC:
        return None
    body = raw[: KEY_PACKET_BODY_STRUCT.size]
    (crc_stored,) = KEY_PACKET_CRC_STRUCT.unpack_from(raw, KEY_PACKET_BODY_STRUCT.size)
    if (zlib.crc32(body) & 0xFFFFFFFF) != crc_stored:
        return None
    _magic, flags, vault_id, paper_key, salt, orig_sha256 = KEY_PACKET_BODY_STRUCT.unpack(body)
    return PaperKeyRecord(
        flags=flags,
        vault_id=vault_id,
        paper_key=paper_key,
        salt=salt,
        orig_sha256=orig_sha256,
    )


# ---------------------------------------------------------------------------
# Thermal Key-Card Image Renderer (384 px wide, ~9.5 cm, RAM only)
# ---------------------------------------------------------------------------


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        ["consolab.ttf", "lucon.ttf", "seguisb.ttf", "arialbd.ttf"]
        if bold
        else ["consola.ttf", "lucon.ttf", "segoeui.ttf", "arial.ttf"]
    )
    win_fonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    for name in candidates:
        for path in (name, os.path.join(win_fonts, name)):
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, size=size)
                except Exception:
                    pass
    return ImageFont.load_default()


def _format_size_short(size_bytes: int) -> str:
    val = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024.0 or unit == "GB":
            return f"{int(val)} {unit}" if unit == "B" else f"{val:.2f} {unit}"
        val /= 1024.0
    return f"{val:.2f} GB"


def render_key_card_image(
    record: PaperKeyRecord,
    filename: str,
    orig_size: int,
    cell_size: int = codec.DEFAULT_CELL_SIZE,
) -> np.ndarray:
    """
    Render a complete 384-px wide thermal Paper-Key Card strictly in RAM:
    - Header block (title, Vault ID, filename, size, 2FA status, date)
    - 1 PV-RIBBON v1 optical tile (6x6 px = 384x288 px, 45.6% RS ECC)
    - Human-readable Base32 + CRC-16 recovery code (3 bold lines)
    - Storage hint & cut line
    """
    file_id_u32 = struct.unpack(">I", record.vault_id[:4])[0]
    pvk_bytes = pack_key_record(record)
    coded_tile = codec.encode_tile_bytes(
        file_id=file_id_u32,
        tile_index=0,
        total_tiles=1,
        payload=pvk_bytes,
    )
    tile_img = codec.render_tile_image(coded_tile, cell_size=cell_size, pad_to_ribbon_width=True)
    tile_h = int(tile_img.shape[0])

    # Build top header & bottom recovery footer via Pillow in RAM (mode 'L', 255=white, 0=black)
    f_title = _load_font(22, bold=True)
    f_sub = _load_font(15, bold=True)
    f_body = _load_font(14, bold=False)
    f_code = _load_font(17, bold=True)
    f_small = _load_font(12, bold=False)

    safe_name = os.path.basename(filename)
    if len(safe_name) > 28:
        safe_name = safe_name[:18] + "..." + safe_name[-7:]

    date_str = time.strftime("%Y-%m-%d %H:%M")
    twofa_str = "2FA: YES" if record.has_passphrase else "2FA: NO"

    header_h = 132
    header_pil = Image.new("L", (codec.PRINTER_WIDTH_PX, header_h), 255)
    hd = ImageDraw.Draw(header_pil)
    hd.rectangle((4, 4, 379, 36), fill=0)
    hd.text((14, 8), "PAPER-VOID  KEY CARD", fill=255, font=f_title)
    hd.text((10, 44), f"VAULT ID: {record.vault_id_hex}", fill=0, font=f_sub)
    hd.text((10, 66), f"FILE: {safe_name}", fill=0, font=f_body)
    hd.text((10, 86), f"SIZE: {_format_size_short(orig_size)}  |  {twofa_str}", fill=0, font=f_body)
    hd.text((10, 106), f"DATE: {date_str}  |  SHA: {record.orig_sha256_hex[:12]}", fill=0, font=f_small)
    hd.line((8, header_h - 4, 375, header_h - 4), fill=0, width=2)

    # Recovery code split into 3 lines (4 + 4 + 3 groups of 5 chars)
    groups = record.recovery_code.split("-")
    line1 = " ".join(groups[0:4])
    line2 = " ".join(groups[4:8])
    line3 = " ".join(groups[8:11])

    footer_h = 166
    footer_pil = Image.new("L", (codec.PRINTER_WIDTH_PX, footer_h), 255)
    fd = ImageDraw.Draw(footer_pil)
    fd.line((8, 4, 375, 4), fill=0, width=2)
    fd.text((10, 12), "MANUAL RECOVERY CODE (BASE32 + CRC16):", fill=0, font=f_small)
    fd.rectangle((6, 30, 377, 110), outline=0, width=2)
    fd.text((14, 38), line1, fill=0, font=f_code)
    fd.text((14, 62), line2, fill=0, font=f_code)
    fd.text((14, 86), line3, fill=0, font=f_code)
    fd.text((10, 118), "Giu noi toi/mat. Chep tay ma tren neu luu > 1 nam.", fill=0, font=f_small)
    fd.text((10, 138), "- - - - - - - - -  CUT HERE  - - - - - - - - -", fill=0, font=f_small)

    # Quiet white margins around the optical tile so camera segmentation is instantaneous
    pad_top = np.full((20, codec.PRINTER_WIDTH_PX), 255, dtype=np.uint8)
    pad_mid_top = np.full((20, codec.PRINTER_WIDTH_PX), 255, dtype=np.uint8)
    pad_mid_bot = np.full((20, codec.PRINTER_WIDTH_PX), 255, dtype=np.uint8)
    pad_bot = np.full((24, codec.PRINTER_WIDTH_PX), 255, dtype=np.uint8)

    header_arr = np.where(np.asarray(header_pil, dtype=np.uint8) <= 127, np.uint8(0), np.uint8(255))
    footer_arr = np.where(np.asarray(footer_pil, dtype=np.uint8) <= 127, np.uint8(0), np.uint8(255))

    card = np.vstack((pad_top, header_arr, pad_mid_top, tile_img, pad_mid_bot, footer_arr, pad_bot))
    assert card.shape[1] == codec.PRINTER_WIDTH_PX
    assert tile_h > 0
    return np.ascontiguousarray(card, dtype=np.uint8)


# ---------------------------------------------------------------------------
# Streaming Vault Inspection, Lock (Encrypt), and Unlock (Decrypt)
# ---------------------------------------------------------------------------


def inspect_vault_header(vault_path: str) -> VaultHeaderInfo:
    """Read only the 60-byte public header of a .pvvault file."""
    with open(vault_path, "rb") as fh:
        raw_hdr = fh.read(VAULT_HEADER_STRUCT.size)
    if len(raw_hdr) != VAULT_HEADER_STRUCT.size:
        raise VaultError("File .pvvault quá ngắn hoặc bị hỏng header.")
    magic, flags, _res, vault_id, base_nonce, salt, chunk_size, orig_size, meta_len = (
        VAULT_HEADER_STRUCT.unpack(raw_hdr)
    )
    if magic != VAULT_MAGIC:
        raise VaultError("Không phải file .pvvault hợp lệ (sai magic PVV1).")
    if not (MIN_CHUNK_SIZE <= chunk_size <= MAX_CHUNK_SIZE):
        raise VaultError(f"Kích thước khối không hợp lệ trong vault: {chunk_size}")
    return VaultHeaderInfo(
        vault_id=vault_id,
        flags=flags,
        chunk_size=chunk_size,
        orig_size=orig_size,
        salt=salt,
        base_nonce=base_nonce,
        meta_len=meta_len,
    )


def lock_file_streaming(
    input_path: str,
    output_vault_path: str,
    *,
    passphrase: str = "",
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    cell_size: int = codec.DEFAULT_CELL_SIZE,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> VaultLockResult:
    """
    Encrypt `input_path` gradually in `chunk_size` (default 1 MB) AES-256-GCM chunks
    to `output_vault_path` (atomically via .part), and return the 1-tile Key Card in RAM.
    """
    abs_in = os.path.abspath(input_path)
    abs_out = os.path.abspath(output_vault_path)
    if not os.path.isfile(abs_in):
        raise FileNotFoundError(f"Không tìm thấy file nguồn: {abs_in}")
    if abs_in == abs_out:
        raise ValueError("File đích .pvvault không được trùng đường dẫn file gốc.")

    csize = max(MIN_CHUNK_SIZE, min(MAX_CHUNK_SIZE, int(chunk_size)))
    orig_size = os.path.getsize(abs_in)
    orig_filename = os.path.basename(abs_in)

    t0 = time.perf_counter()
    vault_id = os.urandom(8)
    base_nonce = os.urandom(12)
    salt = os.urandom(16)
    paper_key = os.urandom(32)
    flags = FLAG_PASSPHRASE_2FA if passphrase else 0

    data_key = _derive_data_key(paper_key, salt, vault_id, flags, passphrase=passphrase)
    aesgcm = AESGCM(data_key)

    # First pass over header metadata: encrypt filename + creation timestamp
    # (orig_sha256 is computed during streaming pass and also embedded in PaperKeyRecord
    # and verified at decrypt; metadata stores original filename + orig_size.)
    meta_dict = {
        "filename": orig_filename,
        "orig_size": orig_size,
        "created_utc": int(time.time()),
    }
    meta_plain = json.dumps(meta_dict, ensure_ascii=False).encode("utf-8")
    meta_nonce = _derive_nonce(base_nonce, 0)
    meta_aad = b"PVV1-META" + vault_id
    enc_meta = aesgcm.encrypt(meta_nonce, meta_plain, meta_aad)

    header_bytes = VAULT_HEADER_STRUCT.pack(
        VAULT_MAGIC,
        flags,
        b"\x00\x00\x00",
        vault_id,
        base_nonce,
        salt,
        csize,
        orig_size,
        len(enc_meta),
    )

    part_path = abs_out + ".part"
    sha256 = hashlib.sha256()
    bytes_done = 0
    chunks_written = 0

    if progress_callback:
        progress_callback(0, orig_size)

    try:
        with open(abs_in, "rb") as fin, open(part_path, "wb") as fout:
            fout.write(header_bytes)
            fout.write(enc_meta)

            if orig_size == 0:
                aad = b"PVV1-CHK" + vault_id + struct.pack(">QB", 0, 1)
                ct = aesgcm.encrypt(_derive_nonce(base_nonce, 1), b"", aad)
                fout.write(CHUNK_LEN_STRUCT.pack(len(ct)))
                fout.write(ct)
                chunks_written = 1
            else:
                chunk_idx = 0
                while True:
                    if _is_cancelled(cancel_event):
                        raise VaultCancelledError("Đã hủy quá trình mã hóa vault.")
                    buf = fin.read(csize)
                    if not buf:
                        break
                    sha256.update(buf)
                    bytes_done += len(buf)
                    is_final = 1 if (bytes_done >= orig_size) else 0
                    aad = b"PVV1-CHK" + vault_id + struct.pack(">QB", chunk_idx, is_final)
                    ct = aesgcm.encrypt(_derive_nonce(base_nonce, chunk_idx + 1), buf, aad)
                    fout.write(CHUNK_LEN_STRUCT.pack(len(ct)))
                    fout.write(ct)
                    chunks_written += 1
                    chunk_idx += 1
                    if progress_callback:
                        progress_callback(bytes_done, orig_size)

            fout.flush()
            os.fsync(fout.fileno())

        os.replace(part_path, abs_out)
    except BaseException:
        try:
            if os.path.exists(part_path):
                os.unlink(part_path)
        except OSError:
            pass
        raise

    record = PaperKeyRecord(
        flags=flags,
        vault_id=vault_id,
        paper_key=paper_key,
        salt=salt,
        orig_sha256=sha256.digest(),
    )
    card_img = render_key_card_image(
        record=record,
        filename=orig_filename,
        orig_size=orig_size,
        cell_size=cell_size,
    )
    card_len_cm = float(card_img.shape[0]) / codec.PX_PER_CM
    elapsed_s = max(0.001, time.perf_counter() - t0)

    return VaultLockResult(
        vault_path=abs_out,
        filename=orig_filename,
        orig_size=orig_size,
        vault_size=os.path.getsize(abs_out),
        chunks_written=chunks_written,
        elapsed_s=elapsed_s,
        key_record=record,
        card_image=card_img,
        card_length_cm=card_len_cm,
    )


def unlock_file_streaming(
    vault_path: str,
    output_dir_or_file: str,
    *,
    key_record: Optional[PaperKeyRecord] = None,
    recovery_code: Optional[str] = None,
    passphrase: str = "",
    output_is_exact_file: bool = False,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> VaultUnlockResult:
    """
    Decrypt `vault_path` gradually in streaming AES-256-GCM chunks using either:
    - `key_record` (scanned from the 1-Tile thermal Key Card photo/camera), OR
    - `recovery_code` (human-typed Base32 + CRC-16 backup string).

    Verifies every GCM chunk tag, final-chunk marker, exact size, and SHA-256 (when
    `key_record` is supplied). Writes atomically via `.part` and cleans up on error.
    """
    abs_vault = os.path.abspath(vault_path)
    if not os.path.isfile(abs_vault):
        raise FileNotFoundError(f"Không tìm thấy file vault: {abs_vault}")

    hdr = inspect_vault_header(abs_vault)
    expected_sha: Optional[bytes] = None

    if key_record is not None:
        if key_record.vault_id != hdr.vault_id:
            raise VaultAuthError(
                f"Thẻ khóa KHÔNG KHỚP file vault này!\n"
                f"Thẻ khóa: {key_record.vault_id_hex}  ≠  Vault: {hdr.vault_id_hex}"
            )
        paper_key = key_record.paper_key
        expected_sha = key_record.orig_sha256
    elif recovery_code:
        paper_key = parse_recovery_code(recovery_code)
    else:
        raise VaultAuthError("Cần cung cấp thẻ khóa quang học hoặc mã cứu hộ Base32.")

    data_key = _derive_data_key(paper_key, hdr.salt, hdr.vault_id, hdr.flags, passphrase=passphrase)
    aesgcm = AESGCM(data_key)

    t0 = time.perf_counter()
    part_path: Optional[str] = None

    try:
        with open(abs_vault, "rb") as fin:
            fin.seek(VAULT_HEADER_STRUCT.size)
            enc_meta = fin.read(hdr.meta_len)
            if len(enc_meta) != hdr.meta_len:
                raise VaultError("File .pvvault bị cắt cụt phần metadata.")

            try:
                meta_plain = aesgcm.decrypt(
                    _derive_nonce(hdr.base_nonce, 0),
                    enc_meta,
                    b"PVV1-META" + hdr.vault_id,
                )
            except InvalidTag as exc:
                raise VaultAuthError(
                    "Giải mã thất bại: sai thẻ khóa, sai mật khẩu 2FA, hoặc file .pvvault đã bị sửa đổi!"
                ) from exc

            meta = json.loads(meta_plain.decode("utf-8"))
            orig_filename = os.path.basename(str(meta.get("filename") or "recovered.bin"))
            if not orig_filename:
                orig_filename = "recovered.bin"

            if output_is_exact_file:
                final_out = os.path.abspath(output_dir_or_file)
            else:
                os.makedirs(output_dir_or_file, exist_ok=True)
                final_out = os.path.abspath(os.path.join(output_dir_or_file, orig_filename))

            part_path = final_out + ".part"
            sha256 = hashlib.sha256()
            bytes_written = 0
            chunk_idx = 0
            saw_final = False

            if progress_callback:
                progress_callback(0, hdr.orig_size)

            with open(part_path, "wb") as fout:
                while True:
                    if _is_cancelled(cancel_event):
                        raise VaultCancelledError("Đã hủy quá trình giải mã.")
                    raw_len = fin.read(CHUNK_LEN_STRUCT.size)
                    if not raw_len:
                        break
                    if len(raw_len) != CHUNK_LEN_STRUCT.size:
                        raise VaultAuthError("File .pvvault bị hỏng hoặc cắt cụt giữa chừng.")
                    (ct_len,) = CHUNK_LEN_STRUCT.unpack(raw_len)
                    ct = fin.read(ct_len)
                    if len(ct) != ct_len:
                        raise VaultAuthError("File .pvvault bị cắt cụt dữ liệu khối.")

                    # Peek if we are at EOF to validate the authenticated is_final flag
                    next_pos = fin.tell()
                    eof_peek = fin.read(1)
                    is_final = 1 if (len(eof_peek) == 0) else 0
                    if eof_peek:
                        fin.seek(next_pos)

                    aad = b"PVV1-CHK" + hdr.vault_id + struct.pack(">QB", chunk_idx, is_final)
                    try:
                        pt = aesgcm.decrypt(_derive_nonce(hdr.base_nonce, chunk_idx + 1), ct, aad)
                    except InvalidTag as exc:
                        raise VaultAuthError(
                            f"Phát hiện dữ liệu bị sửa đổi / hỏng tại khối #{chunk_idx} (GCM tag không khớp)!"
                        ) from exc

                    fout.write(pt)
                    sha256.update(pt)
                    bytes_written += len(pt)
                    chunk_idx += 1
                    if is_final:
                        saw_final = True
                    if progress_callback:
                        progress_callback(bytes_written, hdr.orig_size)

                fout.flush()
                os.fsync(fout.fileno())

        if not saw_final or bytes_written != hdr.orig_size:
            raise VaultAuthError(
                f"Kích thước sau giải mã không khớp ({bytes_written}/{hdr.orig_size} B) — file vault bị cắt cụt!"
            )

        actual_sha = sha256.digest()
        if expected_sha is not None and actual_sha != expected_sha:
            raise VaultAuthError("SHA-256 sau giải mã KHÔNG KHỚP với thẻ khóa giấy!")

        os.replace(part_path, final_out)
        part_path = None
    except BaseException:
        if part_path and os.path.exists(part_path):
            try:
                os.unlink(part_path)
            except OSError:
                pass
        raise

    elapsed_s = max(0.001, time.perf_counter() - t0)
    return VaultUnlockResult(
        output_path=final_out,
        original_filename=orig_filename,
        orig_size=hdr.orig_size,
        sha256_hex=actual_sha.hex(),
        vault_id_hex=hdr.vault_id_hex,
        elapsed_s=elapsed_s,
    )


__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "FLAG_PASSPHRASE_2FA",
    "KEY_PACKET_MAGIC",
    "KEY_PACKET_SIZE",
    "PaperKeyRecord",
    "VAULT_MAGIC",
    "VaultAuthError",
    "VaultCancelledError",
    "VaultError",
    "VaultHeaderInfo",
    "VaultLockResult",
    "VaultUnlockResult",
    "format_recovery_code",
    "inspect_vault_header",
    "lock_file_streaming",
    "pack_key_record",
    "parse_recovery_code",
    "render_key_card_image",
    "unlock_file_streaming",
    "unpack_key_record",
]
