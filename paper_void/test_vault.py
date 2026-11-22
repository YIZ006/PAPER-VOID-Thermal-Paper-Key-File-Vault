"""
Automated end-to-end verification for PAPER-VOID Hybrid Paper-Key Vault (vault.py):
1. Streaming AES-256-GCM encryption of a 5 MB binary file in 1 MB chunks.
2. Rendering of 1-Tile 384-px thermal Key Card (~8-10 cm) strictly in RAM.
3. Optical torture on the Key Card (180 deg + skew, perspective warp, Gaussian blur,
   lighting gradient + glare, >=5% physical scratches) saved as simulated phone JPEG.
4. Phone-photo decode via scanner.decode_key_card_from_media() -> streaming decrypt ->
   100% SHA-256 match.
5. 2FA passphrase (scrypt) + Base32 manual recovery code (including typo rejection) +
   1-bit vault tamper detection (GCM tag failure & atomic .part cleanup).
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import time

import cv2
import numpy as np

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import scanner
import test_stress
import vault


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    t0 = time.perf_counter()
    rng = np.random.default_rng(20261122)

    with tempfile.TemporaryDirectory(prefix="pv_vault_test_") as tmp:
        # 1. Create a 5 MB random binary file
        src_path = os.path.join(tmp, "hồ_sơ_mật_5mb.bin")
        size_5mb = 5 * 1024 * 1024 + 1337
        with open(src_path, "wb") as fh:
            rem = size_5mb
            while rem > 0:
                n = min(1024 * 1024, rem)
                fh.write(os.urandom(n))
                rem -= n
        orig_sha = _sha256_file(src_path)

        # 2. Streaming Lock (1 MB chunks, no 2FA)
        vault_path = os.path.join(tmp, "hồ_sơ_mật_5mb.bin.pvvault")
        progress_calls: list[tuple[int, int]] = []
        lock_res = vault.lock_file_streaming(
            src_path,
            vault_path,
            chunk_size=1024 * 1024,
            progress_callback=lambda d, t: progress_calls.append((d, t)),
        )
        assert lock_res.chunks_written == 6
        assert lock_res.card_image.shape[1] == 384
        assert 7.0 <= lock_res.card_length_cm <= 11.5
        assert len(progress_calls) >= 6 and progress_calls[-1] == (size_5mb, size_5mb)

        # 3. Torture the optical tile inside the Key Card and save as simulated phone photo
        # Optical tile sits at y = 20 + 132 + 20 = 172 .. 172 + 288 = 460
        tile_crop = lock_res.card_image[172 : 172 + 288, 0:384].copy()
        tortured = test_stress.torture_tile_image(
            clean_tile=tile_crop,
            tile_index=2,  # forces 180 deg + strong skew!
            total_tiles=1,
            cell_size=6,
            rng=rng,
        )
        phone_photo = os.path.join(tmp, "ảnh_thẻ_khóa_điện_thoại.jpg")
        ok, enc_jpg = cv2.imencode(".jpg", tortured.frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 88])
        assert ok
        enc_jpg.tofile(phone_photo)

        # 4. Decode from Vietnamese-path phone JPEG & streaming unlock
        decoded_tile = scanner.decode_key_card_from_media([phone_photo])
        assert decoded_tile is not None, "Failed to decode tortured Key Card phone photo!"
        rec = vault.unpack_key_record(decoded_tile.payload)
        assert rec is not None and rec.vault_id == lock_res.key_record.vault_id

        out_dir = os.path.join(tmp, "recovered_photo")
        unlock_res = vault.unlock_file_streaming(vault_path, out_dir, key_record=rec)
        assert unlock_res.original_filename == "hồ_sơ_mật_5mb.bin"
        assert unlock_res.orig_size == size_5mb
        assert unlock_res.sha256_hex == orig_sha == _sha256_file(unlock_res.output_path)

        # 5. Test 2FA Passphrase + Manual Base32 Recovery Code + Typo Rejection
        vault_2fa_path = os.path.join(tmp, "secret_2fa.pvvault")
        lock_2fa = vault.lock_file_streaming(src_path, vault_2fa_path, passphrase="MatKhau@2FA#2026")
        b32_code = lock_2fa.key_record.recovery_code

        # Typo in Base32 must fail CRC-16 immediately
        bad_code = ("A" if b32_code[0] != "A" else "B") + b32_code[1:]
        try:
            vault.parse_recovery_code(bad_code)
            raise AssertionError("Corrupted Base32 code should have failed CRC-16!")
        except vault.VaultAuthError:
            pass

        # Wrong 2FA passphrase must fail
        out_2fa_dir = os.path.join(tmp, "recovered_2fa")
        try:
            vault.unlock_file_streaming(
                vault_2fa_path, out_2fa_dir, recovery_code=b32_code, passphrase="WrongPassword"
            )
            raise AssertionError("Wrong 2FA passphrase should have failed!")
        except vault.VaultAuthError:
            pass

        # Correct Base32 + 2FA passphrase must recover 5 MB bit-exact
        unlock_2fa = vault.unlock_file_streaming(
            vault_2fa_path, out_2fa_dir, recovery_code=b32_code, passphrase="MatKhau@2FA#2026"
        )
        assert unlock_2fa.sha256_hex == orig_sha == _sha256_file(unlock_2fa.output_path)

        # 6. Tampered 1 bit in .pvvault must fail GCM auth & leave 0 .part files
        with open(vault_path, "r+b") as fh:
            fh.seek(2 * 1024 * 1024)
            b = fh.read(1)
            fh.seek(2 * 1024 * 1024)
            fh.write(bytes([b[0] ^ 0x01]))
        tamper_dir = os.path.join(tmp, "tamper_out")
        try:
            vault.unlock_file_streaming(vault_path, tamper_dir, key_record=rec)
            raise AssertionError("Tampered .pvvault should have failed GCM authentication!")
        except vault.VaultAuthError:
            pass
        assert not os.path.exists(tamper_dir) or os.listdir(tamper_dir) == []

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    print("=" * 96)
    print("  PAPER-VOID HYBRID PAPER-KEY VAULT — VERIFICATION REPORT")
    print("=" * 96)
    print(f"  [1] 5 MB Streaming AES-256-GCM Lock : [PASS]  {size_5mb} B ({lock_res.chunks_written} chunks @ 1 MB) in {lock_res.elapsed_s*1000:.0f} ms")
    print(f"  [2] 1-Tile Thermal Key Card (384px) : [PASS]  {lock_res.card_image.shape[1]}x{lock_res.card_image.shape[0]} px = {lock_res.card_length_cm:.2f} cm | ID={lock_res.key_record.vault_id_hex}")
    print(f"  [3] Tortured Phone Photo (180°+5%)  : [PASS]  RS fixed={decoded_tile.rs_errors_corrected} B | Corrupt={tortured.corrupted_area_frac*100:.2f}% | SHA-256 100% MATCH")
    print(f"  [4] Base32 + 2FA scrypt + Tamper    : [PASS]  CRC-16 typo rejected | Wrong 2FA rejected | 1-bit tamper rejected")
    print(f"  TOTAL ELAPSED                       : {elapsed_ms:.1f} ms")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
