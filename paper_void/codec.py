"""PV-RIBBON v1 codec: optical thermal-tape framing, Reed-Solomon ECC, and RAM container."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import lzma
import os
import secrets
import struct
import threading
from typing import Final, Optional
import zlib

import numpy as np
from reedsolo import RSCodec, ReedSolomonError

# ---------------------------------------------------------------------------
# Protocol & Printer Geometry Constants
# ---------------------------------------------------------------------------

PROTOCOL_NAME: Final[str] = "PV-RIBBON"
PROTOCOL_VERSION: Final[int] = 1

PRINTER_WIDTH_PX: Final[int] = 384
PRINTER_DPI: Final[int] = 203
PX_PER_CM: Final[float] = 203.0 / 2.54

GRID_COLS: Final[int] = 64
GRID_ROWS: Final[int] = 48
TOTAL_GRID_CELLS: Final[int] = GRID_ROWS * GRID_COLS  # 3072

VALID_CELL_SIZES: Final[tuple[int, int]] = (5, 6)
DEFAULT_CELL_SIZE: Final[int] = 6

RIBBON_MARGIN_Y_PX: Final[int] = 24
WHITE_PX: Final[int] = 255
BLACK_PX: Final[int] = 0

TILE_GEOMETRY: Final[dict[int, dict[str, float | int]]] = {
    6: {
        "cell_size": 6,
        "tile_width_px": 384,
        "tile_height_px": 288,
        "margin_x_px": 0,
        "inter_tile_gap_px": 18,
        "pitch_px": 306,
        "pitch_cm": 306.0 / PX_PER_CM,
    },
    5: {
        "cell_size": 5,
        "tile_width_px": 320,
        "tile_height_px": 240,
        "margin_x_px": 32,
        "inter_tile_gap_px": 15,
        "pitch_px": 255,
        "pitch_cm": 255.0 / PX_PER_CM,
    },
}

# ---------------------------------------------------------------------------
# Exact 64x48 Cell Partition & Fiducial/Beacon Geometry
# ---------------------------------------------------------------------------

FIDUCIAL_CENTERS: Final[dict[str, tuple[float, float]]] = {
    "TL": (4.5, 4.5),
    "TR": (59.5, 4.5),
    "BR": (59.5, 43.5),
    "BL": (4.5, 43.5),
}

TL_BEACON_COORDS: Final[tuple[tuple[int, int], ...]] = tuple(
    [(r, c) for r in range(1, 3) for c in range(9, 13)]
    + [(r, c) for r in range(9, 13) for c in range(1, 3)]
)
TR_BEACON_COORDS: Final[tuple[tuple[int, int], ...]] = tuple(
    [(r, c) for r in range(1, 3) for c in range(51, 55)]
    + [(r, c) for r in range(9, 13) for c in range(61, 63)]
)
BL_BEACON_COORDS: Final[tuple[tuple[int, int], ...]] = tuple(
    [(r, c) for r in range(45, 47) for c in range(9, 13)]
    + [(r, c) for r in range(35, 39) for c in range(1, 3)]
)
BR_BEACON_COORDS: Final[tuple[tuple[int, int], ...]] = tuple(
    [(r, c) for r in range(45, 47) for c in range(51, 55)]
    + [(r, c) for r in range(35, 39) for c in range(61, 63)]
)

CENTER_CHECKERBOARD_COORDS: Final[dict[tuple[int, int], int]] = {
    (23, 31): 1,
    (23, 32): 0,
    (24, 31): 0,
    (24, 32): 1,
}


def _build_reserved_cells() -> frozenset[tuple[int, int]]:
    reserved: set[tuple[int, int]] = set()
    # Outer 1-cell white border (220 cells)
    for r in range(GRID_ROWS):
        for c in range(GRID_COLS):
            if r == 0 or r == GRID_ROWS - 1 or c == 0 or c == GRID_COLS - 1:
                reserved.add((r, c))

    # 4 Corner Fiducial Zones (4 x 8x8 = 256 cells)
    for r in range(1, 9):
        for c in range(1, 9):
            reserved.add((r, c))
        for c in range(55, 63):
            reserved.add((r, c))
    for r in range(39, 47):
        for c in range(1, 9):
            reserved.add((r, c))
        for c in range(55, 63):
            reserved.add((r, c))

    # 4 Symmetric Orientation L-Beacon Zones (64 cells)
    reserved.update(TL_BEACON_COORDS)
    reserved.update(TR_BEACON_COORDS)
    reserved.update(BL_BEACON_COORDS)
    reserved.update(BR_BEACON_COORDS)

    # Center 2x2 calibration checkerboard (4 cells)
    reserved.update(CENTER_CHECKERBOARD_COORDS.keys())
    return frozenset(reserved)


RESERVED_CELLS: Final[frozenset[tuple[int, int]]] = _build_reserved_cells()


def _build_data_cell_coords() -> tuple[tuple[int, int], ...]:
    coords: list[tuple[int, int]] = []
    for pair_idx in range(23):
        r0 = 1 + 2 * pair_idx
        r1 = r0 + 1
        for c in range(1, 63):
            if (r0, c) not in RESERVED_CELLS and (r1, c) not in RESERVED_CELLS:
                coords.append((r0, c))
                coords.append((r1, c))
    return tuple(coords)


DATA_CELL_COORDS: Final[tuple[tuple[int, int], ...]] = _build_data_cell_coords()
DATA_CELL_COUNT: Final[int] = len(DATA_CELL_COORDS)
assert DATA_CELL_COUNT == 2528, f"Expected 2528 data cells, got {DATA_CELL_COUNT}"

_DATA_ROWS: Final[np.ndarray] = np.array([rc[0] for rc in DATA_CELL_COORDS], dtype=np.intp)
_DATA_COLS: Final[np.ndarray] = np.array([rc[1] for rc in DATA_CELL_COORDS], dtype=np.intp)


def _build_base_grid_template() -> np.ndarray:
    grid = np.zeros((GRID_ROWS, GRID_COLS), dtype=np.uint8)

    def _draw_bullseye(top_r: int, left_c: int, hollow_center: bool) -> None:
        for dr in range(7):
            for dc in range(7):
                dist = max(abs(dr - 3), abs(dc - 3))
                if dist in (1, 3):
                    val = 1
                elif dist == 0:
                    val = 0 if hollow_center else 1
                else:
                    val = 0
                grid[top_r + dr, left_c + dc] = val

    # TL has 1x1 white hole at (4, 4); TR/BL/BR have solid 3x3 black cores
    _draw_bullseye(1, 1, hollow_center=True)
    _draw_bullseye(1, 56, hollow_center=False)
    _draw_bullseye(40, 1, hollow_center=False)
    _draw_bullseye(40, 56, hollow_center=False)

    # TL L-beacon is all black (1); TR/BL/BR remain white (0)
    for r, c in TL_BEACON_COORDS:
        grid[r, c] = 1

    # Center 2x2 calibration checkerboard
    for (r, c), val in CENTER_CHECKERBOARD_COORDS.items():
        grid[r, c] = val

    grid.setflags(write=False)
    return grid


_BASE_GRID_TEMPLATE: Final[np.ndarray] = _build_base_grid_template()

# ---------------------------------------------------------------------------
# Frame, ECC & Whitening Constants
# ---------------------------------------------------------------------------

TILE_HEADER_STRUCT: Final[struct.Struct] = struct.Struct(">BBIHHBB")
TILE_CRC_STRUCT: Final[struct.Struct] = struct.Struct(">I")
TILE_HEADER_BYTES: Final[int] = TILE_HEADER_STRUCT.size  # 12
TILE_PAYLOAD_BYTES: Final[int] = 156
TILE_CRC_BYTES: Final[int] = TILE_CRC_STRUCT.size  # 4
TILE_PLAIN_BYTES: Final[int] = TILE_HEADER_BYTES + TILE_PAYLOAD_BYTES + TILE_CRC_BYTES  # 172

RS_NSYM: Final[int] = 72
TILE_RS_MSG_BYTES: Final[int] = TILE_PLAIN_BYTES // 2  # 86
TILE_RS_BLOCK_BYTES: Final[int] = TILE_RS_MSG_BYTES + RS_NSYM  # 158
TILE_CODED_BYTES: Final[int] = 2 * TILE_RS_BLOCK_BYTES  # 316

assert TILE_CODED_BYTES * 8 == DATA_CELL_COUNT

CONTAINER_MAGIC: Final[bytes] = b"PV"
INNER_NAME_LEN_STRUCT: Final[struct.Struct] = struct.Struct(">H")
INNER_RAW_LEN_STRUCT: Final[struct.Struct] = struct.Struct(">Q")
FILE_ID_STRUCT: Final[struct.Struct] = struct.Struct(">I")

_RS_CODEC: Final[RSCodec] = RSCodec(RS_NSYM)
_RS_LOCK: Final[threading.Lock] = threading.Lock()


def _build_whitening_mask() -> bytes:
    chunks: list[bytes] = []
    idx = 0
    total = 0
    while total < TILE_CODED_BYTES:
        digest = hashlib.sha256(f"PV-RIBBON-v1-WHITEN-{idx}".encode("ascii")).digest()
        chunks.append(digest)
        total += len(digest)
        idx += 1
    return b"".join(chunks)[:TILE_CODED_BYTES]


WHITENING_MASK: Final[bytes] = _build_whitening_mask()
_WHITENING_ARRAY: Final[np.ndarray] = np.frombuffer(WHITENING_MASK, dtype=np.uint8)

# ---------------------------------------------------------------------------
# Public Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EncodeEstimate:
    """Capacity, compression, and physical thermal-paper estimate for a file."""

    filename: str
    raw_size: int
    inner_size: int
    container_size: int
    compressed: bool
    compression_ratio: float
    file_id: int
    raw_sha256: str
    cell_size: int
    tile_count: int
    tile_width_px: int
    tile_height_px: int
    margin_x_px: int
    inter_tile_gap_px: int
    pitch_px: int
    ribbon_width_px: int
    ribbon_height_px: int
    paper_length_cm: float
    net_bytes_per_cm: float
    container_bytes_per_cm: float


@dataclass(frozen=True)
class EncodedRibbon:
    """Complete encoded PV-RIBBON v1 image and per-tile metadata in RAM."""

    estimate: EncodeEstimate
    ribbon_image: np.ndarray
    tile_images: tuple[np.ndarray, ...]
    tile_grids: tuple[np.ndarray, ...]
    tile_frames: tuple[bytes, ...]
    tile_rects: tuple[tuple[int, int, int, int], ...]
    file_id: int
    filename: str
    raw_size: int
    container_size: int
    compressed: bool
    raw_sha256: str
    cell_size: int
    tile_count: int
    ribbon_width_px: int
    ribbon_height_px: int
    paper_length_cm: float
    pitch_px: int
    margin_x_px: int
    margin_y_px: int
    inter_tile_gap_px: int
    data_bit_density: float
    overall_ink_density: float


@dataclass(frozen=True)
class DecodedTileFrame:
    """Validated and Reed-Solomon repaired PV-RIBBON v1 tile payload."""

    version: int
    flags: int
    file_id: int
    tile_index: int
    total_tiles: int
    payload_len: int
    payload: bytes
    crc32: int
    rs_errors_corrected: int


@dataclass(frozen=True)
class RecoveredFile:
    """Fully reassembled, decompressed, and SHA-256 verified file payload."""

    filename: str
    data: bytes
    raw_size: int
    container_size: int
    compressed: bool
    sha256: str
    sha256_bytes: bytes
    file_id: int

    @property
    def raw_bytes(self) -> bytes:
        return self.data


# ---------------------------------------------------------------------------
# Geometry & Base Grid Helpers
# ---------------------------------------------------------------------------


def get_tile_geometry(cell_size: int = DEFAULT_CELL_SIZE) -> dict[str, float | int]:
    """Return physical pixel and centimeter geometry for a valid cell size (5 or 6)."""
    if cell_size not in VALID_CELL_SIZES:
        raise ValueError(f"Unsupported cell_size={cell_size}; expected one of {VALID_CELL_SIZES}")
    geom = dict(TILE_GEOMETRY[cell_size])
    geom["margin_y_px"] = RIBBON_MARGIN_Y_PX
    return geom


def build_base_grid() -> np.ndarray:
    """Return a mutable (48, 64) uint8 logical grid with border, fiducials, beacons, and center mark."""
    return _BASE_GRID_TEMPLATE.copy()


# ---------------------------------------------------------------------------
# Container & Filename Sanitization
# ---------------------------------------------------------------------------


def _sanitize_filename(filename: str) -> str:
    if not isinstance(filename, str):
        raise TypeError("filename must be a str")
    cleaned = filename.replace("\\", "/").rsplit("/", 1)[-1]
    if len(cleaned) >= 2 and cleaned[1] == ":" and cleaned[0].isalpha():
        cleaned = cleaned[2:]
    cleaned = "".join(ch for ch in cleaned if ord(ch) >= 32 and ch != "\x7f").strip()
    cleaned = cleaned.replace("/", "_").replace("\\", "_")
    if cleaned in ("", ".", ".."):
        return "file.bin"
    return cleaned


def _build_container(raw_bytes: bytes, filename: str) -> tuple[str, bytes, int, bool, int, bytes]:
    raw = bytes(raw_bytes)
    safe_name = _sanitize_filename(filename)
    name_utf8 = safe_name.encode("utf-8")
    if len(name_utf8) > 65535:
        raise ValueError("Sanitized filename exceeds 65535 UTF-8 bytes")

    raw_sha256_bytes = hashlib.sha256(raw).digest()
    inner = (
        CONTAINER_MAGIC
        + INNER_NAME_LEN_STRUCT.pack(len(name_utf8))
        + name_utf8
        + INNER_RAW_LEN_STRUCT.pack(len(raw))
        + raw_sha256_bytes
        + raw
    )

    comp = lzma.compress(inner, format=lzma.FORMAT_XZ, preset=6)
    if len(comp) < len(inner):
        compressed = True
        container = b"PV\x01" + comp
    else:
        compressed = False
        container = b"PV\x00" + inner

    file_id = FILE_ID_STRUCT.unpack(hashlib.sha256(container).digest()[:4])[0]
    return safe_name, container, len(inner), compressed, file_id, raw_sha256_bytes


def unpack_file_container(container: bytes) -> RecoveredFile:
    """Parse, optionally LZMA-decompress, and SHA-256 verify a PV-RIBBON container."""
    data = bytes(container)
    if len(data) < 3 or data[:2] != CONTAINER_MAGIC:
        raise ValueError("Invalid PV-RIBBON container magic")

    comp_flag = data[2]
    if comp_flag == 1:
        compressed = True
        try:
            inner = lzma.decompress(data[3:], format=lzma.FORMAT_XZ)
        except lzma.LZMAError as exc:
            raise ValueError(f"LZMA container decompression failed: {exc}") from exc
    elif comp_flag == 0:
        compressed = False
        inner = data[3:]
    else:
        raise ValueError(f"Unknown container compression flag: {comp_flag:#x}")

    min_inner_len = 2 + INNER_NAME_LEN_STRUCT.size + INNER_RAW_LEN_STRUCT.size + 32
    if len(inner) < min_inner_len or inner[:2] != CONTAINER_MAGIC:
        raise ValueError("Invalid inner PV package header")

    pos = 2
    (name_len,) = INNER_NAME_LEN_STRUCT.unpack_from(inner, pos)
    pos += INNER_NAME_LEN_STRUCT.size

    if pos + name_len + INNER_RAW_LEN_STRUCT.size + 32 > len(inner):
        raise ValueError("Truncated inner PV package metadata")

    try:
        decoded_name = inner[pos : pos + name_len].decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Invalid UTF-8 filename in container: {exc}") from exc
    safe_name = _sanitize_filename(decoded_name)
    pos += name_len

    (raw_len,) = INNER_RAW_LEN_STRUCT.unpack_from(inner, pos)
    pos += INNER_RAW_LEN_STRUCT.size

    expected_sha = inner[pos : pos + 32]
    pos += 32

    remaining = len(inner) - pos
    if remaining != raw_len:
        raise ValueError(f"Inner payload length mismatch: expected {raw_len}, got {remaining}")

    raw_bytes = inner[pos:]
    actual_sha = hashlib.sha256(raw_bytes).digest()
    if actual_sha != expected_sha:
        raise ValueError("SHA-256 checksum mismatch on recovered file payload")

    file_id = FILE_ID_STRUCT.unpack(hashlib.sha256(data).digest()[:4])[0]
    return RecoveredFile(
        filename=safe_name,
        data=raw_bytes,
        raw_size=raw_len,
        container_size=len(data),
        compressed=compressed,
        sha256=actual_sha.hex(),
        sha256_bytes=actual_sha,
        file_id=file_id,
    )


# ---------------------------------------------------------------------------
# Tile Frame Framing, 2-Way Interleaved RS(158, 86), & Whitening
# ---------------------------------------------------------------------------


def _apply_whitening(data_316: bytes) -> bytes:
    arr = np.frombuffer(data_316, dtype=np.uint8)
    return np.bitwise_xor(arr, _WHITENING_ARRAY).tobytes()


def encode_tile_bytes(
    file_id: int,
    tile_index: int,
    total_tiles: int,
    payload: bytes,
    flags: int = 0,
    whiten: bool = True,
) -> bytes:
    """Build 172B plain tile frame, 2-way interleave RS(158, 86), and whiten to 316B."""
    payload_bytes = bytes(payload)
    payload_len = len(payload_bytes)
    if payload_len > TILE_PAYLOAD_BYTES:
        raise ValueError(f"Tile payload length {payload_len} exceeds {TILE_PAYLOAD_BYTES} bytes")
    if not (1 <= total_tiles <= 0xFFFF):
        raise ValueError(f"total_tiles={total_tiles} out of range 1..65535")
    if not (0 <= tile_index < total_tiles):
        raise ValueError(f"tile_index={tile_index} out of range 0..{total_tiles - 1}")
    if not (0 <= file_id <= 0xFFFFFFFF):
        raise ValueError(f"file_id={file_id} out of range 0..0xFFFFFFFF")
    if not (0 <= flags <= 0xFF):
        raise ValueError(f"flags={flags} out of range 0..255")

    header = TILE_HEADER_STRUCT.pack(
        PROTOCOL_VERSION,
        flags,
        file_id,
        tile_index,
        total_tiles,
        payload_len,
        0,
    )
    payload_padded = payload_bytes.ljust(TILE_PAYLOAD_BYTES, b"\x00")
    body = header + payload_padded
    crc = zlib.crc32(body) & 0xFFFFFFFF
    plain = body + TILE_CRC_STRUCT.pack(crc)
    assert len(plain) == TILE_PLAIN_BYTES

    with _RS_LOCK:
        cw0 = bytes(_RS_CODEC.encode(plain[:TILE_RS_MSG_BYTES]))
        cw1 = bytes(_RS_CODEC.encode(plain[TILE_RS_MSG_BYTES:]))

    coded = bytearray(TILE_CODED_BYTES)
    coded[0::2] = cw0
    coded[1::2] = cw1
    coded_bytes = bytes(coded)
    return _apply_whitening(coded_bytes) if whiten else coded_bytes


def _decode_rs_block(block: bytes) -> tuple[bytes, int]:
    res = _RS_CODEC.decode(block)
    if not isinstance(res, tuple):
        raise ValueError("Unexpected RSCodec.decode return type")
    if len(res) == 3:
        msg, _msgecc, errata_pos = res
        return bytes(msg), len(errata_pos)
    if len(res) == 2:
        msg, msgecc = res
        err_count = sum(a != b for a, b in zip(block, msgecc))
        return bytes(msg), err_count
    raise ValueError(f"Unexpected RSCodec.decode return tuple length: {len(res)}")


def decode_tile_bytes(tile_bytes: bytes, whiten: bool = True) -> Optional[DecodedTileFrame]:
    """Un-whiten, de-interleave, RS-decode, CRC-verify, and validate a 316B tile frame."""
    if not isinstance(tile_bytes, (bytes, bytearray, memoryview)):
        return None
    raw = bytes(tile_bytes)
    if len(raw) != TILE_CODED_BYTES:
        return None

    coded = _apply_whitening(raw) if whiten else raw
    cw0 = coded[0::2]
    cw1 = coded[1::2]

    try:
        with _RS_LOCK:
            msg0, err0 = _decode_rs_block(cw0)
            msg1, err1 = _decode_rs_block(cw1)
    except (ReedSolomonError, ValueError):
        return None

    plain = msg0 + msg1
    if len(plain) != TILE_PLAIN_BYTES:
        return None

    body = plain[: TILE_HEADER_BYTES + TILE_PAYLOAD_BYTES]
    (crc_stored,) = TILE_CRC_STRUCT.unpack_from(plain, TILE_HEADER_BYTES + TILE_PAYLOAD_BYTES)
    crc_calc = zlib.crc32(body) & 0xFFFFFFFF
    if crc_calc != crc_stored:
        return None

    version, flags, file_id, tile_index, total_tiles, payload_len, _reserved = (
        TILE_HEADER_STRUCT.unpack_from(plain, 0)
    )
    if version != PROTOCOL_VERSION:
        return None
    if payload_len > TILE_PAYLOAD_BYTES:
        return None
    if total_tiles < 1 or tile_index >= total_tiles:
        return None

    payload = plain[TILE_HEADER_BYTES : TILE_HEADER_BYTES + payload_len]
    return DecodedTileFrame(
        version=version,
        flags=flags,
        file_id=file_id,
        tile_index=tile_index,
        total_tiles=total_tiles,
        payload_len=payload_len,
        payload=payload,
        crc32=crc_stored,
        rs_errors_corrected=int(err0 + err1),
    )


# ---------------------------------------------------------------------------
# Tile & Ribbon Rendering / File Estimation
# ---------------------------------------------------------------------------


def render_tile_grid(tile_bytes: bytes) -> np.ndarray:
    """Map 316 encoded tile bytes into a (48, 64) uint8 logical grid (1=black, 0=white)."""
    raw = bytes(tile_bytes)
    if len(raw) != TILE_CODED_BYTES:
        raise ValueError(f"Expected {TILE_CODED_BYTES} tile bytes, got {len(raw)}")
    bits = np.unpackbits(np.frombuffer(raw, dtype=np.uint8), bitorder="big")
    grid = build_base_grid()
    grid[_DATA_ROWS, _DATA_COLS] = bits
    return grid


def render_tile_image(
    grid: np.ndarray | bytes,
    cell_size: int = DEFAULT_CELL_SIZE,
    pad_to_ribbon_width: bool = False,
) -> np.ndarray:
    """Render a (48, 64) grid (or 316 tile bytes) to uint8 monochrome pixels (255=white, 0=black)."""
    if cell_size not in VALID_CELL_SIZES:
        raise ValueError(f"Unsupported cell_size={cell_size}; expected one of {VALID_CELL_SIZES}")

    if isinstance(grid, (bytes, bytearray, memoryview)):
        grid_arr = render_tile_grid(bytes(grid))
    else:
        grid_arr = np.asarray(grid)
        if grid_arr.shape != (GRID_ROWS, GRID_COLS):
            raise ValueError(f"Expected grid shape {(GRID_ROWS, GRID_COLS)}, got {grid_arr.shape}")

    expanded = np.repeat(np.repeat(grid_arr != 0, cell_size, axis=0), cell_size, axis=1)
    tile_img = np.where(expanded, np.uint8(BLACK_PX), np.uint8(WHITE_PX))

    if not pad_to_ribbon_width:
        return tile_img

    geom = TILE_GEOMETRY[cell_size]
    margin_x = int(geom["margin_x_px"])
    if margin_x == 0:
        return tile_img
    tile_h, tile_w = tile_img.shape
    padded = np.full((tile_h, PRINTER_WIDTH_PX), WHITE_PX, dtype=np.uint8)
    padded[:, margin_x : margin_x + tile_w] = tile_img
    return padded


def _build_estimate_from_container(
    safe_name: str,
    raw_size: int,
    inner_size: int,
    container_size: int,
    compressed: bool,
    file_id: int,
    raw_sha256_bytes: bytes,
    cell_size: int,
) -> EncodeEstimate:
    geom = get_tile_geometry(cell_size)
    tile_w = int(geom["tile_width_px"])
    tile_h = int(geom["tile_height_px"])
    margin_x = int(geom["margin_x_px"])
    gap = int(geom["inter_tile_gap_px"])
    pitch = int(geom["pitch_px"])

    tile_count = max(1, (container_size + TILE_PAYLOAD_BYTES - 1) // TILE_PAYLOAD_BYTES)
    if tile_count > 0xFFFF:
        raise ValueError(f"File requires {tile_count} tiles, exceeding PV-RIBBON v1 limit of 65535")

    ribbon_height = (
        2 * RIBBON_MARGIN_Y_PX
        + tile_count * tile_h
        + (tile_count - 1) * gap
    )
    paper_length_cm = ribbon_height / PX_PER_CM
    compression_ratio = (container_size / raw_size) if raw_size > 0 else 1.0
    net_bytes_per_cm = (raw_size / paper_length_cm) if paper_length_cm > 0 else 0.0
    container_bytes_per_cm = (container_size / paper_length_cm) if paper_length_cm > 0 else 0.0

    return EncodeEstimate(
        filename=safe_name,
        raw_size=raw_size,
        inner_size=inner_size,
        container_size=container_size,
        compressed=compressed,
        compression_ratio=compression_ratio,
        file_id=file_id,
        raw_sha256=raw_sha256_bytes.hex(),
        cell_size=cell_size,
        tile_count=tile_count,
        tile_width_px=tile_w,
        tile_height_px=tile_h,
        margin_x_px=margin_x,
        inter_tile_gap_px=gap,
        pitch_px=pitch,
        ribbon_width_px=PRINTER_WIDTH_PX,
        ribbon_height_px=ribbon_height,
        paper_length_cm=paper_length_cm,
        net_bytes_per_cm=net_bytes_per_cm,
        container_bytes_per_cm=container_bytes_per_cm,
    )


def estimate_file(
    raw_bytes: bytes,
    filename: str = "file.bin",
    cell_size: int = DEFAULT_CELL_SIZE,
) -> EncodeEstimate:
    """Compute container size, tile count, and physical thermal paper length in RAM."""
    if cell_size not in VALID_CELL_SIZES:
        raise ValueError(f"Unsupported cell_size={cell_size}; expected one of {VALID_CELL_SIZES}")
    raw = bytes(raw_bytes)
    safe_name, container, inner_size, compressed, file_id, raw_sha = _build_container(raw, filename)
    return _build_estimate_from_container(
        safe_name=safe_name,
        raw_size=len(raw),
        inner_size=inner_size,
        container_size=len(container),
        compressed=compressed,
        file_id=file_id,
        raw_sha256_bytes=raw_sha,
        cell_size=cell_size,
    )


def encode_file_to_ribbon(
    raw_bytes: bytes,
    filename: str = "file.bin",
    cell_size: int = DEFAULT_CELL_SIZE,
) -> EncodedRibbon:
    """Encode raw file bytes in RAM into a 384px-wide PV-RIBBON v1 thermal tape image."""
    if cell_size not in VALID_CELL_SIZES:
        raise ValueError(f"Unsupported cell_size={cell_size}; expected one of {VALID_CELL_SIZES}")
    raw = bytes(raw_bytes)
    safe_name, container, inner_size, compressed, file_id, raw_sha = _build_container(raw, filename)
    est = _build_estimate_from_container(
        safe_name=safe_name,
        raw_size=len(raw),
        inner_size=inner_size,
        container_size=len(container),
        compressed=compressed,
        file_id=file_id,
        raw_sha256_bytes=raw_sha,
        cell_size=cell_size,
    )

    ribbon = np.full((est.ribbon_height_px, PRINTER_WIDTH_PX), WHITE_PX, dtype=np.uint8)
    tile_frames: list[bytes] = []
    tile_grids: list[np.ndarray] = []
    tile_images: list[np.ndarray] = []
    tile_rects: list[tuple[int, int, int, int]] = []

    data_black_sum = 0
    total_black_sum = 0

    for idx in range(est.tile_count):
        chunk = container[idx * TILE_PAYLOAD_BYTES : (idx + 1) * TILE_PAYLOAD_BYTES]
        frame_bytes = encode_tile_bytes(
            file_id=file_id,
            tile_index=idx,
            total_tiles=est.tile_count,
            payload=chunk,
        )
        grid = render_tile_grid(frame_bytes)
        tile_img = render_tile_image(grid, cell_size=cell_size, pad_to_ribbon_width=False)

        y = RIBBON_MARGIN_Y_PX + idx * est.pitch_px
        x = est.margin_x_px
        ribbon[y : y + est.tile_height_px, x : x + est.tile_width_px] = tile_img

        tile_frames.append(frame_bytes)
        tile_grids.append(grid)
        tile_images.append(tile_img)
        tile_rects.append((x, y, est.tile_width_px, est.tile_height_px))

        data_black_sum += int(np.count_nonzero(grid[_DATA_ROWS, _DATA_COLS]))
        total_black_sum += int(np.count_nonzero(grid))

    data_bit_density = data_black_sum / (est.tile_count * DATA_CELL_COUNT)
    overall_ink_density = total_black_sum / (est.tile_count * TOTAL_GRID_CELLS)

    return EncodedRibbon(
        estimate=est,
        ribbon_image=ribbon,
        tile_images=tuple(tile_images),
        tile_grids=tuple(tile_grids),
        tile_frames=tuple(tile_frames),
        tile_rects=tuple(tile_rects),
        file_id=file_id,
        filename=safe_name,
        raw_size=len(raw),
        container_size=len(container),
        compressed=compressed,
        raw_sha256=est.raw_sha256,
        cell_size=cell_size,
        tile_count=est.tile_count,
        ribbon_width_px=PRINTER_WIDTH_PX,
        ribbon_height_px=est.ribbon_height_px,
        paper_length_cm=est.paper_length_cm,
        pitch_px=est.pitch_px,
        margin_x_px=est.margin_x_px,
        margin_y_px=RIBBON_MARGIN_Y_PX,
        inter_tile_gap_px=est.inter_tile_gap_px,
        data_bit_density=data_bit_density,
        overall_ink_density=overall_ink_density,
    )


# ---------------------------------------------------------------------------
# Secure File Shredder (Multi-pass overwrite + Truncate + Double Rename + Unlink)
# ---------------------------------------------------------------------------

_SHRED_CHUNK_SIZE: Final[int] = 65536


def secure_shred_file(filepath: str, passes: int = 3) -> None:
    """Overwrite file in 64KB RAM chunks, fsync, truncate, double-rename, and unlink."""
    if passes < 1:
        raise ValueError(f"passes must be >= 1, got {passes}")

    abs_path = os.path.abspath(os.fspath(filepath))
    if not os.path.lexists(abs_path):
        raise FileNotFoundError(f"File not found: {abs_path}")
    if os.path.islink(abs_path):
        raise ValueError(f"Refusing to shred symbolic link: {abs_path}")
    if not os.path.isfile(abs_path):
        raise IsADirectoryError(f"Not a regular file: {abs_path}")

    file_size = os.path.getsize(abs_path)
    # Build pass sequence: for default passes=3 -> (os.urandom, 0xFF, os.urandom), then final 0x00
    pass_modes: list[Optional[int]] = [None if (p % 2 == 0) else 0xFF for p in range(passes)]
    pass_modes.append(0x00)

    with open(abs_path, "r+b") as fh:
        for byte_fill in pass_modes:
            fh.seek(0)
            remaining = file_size
            fixed_buf = (
                bytes([byte_fill]) * min(_SHRED_CHUNK_SIZE, file_size)
                if (byte_fill is not None and file_size > 0)
                else None
            )
            while remaining > 0:
                chunk_len = min(_SHRED_CHUNK_SIZE, remaining)
                chunk = os.urandom(chunk_len) if fixed_buf is None else fixed_buf[:chunk_len]
                fh.write(chunk)
                remaining -= chunk_len
            fh.flush()
            os.fsync(fh.fileno())

        fh.seek(0)
        fh.truncate(0)
        fh.flush()
        os.fsync(fh.fileno())

    parent_dir = os.path.dirname(abs_path)
    current_path = abs_path
    for _ in range(2):
        while True:
            candidate = os.path.join(parent_dir, f".pv_shred_{secrets.token_hex(12)}.tmp")
            if not os.path.lexists(candidate):
                break
        os.replace(current_path, candidate)
        current_path = candidate

    os.unlink(current_path)


__all__ = [
    "BLACK_PX",
    "BL_BEACON_COORDS",
    "BR_BEACON_COORDS",
    "CENTER_CHECKERBOARD_COORDS",
    "CONTAINER_MAGIC",
    "DATA_CELL_COORDS",
    "DATA_CELL_COUNT",
    "DEFAULT_CELL_SIZE",
    "DecodedTileFrame",
    "EncodeEstimate",
    "EncodedRibbon",
    "FIDUCIAL_CENTERS",
    "GRID_COLS",
    "GRID_ROWS",
    "PRINTER_DPI",
    "PRINTER_WIDTH_PX",
    "PROTOCOL_NAME",
    "PROTOCOL_VERSION",
    "PX_PER_CM",
    "RESERVED_CELLS",
    "RIBBON_MARGIN_Y_PX",
    "RS_NSYM",
    "RecoveredFile",
    "TILE_CODED_BYTES",
    "TILE_CRC_BYTES",
    "TILE_GEOMETRY",
    "TILE_HEADER_BYTES",
    "TILE_PAYLOAD_BYTES",
    "TILE_PLAIN_BYTES",
    "TILE_RS_BLOCK_BYTES",
    "TILE_RS_MSG_BYTES",
    "TL_BEACON_COORDS",
    "TR_BEACON_COORDS",
    "TOTAL_GRID_CELLS",
    "VALID_CELL_SIZES",
    "WHITENING_MASK",
    "WHITE_PX",
    "build_base_grid",
    "decode_tile_bytes",
    "encode_file_to_ribbon",
    "encode_tile_bytes",
    "estimate_file",
    "get_tile_geometry",
    "render_tile_grid",
    "render_tile_image",
    "secure_shred_file",
    "unpack_file_container",
]
