"""
PAPER-VOID BLE Thermal Printer Driver & RAM Preview.

Supports two 384-px (203 DPI) thermal printer protocol families over bleak:
1. ClassicCatPrinterProtocol (GB01 / X5 / MX06 / GT01 / YT01 / PD01 / ...):
   - Framing: 0x51 0x78 [CMD] 0x00 [LEN_LO] [LEN_HI] [PAYLOAD] [CRC8] 0xFF
   - AE01 write commands + AE02 flow-control pause/resume notifications.
2. MXW01Protocol (MXW01 series):
   - Framing: 0x22 0x21 [CMD] 0x00 [LEN_LO] [LEN_HI] [PAYLOAD] [CRC8] 0xFF
   - AE01 control channel (0xA1 status, 0xA2 intensity, 0xA9 print request, 0xAD flush)
   - AE03 high-speed bulk 1-bit monochrome row stream (48 bytes/row, MTU-chunked).

All image conversion, packing, and Tkinter dry-run preview rendering happen
strictly in RAM (NumPy / Pillow / ImageTk.PhotoImage). No temporary files are
ever written to disk.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from dataclasses import dataclass
import json
import os
import re
import struct
import subprocess
import threading
import time
import tkinter as tk
from typing import Any, Callable, Iterable, Optional, Union
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
from PIL import Image, ImageTk

try:
    from bleak import BleakClient, BleakScanner

    HAS_BLEAK = True
except ImportError:  # pragma: no cover - runtime guard if bleak missing
    BleakClient = None  # type: ignore[assignment]
    BleakScanner = None  # type: ignore[assignment]
    HAS_BLEAK = False

PRINTER_WIDTH = 384
PRINTER_WIDTH_PX = PRINTER_WIDTH
BYTES_PER_ROW = PRINTER_WIDTH // 8  # 48 bytes per 384-dot row
PRINTER_DPI = 203
PX_PER_CM = PRINTER_DPI / 2.54  # ~79.92126 px/cm

SERVICE_UUID = "0000ae30-0000-1000-8000-00805f9b34fb"
CHAR_WRITE_UUID = "0000ae01-0000-1000-8000-00805f9b34fb"
CHAR_CONTROL_UUID = CHAR_WRITE_UUID
CHAR_NOTIFY_UUID = "0000ae02-0000-1000-8000-00805f9b34fb"
CHAR_DATA_UUID = "0000ae03-0000-1000-8000-00805f9b34fb"

KNOWN_PRINTER_PREFIXES: tuple[str, ...] = (
    "X5",
    "GB",
    "GT",
    "MX",
    "YT",
    "PD",
    "SC",
    "_ZZ",
)

# Verified CRC-8 lookup table (poly 0x07, init 0x00) matching iy.f.calcCrc8
_CRC8_TABLE: tuple[int, ...] = (
    0x00, 0x07, 0x0E, 0x09, 0x1C, 0x1B, 0x12, 0x15, 0x38, 0x3F, 0x36, 0x31,
    0x24, 0x23, 0x2A, 0x2D, 0x70, 0x77, 0x7E, 0x79, 0x6C, 0x6B, 0x62, 0x65,
    0x48, 0x4F, 0x46, 0x41, 0x54, 0x53, 0x5A, 0x5D, 0xE0, 0xE7, 0xEE, 0xE9,
    0xFC, 0xFB, 0xF2, 0xF5, 0xD8, 0xDF, 0xD6, 0xD1, 0xC4, 0xC3, 0xCA, 0xCD,
    0x90, 0x97, 0x9E, 0x99, 0x8C, 0x8B, 0x82, 0x85, 0xA8, 0xAF, 0xA6, 0xA1,
    0xB4, 0xB3, 0xBA, 0xBD, 0xC7, 0xC0, 0xC9, 0xCE, 0xDB, 0xDC, 0xD5, 0xD2,
    0xFF, 0xF8, 0xF1, 0xF6, 0xE3, 0xE4, 0xED, 0xEA, 0xB7, 0xB0, 0xB9, 0xBE,
    0xAB, 0xAC, 0xA5, 0xA2, 0x8F, 0x88, 0x81, 0x86, 0x93, 0x94, 0x9D, 0x9A,
    0x27, 0x20, 0x29, 0x2E, 0x3B, 0x3C, 0x35, 0x32, 0x1F, 0x18, 0x11, 0x16,
    0x03, 0x04, 0x0D, 0x0A, 0x57, 0x50, 0x59, 0x5E, 0x4B, 0x4C, 0x45, 0x42,
    0x6F, 0x68, 0x61, 0x66, 0x73, 0x74, 0x7D, 0x7A, 0x89, 0x8E, 0x87, 0x80,
    0x95, 0x92, 0x9B, 0x9C, 0xB1, 0xB6, 0xBF, 0xB8, 0xAD, 0xAA, 0xA3, 0xA4,
    0xF9, 0xFE, 0xF7, 0xF0, 0xE5, 0xE2, 0xEB, 0xEC, 0xC1, 0xC6, 0xCF, 0xC8,
    0xDD, 0xDA, 0xD3, 0xD4, 0x69, 0x6E, 0x67, 0x60, 0x75, 0x72, 0x7B, 0x7C,
    0x51, 0x56, 0x5F, 0x58, 0x4D, 0x4A, 0x43, 0x44, 0x19, 0x1E, 0x17, 0x10,
    0x05, 0x02, 0x0B, 0x0C, 0x21, 0x26, 0x2F, 0x28, 0x3D, 0x3A, 0x33, 0x34,
    0x4E, 0x49, 0x40, 0x47, 0x52, 0x55, 0x5C, 0x5B, 0x76, 0x71, 0x78, 0x7F,
    0x6A, 0x6D, 0x64, 0x63, 0x3E, 0x39, 0x30, 0x37, 0x22, 0x25, 0x2C, 0x2B,
    0x06, 0x01, 0x08, 0x0F, 0x1A, 0x1D, 0x14, 0x13, 0xAE, 0xA9, 0xA0, 0xA7,
    0xB2, 0xB5, 0xBC, 0xBB, 0x96, 0x91, 0x98, 0x9F, 0x8A, 0x8D, 0x84, 0x83,
    0xDE, 0xD9, 0xD0, 0xD7, 0xC2, 0xC5, 0xCC, 0xCB, 0xE6, 0xE1, 0xE8, 0xEF,
    0xFA, 0xFD, 0xF4, 0xF3,
)


def crc8(data: bytes) -> int:
    """Compute CRC-8 checksum (polynomial 0x07, init 0x00) over payload bytes."""
    crc = 0
    table = _CRC8_TABLE
    for b in data:
        crc = table[(crc ^ b) & 0xFF]
    return crc


class PrinterDriverError(RuntimeError):
    """Base error raised by the BLE thermal printer driver."""


class PrintCancelledError(PrinterDriverError):
    """Raised when an active BLE print job is cancelled by the caller."""


@dataclass(frozen=True)
class PrintWriteStep:
    """Single BLE GATT write operation emitted by a protocol implementation."""

    char_uuid: str
    data: bytes
    rows_represented: int = 0
    delay_s: float = 0.0
    write_with_response: bool = False
    label: str = ""


@dataclass(frozen=True)
class BlePrinterDevice:
    """Discovered BLE thermal printer advertisement record."""

    name: str
    address: str
    rssi: Optional[int] = None
    protocol_hint: str = "classic"
    service_uuids: tuple[str, ...] = ()

    @property
    def display_label(self) -> str:
        """Human-readable label suitable for dropdown lists."""
        clean_name = self.name or "Unknown Printer"
        rssi_txt = f" | {self.rssi} dBm" if self.rssi is not None else ""
        return f"{clean_name} [{self.address}] ({self.protocol_hint.upper()}{rssi_txt})"

    def __str__(self) -> str:
        return self.display_label


@dataclass(frozen=True)
class PrintResult:
    """Summary returned after completing a BLE print job."""

    ok: bool
    protocol: str
    total_rows: int
    bytes_sent: int
    elapsed_s: float
    last_notify_hex: str = ""
    firmware: str = ""


def ribbon_to_grayscale_array(image: Union[np.ndarray, Image.Image]) -> np.ndarray:
    """
    Normalize a 384-px thermal image or mask to a 2D uint8 array (255=white, 0=black)
    strictly in RAM.
    """
    if isinstance(image, Image.Image):
        gray_img = image.convert("L")
        w, h = gray_img.size
        if w != PRINTER_WIDTH:
            new_h = max(1, round(h * PRINTER_WIDTH / w))
            gray_img = gray_img.resize((PRINTER_WIDTH, new_h), Image.Resampling.NEAREST)
        return np.ascontiguousarray(np.asarray(gray_img, dtype=np.uint8))

    arr = np.asarray(image)
    if arr.ndim == 3:
        if arr.shape[2] >= 3:
            rgb = arr[..., :3].astype(np.float32)
            gray_f = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
            arr = np.clip(np.round(gray_f), 0, 255).astype(np.uint8)
        else:
            arr = arr[..., 0]
    if arr.ndim != 2:
        raise ValueError(f"Expected 2D/3D image array, got shape {arr.shape}")

    if arr.dtype == np.bool_:
        # Boolean masks follow logical convention: True = ink (0), False = paper (255).
        arr_u8 = np.where(arr, np.uint8(0), np.uint8(255))
    else:
        arr_u8 = np.clip(arr, 0, 255).astype(np.uint8, copy=False)

    h, w = arr_u8.shape
    if w != PRINTER_WIDTH:
        new_h = max(1, round(h * PRINTER_WIDTH / w))
        pil_tmp = Image.fromarray(arr_u8, mode="L").resize(
            (PRINTER_WIDTH, new_h), Image.Resampling.NEAREST
        )
        arr_u8 = np.asarray(pil_tmp, dtype=np.uint8)

    return np.ascontiguousarray(arr_u8)


def normalize_mono_bitmap(
    image: Union[np.ndarray, Image.Image],
    threshold: int = 127,
    bold_boost: bool = False,
) -> np.ndarray:
    """
    Convert an input image or ndarray to a 2D boolean mask (H, 384) where
    True = black ink dot and False = white paper.
    """
    raw = np.asarray(image) if not isinstance(image, Image.Image) else None
    if raw is not None and raw.dtype == np.bool_ and raw.ndim == 2 and raw.shape[1] == PRINTER_WIDTH:
        black = raw.copy()
    else:
        gray_u8 = ribbon_to_grayscale_array(image)
        black = gray_u8 <= int(threshold)

    if bold_boost:
        dilated = black.copy()
        dilated[:, 1:] |= black[:, :-1]
        dilated[1:, :] |= black[:-1, :]
        black = dilated

    return np.ascontiguousarray(black, dtype=bool)


def pack_mono_rows(
    image: Union[np.ndarray, Image.Image],
    bitorder: str = "little",
    threshold: int = 127,
) -> np.ndarray:
    """
    Pack a 384-px monochrome ribbon into (H, 48) uint8 bitmap rows.
    Thermal shift registers read LSB-first ('little') per byte.
    """
    black = normalize_mono_bitmap(image, threshold=threshold)
    packed = np.packbits(black.astype(np.uint8), axis=1, bitorder=bitorder)
    return np.ascontiguousarray(packed, dtype=np.uint8)


def decode_notify_firmware(data: Union[bytes, bytearray, str]) -> str:
    """Extract dotted firmware version string (e.g. '1.3.3') from notify bytes or hex."""
    if not data:
        return ""
    if isinstance(data, str):
        try:
            raw = bytes.fromhex(data)
        except ValueError:
            return ""
    else:
        raw = bytes(data)
    if len(raw) < 9:
        return ""
    payload = raw[6:-2]
    ascii_text = "".join(chr(b) if 0x20 <= b <= 0x7E else " " for b in payload)
    match = re.search(r"\d+\.\d+(?:\.\d+)*", ascii_text)
    return match.group(0) if match else ""


def detect_protocol(device_name: str, char_uuids: Iterable[str] = ()) -> str:
    """
    Detect printer protocol family ('mxw01' or 'classic') from advertised name
    and/or discovered GATT characteristic UUIDs.
    """
    for raw_uuid in char_uuids or ():
        norm = str(raw_uuid).strip().lower()
        if norm == CHAR_DATA_UUID or norm.startswith("0000ae03") or "ae03" in norm:
            return "mxw01"

    upper_name = (device_name or "").strip().upper()
    if "MXW" in upper_name:
        return "mxw01"
    return "classic"


class ThermalPrinterProtocol(ABC):
    """Abstract base class for 384-px BLE thermal printer binary protocols."""

    protocol_id: str = ""
    display_name: str = ""
    service_uuid: str = SERVICE_UUID
    control_char_uuid: str = CHAR_WRITE_UUID
    notify_char_uuid: Optional[str] = CHAR_NOTIFY_UUID
    data_char_uuid: Optional[str] = None
    frame_magic: bytes = b"\x51\x78"

    @classmethod
    def frame_packet(cls, cmd: int, payload: bytes = b"") -> bytes:
        """Build `[MAGIC_0, MAGIC_1, CMD, 0x00, LEN_LO, LEN_HI, PAYLOAD, CRC8, 0xFF]`."""
        payload_bytes = bytes(payload)
        length = len(payload_bytes)
        if length > 0xFFFF:
            raise ValueError(f"Payload length {length} exceeds 65535 bytes")
        header = bytes(
            [
                cls.frame_magic[0],
                cls.frame_magic[1],
                cmd & 0xFF,
                0x00,
                length & 0xFF,
                (length >> 8) & 0xFF,
            ]
        )
        return header + payload_bytes + bytes([crc8(payload_bytes), 0xFF])

    @abstractmethod
    def make_ping_cmd(self) -> bytes:
        """Build the protocol's device state / status query packet."""

    @abstractmethod
    def build_print_steps(
        self,
        image: Union[np.ndarray, Image.Image],
        *,
        energy: int = 22000,
        speed: int = 28,
        blackening: int = 0x35,
        draw_mode: int = 0x01,
        feed_lines: int = 72,
        mtu_payload: int = 180,
    ) -> list[PrintWriteStep]:
        """Compile an ordered list of GATT write steps for the given ribbon image."""

    @abstractmethod
    def build_print_payload(
        self,
        image: Union[np.ndarray, Image.Image],
        *,
        energy: int = 22000,
        speed: int = 28,
        blackening: int = 0x35,
        draw_mode: int = 0x01,
        feed_lines: int = 72,
    ) -> bytes:
        """Build a contiguous binary byte representation of the print job for offline tests."""

    def is_flow_pause(self, data: bytes) -> bool:
        """Return True when an AE02 notification requests pausing transmission."""
        return False

    def is_flow_resume(self, data: bytes) -> bool:
        """Return True when an AE02 notification requests resuming transmission."""
        return False

    def parse_notification(self, data: bytes) -> dict[str, Any]:
        """Decode an incoming notification packet into a structured status dictionary."""
        raw = bytes(data)
        flow: Optional[str] = None
        if self.is_flow_pause(raw):
            flow = "pause"
        elif self.is_flow_resume(raw):
            flow = "resume"
        return {
            "raw_hex": raw.hex(),
            "cmd": raw[2] if len(raw) >= 3 else None,
            "flow": flow,
            "firmware": decode_notify_firmware(raw),
        }


class ClassicCatPrinterProtocol(ThermalPrinterProtocol):
    """
    Classic CatPrinter / TinyPrint X5 / GB01 / MX06 / GT01 BLE protocol.

    Service `0000ae30`, write characteristic `0000ae01`, notify `0000ae02`.
    Framing: `0x51 0x78 [CMD] 0x00 [LEN_LO] [LEN_HI] [PAYLOAD] [CRC8] 0xFF`.
    """

    protocol_id = "classic"
    display_name = "Classic CatPrinter (GB01/X5/MX06/GT01)"
    service_uuid = SERVICE_UUID
    control_char_uuid = CHAR_WRITE_UUID
    notify_char_uuid = CHAR_NOTIFY_UUID
    data_char_uuid = None
    frame_magic = b"\x51\x78"

    CMD_RETRACT = 0xA0
    CMD_FEED = 0xA1
    CMD_PRINT_ROW = 0xA2
    CMD_START_PRINT = 0xA3
    CMD_BLACKENING = 0xA4
    CMD_LATTICE = 0xA6
    CMD_GET_DEV_STATE = 0xA8
    CMD_UPDATE_DEV = 0xA9
    CMD_FLOW_CONTROL = 0xAE
    CMD_SET_ENERGY = 0xAF
    CMD_SPEED = 0xBD
    CMD_DRAW_MODE = 0xBE

    BLACKENING_LIGHT = 0x32
    BLACKENING_NORMAL = 0x33
    BLACKENING_DARK = 0x35
    VALID_BLACKENING = (BLACKENING_LIGHT, BLACKENING_NORMAL, BLACKENING_DARK)

    DRAW_MODE_PHOTO = 0x00
    DRAW_MODE_TEXT = 0x01
    DRAW_MODE_GRAY = 0x02
    DRAW_MODE_DEEP = 0x03

    LATTICE_START = bytes.fromhex("aa551738445f5f5f44382c")
    LATTICE_END = bytes.fromhex("aa55170000000000000017")

    @classmethod
    def make_cmd(cls, cmd: int, payload: bytes = b"") -> bytes:
        """Wrap command byte and payload in a `51 78 ... CRC 0xFF` packet."""
        return cls.frame_packet(cmd, payload)

    @classmethod
    def make_ping_cmd(cls) -> bytes:
        """Build `0xA8` device state query packet (`5178a80001000000ff`)."""
        return cls.make_cmd(cls.CMD_GET_DEV_STATE, b"\x00")

    @classmethod
    def make_blackening_cmd(cls, blackening: int = BLACKENING_DARK) -> bytes:
        """Build `0xA4` blackening level command (`0x32`, `0x33`, or `0x35`)."""
        ival = int(blackening)
        snapped = min(cls.VALID_BLACKENING, key=lambda candidate: abs(candidate - ival))
        return cls.make_cmd(cls.CMD_BLACKENING, bytes([snapped]))

    @classmethod
    def make_energy_cmd(cls, energy: int = 22000) -> bytes:
        """Build `0xAF` thermal energy command (`<H`, clamped 3000..45000)."""
        clamped = max(3000, min(45000, int(energy)))
        return cls.make_cmd(cls.CMD_SET_ENERGY, struct.pack("<H", clamped))

    @classmethod
    def make_draw_mode_cmd(cls, draw_mode: int = DRAW_MODE_TEXT) -> bytes:
        """Build `0xBE` draw mode command (`0x00` photo, `0x01` text, `0x02` gray, `0x03` deep)."""
        clamped = max(0, min(3, int(draw_mode)))
        return cls.make_cmd(cls.CMD_DRAW_MODE, bytes([clamped]))

    @classmethod
    def make_speed_cmd(cls, speed: int = 28) -> bytes:
        """Build `0xBD` paper feed speed command (`8..60`; higher value = slower/darker)."""
        clamped = max(8, min(60, int(speed)))
        return cls.make_cmd(cls.CMD_SPEED, bytes([clamped]))

    @classmethod
    def make_lattice_start_cmd(cls) -> bytes:
        """Build `0xA6` lattice print-start sequence."""
        return cls.make_cmd(cls.CMD_LATTICE, cls.LATTICE_START)

    @classmethod
    def make_lattice_end_cmd(cls) -> bytes:
        """Build `0xA6` lattice print-end sequence."""
        return cls.make_cmd(cls.CMD_LATTICE, cls.LATTICE_END)

    @classmethod
    def make_print_row_cmd(cls, row_bytes: bytes) -> bytes:
        """Build `0xA2` single-row 48-byte bitmap packet."""
        if len(row_bytes) != BYTES_PER_ROW:
            raise ValueError(f"Expected {BYTES_PER_ROW} bytes per row, got {len(row_bytes)}")
        return cls.make_cmd(cls.CMD_PRINT_ROW, bytes(row_bytes))

    @classmethod
    def make_feed_cmd(cls, lines: int = 72) -> bytes:
        """Build `0xA1` paper feed command (`<H`, clamped 1..500)."""
        clamped = max(1, min(500, int(lines)))
        return cls.make_cmd(cls.CMD_FEED, struct.pack("<H", clamped))

    @classmethod
    def make_retract_cmd(cls, lines: int) -> bytes:
        """Build `0xA0` paper retract command (`<H`, clamped 1..500)."""
        clamped = max(1, min(500, int(lines)))
        return cls.make_cmd(cls.CMD_RETRACT, struct.pack("<H", clamped))

    def is_flow_pause(self, data: bytes) -> bool:
        """Detect `0x51 0x78 0xAE ... 0x10 ...` flow pause notification on AE02."""
        return len(data) >= 9 and data[0:3] == b"\x51\x78\xae" and data[6] == 0x10

    def is_flow_resume(self, data: bytes) -> bool:
        """Detect `0x51 0x78 0xAE ... 0x00 ...` flow resume notification on AE02."""
        return len(data) >= 9 and data[0:3] == b"\x51\x78\xae" and data[6] == 0x00

    def build_print_steps(
        self,
        image: Union[np.ndarray, Image.Image],
        *,
        energy: int = 22000,
        speed: int = 28,
        blackening: int = BLACKENING_DARK,
        draw_mode: int = DRAW_MODE_TEXT,
        feed_lines: int = 72,
        mtu_payload: int = 180,
    ) -> list[PrintWriteStep]:
        """Compile Classic CatPrinter GATT write steps over AE01."""
        packed_rows = pack_mono_rows(image, bitorder="little")
        row_count = int(packed_rows.shape[0])

        setup_bytes = (
            self.make_ping_cmd()
            + self.make_blackening_cmd(blackening)
            + self.make_energy_cmd(energy)
            + self.make_draw_mode_cmd(draw_mode)
            + self.make_speed_cmd(speed)
            + self.make_lattice_start_cmd()
        )
        steps: list[PrintWriteStep] = [
            PrintWriteStep(
                char_uuid=self.control_char_uuid,
                data=setup_bytes,
                rows_represented=0,
                delay_s=0.02,
                label="classic_setup",
            )
        ]

        # Each 0xA2 row packet is 56 bytes (6B header + 48B bitmap + 2B trailer).
        row_pkt_len = 8 + BYTES_PER_ROW
        rows_per_chunk = max(1, min(8, max(row_pkt_len, int(mtu_payload)) // row_pkt_len))
        # Pacing scales gently with thermal energy/speed setting (~10-14ms per row).
        per_row_delay_s = max(0.008, min(0.018, 0.009 + (max(0, int(speed) - 20) * 0.0002)))

        row_header = bytes([0x51, 0x78, self.CMD_PRINT_ROW, 0x00, BYTES_PER_ROW, 0x00])
        buf = bytearray()
        chunk_rows = 0
        for row_idx in range(row_count):
            row_bytes = packed_rows[row_idx].tobytes()
            buf += row_header
            buf += row_bytes
            buf.append(crc8(row_bytes))
            buf.append(0xFF)
            chunk_rows += 1
            if chunk_rows >= rows_per_chunk or row_idx == row_count - 1:
                steps.append(
                    PrintWriteStep(
                        char_uuid=self.control_char_uuid,
                        data=bytes(buf),
                        rows_represented=chunk_rows,
                        delay_s=per_row_delay_s * chunk_rows,
                        label="classic_rows",
                    )
                )
                buf.clear()
                chunk_rows = 0

        tail = bytearray(self.make_lattice_end_cmd())
        if feed_lines > 0:
            tail += self.make_feed_cmd(feed_lines)
        steps.append(
            PrintWriteStep(
                char_uuid=self.control_char_uuid,
                data=bytes(tail),
                rows_represented=0,
                delay_s=0.05,
                label="classic_finish",
            )
        )
        return steps

    def build_print_payload(
        self,
        image: Union[np.ndarray, Image.Image],
        *,
        energy: int = 22000,
        speed: int = 28,
        blackening: int = BLACKENING_DARK,
        draw_mode: int = DRAW_MODE_TEXT,
        feed_lines: int = 72,
    ) -> bytes:
        """Build the concatenated Classic CatPrinter binary payload for offline verification."""
        steps = self.build_print_steps(
            image,
            energy=energy,
            speed=speed,
            blackening=blackening,
            draw_mode=draw_mode,
            feed_lines=feed_lines,
            mtu_payload=448,
        )
        return b"".join(step.data for step in steps)


class MXW01Protocol(ThermalPrinterProtocol):
    """
    MXW01 thermal printer BLE protocol (`0x22 0x21` framing + AE03 raw stream).

    - Service: `0000ae30-0000-1000-8000-00805f9b34fb`
    - Control Char (`AE01`): `0000ae01-...` (commands `0xA1`, `0xA2`, `0xA9`, `0xAD`)
    - Notify Char (`AE02`, optional): `0000ae02-...`
    - Data Char (`AE03`): `0000ae03-...` (bulk raw 48-byte 1-bit monochrome rows)
    """

    protocol_id = "mxw01"
    display_name = "MXW01 (0x22 0x21 Control + AE03 Stream)"
    service_uuid = SERVICE_UUID
    control_char_uuid = CHAR_WRITE_UUID
    notify_char_uuid = CHAR_NOTIFY_UUID
    data_char_uuid = CHAR_DATA_UUID
    frame_magic = b"\x22\x21"

    CMD_STATUS = 0xA1
    CMD_SET_INTENSITY = 0xA2
    CMD_PRINT_REQUEST = 0xA9
    CMD_FLUSH = 0xAD

    INTENSITY_MIN = 0x30
    INTENSITY_MAX = 0xFF
    ENERGY_MIN = 5000
    ENERGY_MAX = 45000
    DEFAULT_PRINT_MODE = 0x0030  # Uncompressed 1-bit monochrome stream mode (<H)

    @classmethod
    def make_cmd(cls, cmd: int, payload: bytes = b"") -> bytes:
        """Wrap command byte and payload in a `22 21 ... CRC 0xFF` MXW01 packet."""
        return cls.frame_packet(cmd, payload)

    @classmethod
    def make_status_cmd(cls) -> bytes:
        """Build `0xA1` MXW01 status query packet on AE01."""
        return cls.make_cmd(cls.CMD_STATUS, b"\x00")

    @classmethod
    def make_ping_cmd(cls) -> bytes:
        """Alias for `make_status_cmd()` fulfilling `ThermalPrinterProtocol`."""
        return cls.make_status_cmd()

    @classmethod
    def map_energy_to_intensity(cls, energy: int) -> int:
        """Map UI thermal energy slider (5000..45000) linearly to 1-byte MXW01 intensity (0x30..0xFF)."""
        clamped = max(cls.ENERGY_MIN, min(cls.ENERGY_MAX, int(energy)))
        ratio = (clamped - cls.ENERGY_MIN) / float(cls.ENERGY_MAX - cls.ENERGY_MIN)
        mapped = round(cls.INTENSITY_MIN + ratio * (cls.INTENSITY_MAX - cls.INTENSITY_MIN))
        return max(cls.INTENSITY_MIN, min(cls.INTENSITY_MAX, int(mapped)))

    @classmethod
    def make_intensity_cmd(
        cls,
        energy: int = 22000,
        *,
        intensity: Optional[int] = None,
    ) -> bytes:
        """Build `0xA2` thermal intensity command (`1 byte`, `0x30..0xFF`) on AE01."""
        if intensity is not None:
            val = max(0x01, min(0xFF, int(intensity)))
        else:
            val = cls.map_energy_to_intensity(energy)
        return cls.make_cmd(cls.CMD_SET_INTENSITY, bytes([val]))

    @classmethod
    def make_print_request_cmd(
        cls,
        line_count: int,
        mode: int = DEFAULT_PRINT_MODE,
    ) -> bytes:
        """
        Build `0xA9` MXW01 print job header/request packet on AE01.

        Payload layout (`<HH`, 4 bytes):
        - uint16 LE `line_count`: exact number of 384-px monochrome rows streamed to AE03.
        - uint16 LE `mode`: printer stream mode word (default `0x0030`, i.e. bytes `30 00`
          for uncompressed 1-bit thermal bitmap streaming).
        """
        lines = max(1, min(65535, int(line_count)))
        mode_word = int(mode) & 0xFFFF
        payload = struct.pack("<HH", lines, mode_word)
        return cls.make_cmd(cls.CMD_PRINT_REQUEST, payload)

    @classmethod
    def make_flush_cmd(cls) -> bytes:
        """Build `0xAD` MXW01 buffer flush/commit command on AE01."""
        return cls.make_cmd(cls.CMD_FLUSH, b"\x00")

    @classmethod
    def _chunk_packed_rows(
        cls,
        packed_rows: np.ndarray,
        *,
        chunk_rows: int = 4,
        mtu_payload: Optional[int] = None,
        feed_lines: int = 0,
    ) -> list[bytes]:
        """Slice already-packed (H, 48) rows into MTU-friendly AE03 byte chunks."""
        rows = packed_rows
        if feed_lines > 0:
            blank = np.zeros((int(feed_lines), BYTES_PER_ROW), dtype=np.uint8)
            rows = np.vstack((rows, blank))

        raw_stream = rows.tobytes()
        if not raw_stream:
            return []

        rows_per_chunk = max(1, int(chunk_rows))
        chunk_size = rows_per_chunk * BYTES_PER_ROW
        if mtu_payload is not None and mtu_payload > 0:
            if mtu_payload >= BYTES_PER_ROW:
                aligned = (int(mtu_payload) // BYTES_PER_ROW) * BYTES_PER_ROW
                chunk_size = min(chunk_size, aligned)
            else:
                chunk_size = int(mtu_payload)

        return [
            raw_stream[offset : offset + chunk_size]
            for offset in range(0, len(raw_stream), chunk_size)
        ]

    @classmethod
    def build_data_chunks(
        cls,
        image: Union[np.ndarray, Image.Image],
        *,
        chunk_rows: int = 4,
        mtu_payload: Optional[int] = None,
        feed_lines: int = 0,
    ) -> list[bytes]:
        """
        Pack monochrome rows (48 bytes/row, LSB-first) into MTU-friendly AE03 chunks.

        Defaults to 4 rows = 192 bytes per chunk (or clamped to `mtu_payload` aligned
        to 48-byte row boundaries when `mtu_payload >= 48`).
        """
        packed_rows = pack_mono_rows(image, bitorder="little")
        return cls._chunk_packed_rows(
            packed_rows,
            chunk_rows=chunk_rows,
            mtu_payload=mtu_payload,
            feed_lines=feed_lines,
        )

    def build_print_steps(
        self,
        image: Union[np.ndarray, Image.Image],
        *,
        energy: int = 22000,
        speed: int = 28,
        blackening: int = 0x35,
        draw_mode: int = 0x01,
        feed_lines: int = 72,
        mtu_payload: int = 192,
    ) -> list[PrintWriteStep]:
        """Compile MXW01 AE01 control + AE03 bulk bitmap stream steps."""
        packed_img = pack_mono_rows(image, bitorder="little")
        img_rows = int(packed_img.shape[0])
        extra_feed = max(0, int(feed_lines))
        total_lines = img_rows + extra_feed
        if total_lines < 1:
            raise ValueError("Cannot build MXW01 print job with 0 lines")

        # Adjust thermal intensity subtly if blackening light/normal requested
        effective_energy = int(energy)
        if blackening <= 0x32:
            effective_energy = max(self.ENERGY_MIN, int(effective_energy * 0.85))

        steps: list[PrintWriteStep] = [
            PrintWriteStep(
                char_uuid=self.control_char_uuid,
                data=self.make_status_cmd(),
                rows_represented=0,
                delay_s=0.04,
                label="mxw01_status",
            ),
            PrintWriteStep(
                char_uuid=self.control_char_uuid,
                data=self.make_intensity_cmd(effective_energy),
                rows_represented=0,
                delay_s=0.03,
                label="mxw01_intensity",
            ),
            PrintWriteStep(
                char_uuid=self.control_char_uuid,
                data=self.make_print_request_cmd(total_lines, mode=self.DEFAULT_PRINT_MODE),
                rows_represented=0,
                delay_s=0.06,
                label="mxw01_print_request",
            ),
        ]

        # Build bulk chunks aligned to 48-byte rows (default 192 B = 4 rows/write)
        effective_mtu = max(BYTES_PER_ROW, int(mtu_payload))
        rows_per_chunk = max(1, min(4, effective_mtu // BYTES_PER_ROW))
        chunks = self._chunk_packed_rows(
            packed_img,
            chunk_rows=rows_per_chunk,
            mtu_payload=effective_mtu,
            feed_lines=extra_feed,
        )
        # ~10-12ms per row pacing (11ms baseline, adjusted softly with speed)
        per_row_delay_s = max(0.010, min(0.014, 0.010 + max(0, int(speed) - 20) * 0.0001))

        accumulated_img_rows = 0
        for chunk in chunks:
            chunk_row_count = max(1, round(len(chunk) / BYTES_PER_ROW))
            img_credit = min(chunk_row_count, max(0, img_rows - accumulated_img_rows))
            accumulated_img_rows += img_credit
            steps.append(
                PrintWriteStep(
                    char_uuid=self.data_char_uuid or CHAR_DATA_UUID,
                    data=chunk,
                    rows_represented=img_credit,
                    delay_s=per_row_delay_s * chunk_row_count,
                    label="mxw01_data_stream",
                )
            )

        steps.append(
            PrintWriteStep(
                char_uuid=self.control_char_uuid,
                data=self.make_flush_cmd(),
                rows_represented=0,
                delay_s=0.10,
                label="mxw01_flush",
            )
        )
        return steps

    def build_print_payload(
        self,
        image: Union[np.ndarray, Image.Image],
        *,
        energy: int = 22000,
        speed: int = 28,
        blackening: int = 0x35,
        draw_mode: int = 0x01,
        feed_lines: int = 72,
    ) -> bytes:
        """Build concatenated AE01 + AE03 binary stream for offline verification."""
        steps = self.build_print_steps(
            image,
            energy=energy,
            speed=speed,
            blackening=blackening,
            draw_mode=draw_mode,
            feed_lines=feed_lines,
            mtu_payload=192,
        )
        return b"".join(step.data for step in steps)


def create_protocol(
    protocol: str = "auto",
    device_name: str = "",
    char_uuids: Iterable[str] = (),
) -> ThermalPrinterProtocol:
    """Instantiate the requested or auto-detected thermal printer protocol."""
    choice = (protocol or "auto").strip().lower()
    if choice in ("auto", ""):
        choice = detect_protocol(device_name, char_uuids)
    if choice == "mxw01":
        return MXW01Protocol()
    if choice in ("classic", "x5", "cat", "catprinter"):
        return ClassicCatPrinterProtocol()
    raise ValueError(f"Unsupported printer protocol: {protocol!r}")


async def scan_ble_printers(
    timeout: float = 5.0,
    include_all: bool = False,
) -> list[BlePrinterDevice]:
    """
    Scan for nearby BLE thermal printers using `bleak.BleakScanner`.

    Matches known thermal printer name prefixes (`X5`, `GB`, `GT`, `MX`, `MXW`,
    `YT`, `PD`, `SC`, `_ZZ`) or devices advertising service `0000ae30-...`,
    unless `include_all=True`.
    """
    if not HAS_BLEAK or BleakScanner is None:
        raise PrinterDriverError("Thư viện bleak chưa được cài đặt.")

    discovered = await BleakScanner.discover(timeout=float(timeout), return_adv=True)
    devices: list[BlePrinterDevice] = []
    seen_addrs: set[str] = set()

    if isinstance(discovered, dict):
        items = list(discovered.values())
    else:
        items = [(dev, None) for dev in discovered]

    for entry in items:
        if isinstance(entry, tuple) and len(entry) == 2:
            dev, adv = entry
        else:
            dev, adv = entry, None

        addr = str(getattr(dev, "address", "") or "")
        if not addr or addr in seen_addrs:
            continue
        name = str(getattr(dev, "name", "") or (getattr(adv, "local_name", "") if adv else "") or "").strip()
        svc_uuids = tuple(
            str(u).lower() for u in (getattr(adv, "service_uuids", None) or ())
        )
        rssi = getattr(adv, "rssi", None) if adv is not None else getattr(dev, "rssi", None)

        upper = name.upper()
        matches_name = any(upper.startswith(pfx) for pfx in KNOWN_PRINTER_PREFIXES)
        matches_svc = any(SERVICE_UUID in u or "ae30" in u for u in svc_uuids)
        if not include_all and not (matches_name or matches_svc):
            continue

        seen_addrs.add(addr)
        devices.append(
            BlePrinterDevice(
                name=name or addr,
                address=addr,
                rssi=int(rssi) if rssi is not None else None,
                protocol_hint=detect_protocol(name),
                service_uuids=svc_uuids,
            )
        )

    devices.sort(key=lambda d: (-(d.rssi if d.rssi is not None else -999), d.name.upper()))
    return devices


def _extract_char_uuids(client: Any) -> list[str]:
    """Collect lowercase characteristic UUIDs from a connected BleakClient."""
    services = getattr(client, "services", None)
    if services is None:
        return []
    uuids: list[str] = []
    try:
        for svc in services:
            for char in getattr(svc, "characteristics", ()):
                uuids.append(str(char.uuid).lower())
    except TypeError:
        pass
    return uuids


def _resolve_mtu_payload(client: Any, char_uuid: str, default_mtu: int = 180) -> int:
    """Resolve safe GATT write payload limit from negotiated MTU or characteristic info."""
    limit = default_mtu
    services = getattr(client, "services", None)
    if services is not None:
        get_char = getattr(services, "get_characteristic", None)
        if callable(get_char):
            try:
                char_obj = get_char(char_uuid)
                if char_obj is not None:
                    max_wwr = getattr(char_obj, "max_write_without_response_size", None)
                    if isinstance(max_wwr, int) and max_wwr >= 20:
                        limit = min(limit, max_wwr)
            except Exception:
                pass
    try:
        mtu_val = getattr(client, "mtu_size", None)
        if isinstance(mtu_val, int) and mtu_val >= 23:
            limit = min(limit, mtu_val - 3)
    except Exception:
        pass
    return max(20, min(244, int(limit)))


def _is_cancelled(cancel_event: Any) -> bool:
    if cancel_event is None:
        return False
    if callable(cancel_event):
        return bool(cancel_event())
    is_set = getattr(cancel_event, "is_set", None)
    return bool(is_set()) if callable(is_set) else False


class BleThermalPrinter:
    """Async bleak driver supporting Classic CatPrinter and MXW01 thermal printers."""

    def __init__(
        self,
        address: str,
        device_name: str = "",
        protocol: str = "auto",
        timeout: float = 12.0,
    ) -> None:
        self.address = str(address).strip()
        self.device_name = str(device_name or "").strip()
        self.protocol_name = (protocol or "auto").strip().lower()
        self.timeout = float(timeout)

    async def print_ribbon(
        self,
        ribbon_img: Union[np.ndarray, Image.Image],
        *,
        energy: int = 22000,
        speed: int = 28,
        blackening: int = ClassicCatPrinterProtocol.BLACKENING_DARK,
        draw_mode: int = ClassicCatPrinterProtocol.DRAW_MODE_TEXT,
        feed_lines: int = 72,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        cancel_event: Optional[Union[threading.Event, asyncio.Event, Callable[[], bool]]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
        client_factory: Optional[Callable[..., Any]] = None,
        thermal_cooldown_rows: int = 96,
        thermal_cooldown_s: float = 0.25,
    ) -> PrintResult:
        """
        Connect via BLE, auto-detect protocol (if `auto`), stream all thermal rows
        with AE02 flow-control support, and report progress `(sent_rows, total_rows)`.
        """
        if not self.address:
            raise PrinterDriverError("Chưa chọn địa chỉ MAC/UUID máy in BLE.")
        if client_factory is None and (not HAS_BLEAK or BleakClient is None):
            raise PrinterDriverError("Thư viện bleak chưa được cài đặt.")

        gray = ribbon_to_grayscale_array(ribbon_img)
        total_rows = int(gray.shape[0])
        if total_rows <= 0:
            raise ValueError("Ribbon image has 0 rows")

        factory = client_factory or BleakClient
        t0 = time.perf_counter()
        last_notify_hex = ""
        last_firmware = ""
        bytes_sent = 0
        sent_rows = 0

        if status_callback:
            status_callback(f"Đang kết nối BLE tới {self.device_name or self.address}...")

        async with factory(self.address, timeout=self.timeout) as client:
            if _is_cancelled(cancel_event):
                raise PrintCancelledError("Đã hủy lệnh in trước khi truyền dữ liệu.")

            char_uuids = _extract_char_uuids(client)
            proto = create_protocol(self.protocol_name, self.device_name, char_uuids)
            data_char = proto.data_char_uuid or proto.control_char_uuid
            mtu_payload = _resolve_mtu_payload(client, data_char, default_mtu=192)

            if status_callback:
                status_callback(
                    f"Đã kết nối ({proto.display_name}, MTU payload={mtu_payload}B). Đang in {total_rows} dòng..."
                )

            resume_event = asyncio.Event()
            resume_event.set()
            notify_started = False

            def _on_notify(_char: Any, raw_data: bytearray) -> None:
                nonlocal last_notify_hex, last_firmware
                payload = bytes(raw_data)
                last_notify_hex = payload.hex()
                parsed = proto.parse_notification(payload)
                if parsed.get("firmware"):
                    last_firmware = str(parsed["firmware"])
                flow = parsed.get("flow")
                if flow == "pause":
                    resume_event.clear()
                elif flow == "resume":
                    resume_event.set()

            if proto.notify_char_uuid and (
                not char_uuids or proto.notify_char_uuid in char_uuids
            ):
                try:
                    await client.start_notify(proto.notify_char_uuid, _on_notify)
                    notify_started = True
                except Exception:
                    notify_started = False

            try:
                steps = proto.build_print_steps(
                    gray,
                    energy=energy,
                    speed=speed,
                    blackening=blackening,
                    draw_mode=draw_mode,
                    feed_lines=feed_lines,
                    mtu_payload=mtu_payload,
                )
                if progress_callback:
                    progress_callback(0, total_rows)

                for step in steps:
                    step_limit = _resolve_mtu_payload(client, step.char_uuid, default_mtu=mtu_payload)
                    view = memoryview(step.data)
                    for offset in range(0, len(view), step_limit):
                        while not resume_event.is_set():
                            if _is_cancelled(cancel_event):
                                raise PrintCancelledError("Đã hủy lệnh in bởi người dùng.")
                            try:
                                await asyncio.wait_for(resume_event.wait(), timeout=0.1)
                            except asyncio.TimeoutError:
                                continue

                        if _is_cancelled(cancel_event):
                            raise PrintCancelledError("Đã hủy lệnh in bởi người dùng.")

                        chunk = bytes(view[offset : offset + step_limit])
                        await client.write_gatt_char(
                            step.char_uuid,
                            chunk,
                            response=step.write_with_response,
                        )
                        bytes_sent += len(chunk)

                    if step.rows_represented > 0:
                        prev_band = sent_rows // max(1, int(thermal_cooldown_rows)) if thermal_cooldown_rows > 0 else 0
                        sent_rows = min(total_rows, sent_rows + step.rows_represented)
                        if progress_callback:
                            progress_callback(sent_rows, total_rows)
                        if (
                            thermal_cooldown_rows > 0
                            and thermal_cooldown_s > 0
                            and sent_rows < total_rows
                            and (sent_rows // int(thermal_cooldown_rows)) > prev_band
                        ):
                            await asyncio.sleep(float(thermal_cooldown_s))

                    if step.delay_s > 0:
                        await asyncio.sleep(step.delay_s)

                await asyncio.sleep(0.18)
                if progress_callback and sent_rows < total_rows:
                    progress_callback(total_rows, total_rows)
            finally:
                if notify_started and proto.notify_char_uuid:
                    try:
                        await client.stop_notify(proto.notify_char_uuid)
                    except Exception:
                        pass

        elapsed_s = max(0.001, time.perf_counter() - t0)
        return PrintResult(
            ok=True,
            protocol=proto.protocol_id,
            total_rows=total_rows,
            bytes_sent=bytes_sent,
            elapsed_s=elapsed_s,
            last_notify_hex=last_notify_hex,
            firmware=last_firmware,
        )

    async def print_image(self, image: Union[np.ndarray, Image.Image], **kwargs: Any) -> PrintResult:
        """Convenience alias for `print_ribbon()`."""
        return await self.print_ribbon(image, **kwargs)

    def print_ribbon_sync(
        self,
        ribbon_img: Union[np.ndarray, Image.Image],
        *,
        energy: int = 20000,
        speed: int = 30,
        blackening: int = ClassicCatPrinterProtocol.BLACKENING_DARK,
        draw_mode: int = ClassicCatPrinterProtocol.DRAW_MODE_TEXT,
        feed_lines: int = 72,
        band_rows: int = 64,
        band_cooldown_s: float = 0.28,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        cancel_event: Optional[Union[threading.Event, Callable[[], bool]]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
        **kwargs: Any,
    ) -> PrintResult:
        """
        Synchronous wrapper for print_ribbon() designed to be executed inside
        background worker threads.
        """
        return asyncio.run(
            self.print_ribbon(
                ribbon_img,
                energy=energy,
                speed=speed,
                blackening=blackening,
                draw_mode=draw_mode,
                feed_lines=feed_lines,
                thermal_cooldown_rows=band_rows,
                thermal_cooldown_s=band_cooldown_s,
                progress_callback=progress_callback,
                cancel_event=cancel_event,
                status_callback=status_callback,
                **kwargs,
            )
        )

    def print_image_sync(self, image: Union[np.ndarray, Image.Image], **kwargs: Any) -> PrintResult:
        """Convenience alias for synchronous `print_ribbon_sync()`."""
        return self.print_ribbon_sync(image, **kwargs)


DEFAULT_BRIDGE_URL = os.environ.get("TINYPRINT_BRIDGE_URL", "http://127.0.0.1:9100").rstrip("/")
_ADB_EXE = os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe")
_FLOW_PAUSE_HEX = "5178ae0101001070ff"


class HttpBridgePrinter:
    """
    HTTP BLE Bridge client compatible with TinyPrintBridge (`http://127.0.0.1:9100`).

    Streams Classic CatPrinter / TinyPrint X5 print rows gradually in paced thermal
    bands (with short head-cooldown pauses) so the 203 DPI thermal head stays crisp
    and cool without dropping packets.
    """

    def __init__(
        self,
        bridge_url: str = DEFAULT_BRIDGE_URL,
        *,
        chunk_size: int = 120,
        delay_ms: int = 12,
        auto_adb: bool = True,
    ) -> None:
        self.bridge_url = (bridge_url or DEFAULT_BRIDGE_URL).rstrip("/")
        self.chunk_size = int(chunk_size)
        self.delay_ms = int(delay_ms)
        self.auto_adb = bool(auto_adb)

    def _is_local_bridge(self) -> bool:
        parsed = urllib.parse.urlparse(self.bridge_url)
        host = (parsed.hostname or "").lower()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return host in ("127.0.0.1", "localhost") and port == 9100

    def _run_adb(self, args: list[str], timeout: float = 6.0) -> None:
        adb = _ADB_EXE if os.path.exists(_ADB_EXE) else "adb"
        try:
            subprocess.run(
                [adb, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, "ADB_LIBUSB": "0"},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass

    def _http_req(
        self,
        method: str,
        path: str,
        body: Optional[bytes] = None,
        timeout: float = 20.0,
    ) -> Any:
        url = self.bridge_url + path
        req = urllib.request.Request(url, data=body, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/octet-stream")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw)
        except urllib.error.URLError as exc:
            raise PrinterDriverError(f"Không kết nối được cầu nối Android tại {self.bridge_url}: {exc}") from exc

    def get_status(self, auto_forward: bool = True) -> dict[str, Any]:
        """Query bridge `/status`, running `adb forward tcp:9100 tcp:9100` once if local."""
        try:
            res = self._http_req("GET", "/status", timeout=2.5)
            if isinstance(res, dict):
                return res
        except Exception:
            if not (auto_forward and self.auto_adb and self._is_local_bridge()):
                raise
        self._run_adb(["forward", "tcp:9100", "tcp:9100"])
        res = self._http_req("GET", "/status", timeout=3.0)
        if not isinstance(res, dict):
            raise PrinterDriverError("Phản hồi /status từ cầu nối không hợp lệ.")
        return res

    def _restore_return_activity(self) -> None:
        return_activity = os.environ.get("PAPER_VOID_RETURN_ACTIVITY", "").strip()
        if return_activity and self.auto_adb and self._is_local_bridge():
            self._run_adb(["shell", "am", "start", "-n", return_activity])

    def _bootstrap_local_bridge(self) -> Optional[dict[str, Any]]:
        self._run_adb(["forward", "tcp:9100", "tcp:9100"])
        self._run_adb(["shell", "am", "start-foreground-service", "-n", "com.tinyprint.bridge/.BleBridgeService"])
        for _ in range(4):
            time.sleep(0.35)
            self._run_adb(["forward", "tcp:9100", "tcp:9100"])
            try:
                res = self._http_req("GET", "/status", timeout=2.0)
                if isinstance(res, dict):
                    return res
            except Exception:
                continue

        self._run_adb(["shell", "am", "start", "-n", "com.tinyprint.bridge/.BridgeActivity"])
        for _ in range(8):
            time.sleep(0.45)
            self._run_adb(["forward", "tcp:9100", "tcp:9100"])
            try:
                res = self._http_req("GET", "/status", timeout=2.0)
                if isinstance(res, dict):
                    self._restore_return_activity()
                    return res
            except Exception:
                continue
        return None

    def launch_and_connect(
        self,
        printer_name: str = "X5",
        mac: Optional[str] = None,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """
        One-click Android phone bridge bootstrap:
        1. Forward tcp:9100 via ADB (when local)
        2. Launch TinyPrintBridge app on the phone if not already running
        3. Connect the phone app to the X5 thermal printer if not connected
        4. Verify 2-way link with a 0-paper ping (0xA8)
        """
        status: Optional[dict[str, Any]] = None
        if status_callback:
            status_callback("Đang kiểm tra cầu nối điện thoại…")
        try:
            status = self.get_status(auto_forward=True)
        except Exception:
            if self.auto_adb and self._is_local_bridge():
                if status_callback:
                    status_callback("Đang bật app TinyPrintBridge trên điện thoại qua ADB…")
                status = self._bootstrap_local_bridge()
        if status is None:
            raise PrinterDriverError(
                f"Không thể bật/nối tới app TinyPrintBridge tại {self.bridge_url}. "
                "Hãy kiểm tra cáp USB (hoặc Wireless ADB) và USB Debugging trên điện thoại."
            )

        if not (status.get("connected") and status.get("writeUuid")):
            if status_callback:
                status_callback(f"App ĐT đã mở — đang kết nối tới máy in {printer_name}…")
            params: dict[str, str] = {}
            if mac:
                params["mac"] = mac
            if printer_name:
                params["name"] = printer_name
            qs = ("?" + urllib.parse.urlencode(params)) if params else ""
            res = self._http_req("POST", "/connect" + qs, body=b"", timeout=18.0)
            if not isinstance(res, dict) or not res.get("connected"):
                err = res.get("lastError") if isinstance(res, dict) else res
                raise PrinterDriverError(
                    f"App điện thoại chưa nối được máy in {printer_name}: {err or 'Hãy bật nguồn máy in X5.'}"
                )
            status = res

        # 0-paper ping to verify live 2-way printer communication
        try:
            ping_pkt = ClassicCatPrinterProtocol.make_ping_cmd()
            self._http_req("POST", f"/write?chunk={self.chunk_size}&delay={self.delay_ms}", body=ping_pkt, timeout=8.0)
        except Exception:
            pass
        return status

    def _wake_phone_over_wifi(self, host: str) -> bool:
        """Attempt to wake up Android app remotely via wireless ADB (port 5555)."""
        if not self.auto_adb or not host:
            return False
        try:
            self._run_adb(["connect", f"{host}:5555"])
            self._run_adb(["-s", f"{host}:5555", "shell", "am", "start-foreground-service", "-n", "com.tinyprint.bridge/.BleBridgeService"])
            self._run_adb(["-s", f"{host}:5555", "shell", "am", "start", "-n", "com.tinyprint.bridge/.BridgeActivity"])
            return True
        except Exception:
            return False

    def ensure_service_ready(self, status_callback: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
        """Ensure the phone HTTP bridge is reachable (via USB ADB or Tailscale IP) without requiring X5 BLE."""
        try:
            return self.get_status(auto_forward=True)
        except Exception:
            if self.auto_adb and self._is_local_bridge():
                if status_callback:
                    status_callback("Đang bật dịch vụ trên điện thoại qua cáp USB (ADB)…")
                res = self._bootstrap_local_bridge()
                if res is not None:
                    return res
            elif self.auto_adb and not self._is_local_bridge():
                parsed = urllib.parse.urlparse(self.bridge_url)
                host = parsed.hostname or ""
                if host:
                    if status_callback:
                        status_callback(f"Đang gửi lệnh tự bật app từ xa tới {host}…")
                    self._wake_phone_over_wifi(host)
                    for _ in range(5):
                        time.sleep(0.4)
                        try:
                            res = self._http_req("GET", "/status", timeout=2.0)
                            if isinstance(res, dict):
                                return res
                        except Exception:
                            continue
            raise PrinterDriverError(
                f"Không kết nối được điện thoại tại {self.bridge_url}. "
                "Nếu dùng không dây, hãy chạm vào app TinyPrintBridge trên điện thoại một lần (hoặc cắm cáp USB để máy tính tự động bật)."
            )

    def start_phone_scanner(self, status_callback: Optional[Callable[[str], None]] = None) -> None:
        """Open the dedicated full-screen ScannerActivity on the Android phone (USB ADB or Tailscale)."""
        self.ensure_service_ready(status_callback=status_callback)
        if status_callback:
            status_callback("Đang mở màn hình quét mã trên điện thoại…")
        try:
            self._http_req("POST", "/scanner/start", body=b"", timeout=5.0)
        except Exception:
            pass
        # On FlymeOS/Android 12+, background startActivity may be throttled; also trigger via ADB
        if self.auto_adb and self._is_local_bridge():
            self._run_adb(["shell", "am", "start", "-n", "com.tinyprint.bridge/.ScannerActivity"])
        elif self.auto_adb and not self._is_local_bridge():
            parsed = urllib.parse.urlparse(self.bridge_url)
            host = parsed.hostname or ""
            if host:
                self._run_adb(["-s", f"{host}:5555", "shell", "am", "start", "-n", "com.tinyprint.bridge/.ScannerActivity"])

    def fetch_scanner_frame(self, timeout: float = 3.0) -> tuple[bytes, int, bool]:
        """Fetch latest JPEG frame from phone ScannerActivity (`GET /scanner/frame`)."""
        url = self.bridge_url + "/scanner/frame"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                seq = int(resp.headers.get("X-Frame-Seq", "0") or "0")
                active = (resp.headers.get("X-Scanner-Active", "1") or "1") == "1"
                if resp.status == 204:
                    return b"", seq, active
                return resp.read(), seq, active
        except urllib.error.URLError as exc:
            raise PrinterDriverError(f"Mất kết nối tới điện thoại: {exc}") from exc

    def ack_phone_scanner(self, vault_id_hex: str = "") -> None:
        """Trigger green zoom-snap + '✔ ĐÃ NHẬN MÃ THẺ' animation on the phone screen."""
        qs = "?" + urllib.parse.urlencode({"vault_id": vault_id_hex}) if vault_id_hex else ""
        try:
            self._http_req("POST", "/scanner/ack" + qs, body=b"", timeout=4.0)
        except Exception:
            pass
        finally:
            self._restore_return_activity()

    def stop_phone_scanner(self) -> None:
        """Close ScannerActivity on the phone when cancelled from PC."""
        try:
            self._http_req("POST", "/scanner/stop", body=b"", timeout=3.0)
        except Exception:
            pass
        finally:
            self._restore_return_activity()

    def shutdown_phone_bridge(self) -> None:
        """Cleanly stop TinyPrintBridge on the Android phone (works over USB ADB & Tailscale)."""
        try:
            self._http_req("POST", "/shutdown", body=b"", timeout=3.5)
        except Exception:
            pass
        finally:
            if self.auto_adb and self._is_local_bridge():
                time.sleep(0.15)
                self._run_adb(["shell", "am", "force-stop", "com.tinyprint.bridge"])
            self._restore_return_activity()

    def _wait_if_flow_paused(self, last_notify_hex: str, cancel_event: Any) -> None:
        if (last_notify_hex or "").lower() != _FLOW_PAUSE_HEX:
            return
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if _is_cancelled(cancel_event):
                raise PrintCancelledError("Đã hủy lệnh in qua cầu nối.")
            time.sleep(0.2)
            try:
                st = self._http_req("GET", "/status", timeout=2.0)
                if isinstance(st, dict) and str(st.get("lastNotifyHex") or "").lower() != _FLOW_PAUSE_HEX:
                    return
            except Exception:
                return

    def print_ribbon_sync(
        self,
        ribbon_img: Union[np.ndarray, Image.Image],
        *,
        energy: int = 20000,
        speed: int = 30,
        blackening: int = ClassicCatPrinterProtocol.BLACKENING_DARK,
        draw_mode: int = ClassicCatPrinterProtocol.DRAW_MODE_TEXT,
        feed_lines: int = 72,
        band_rows: int = 64,
        band_cooldown_s: float = 0.28,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        cancel_event: Optional[Union[threading.Event, Callable[[], bool]]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> PrintResult:
        """
        Send a 384-px ribbon to TinyPrint X5 via the Android HTTP bridge in gradual
        thermal bands (default 64 rows = ~0.8 cm per batch + 0.28s head cooldown).
        """
        gray = ribbon_to_grayscale_array(ribbon_img)
        total_rows = int(gray.shape[0])
        if total_rows <= 0:
            raise ValueError("Ribbon image has 0 rows")

        t0 = time.perf_counter()
        if status_callback:
            status_callback(f"Đang kiểm tra cầu nối {self.bridge_url}...")

        status = self.get_status(auto_forward=True)
        if not status.get("connected"):
            raise PrinterDriverError(
                "Cầu nối điện thoại đang mở nhưng chưa kết nối máy in X5. "
                "Hãy bấm Connect trong app TinyPrintBridge trên điện thoại."
            )

        proto = ClassicCatPrinterProtocol()
        packed_rows = pack_mono_rows(gray, bitorder="little")

        if status_callback:
            name = str(status.get("name") or "X5")
            status_callback(f"Đang in từ từ qua cầu nối → {name} ({total_rows} dòng)...")
        if progress_callback:
            progress_callback(0, total_rows)

        def _post_batch(raw_bytes: bytes) -> dict[str, Any]:
            if _is_cancelled(cancel_event):
                raise PrintCancelledError("Đã hủy lệnh in bởi người dùng.")
            res = self._http_req(
                "POST",
                f"/write?chunk={self.chunk_size}&delay={self.delay_ms}",
                body=raw_bytes,
                timeout=45.0,
            )
            if not isinstance(res, dict) or not res.get("ok"):
                err = res.get("error") if isinstance(res, dict) else res
                raise PrinterDriverError(f"Ghi dữ liệu qua cầu nối thất bại: {err}")
            self._wait_if_flow_paused(str(res.get("lastNotifyHex") or ""), cancel_event)
            return res

        bytes_sent = 0
        last_notify = ""

        # 1. Setup packet
        setup = (
            proto.make_ping_cmd()
            + proto.make_blackening_cmd(blackening)
            + proto.make_energy_cmd(energy)
            + proto.make_draw_mode_cmd(draw_mode)
            + proto.make_speed_cmd(speed)
            + proto.make_lattice_start_cmd()
        )
        res = _post_batch(setup)
        bytes_sent += len(setup)
        last_notify = str(res.get("lastNotifyHex") or last_notify)

        # 2. Stream rows in gentle bands with thermal cooldown
        row_header = bytes([0x51, 0x78, proto.CMD_PRINT_ROW, 0x00, BYTES_PER_ROW, 0x00])
        step_rows = max(16, int(band_rows))
        sent_rows = 0

        for start_r in range(0, total_rows, step_rows):
            end_r = min(total_rows, start_r + step_rows)
            buf = bytearray()
            for r_idx in range(start_r, end_r):
                rb = packed_rows[r_idx].tobytes()
                buf += row_header
                buf += rb
                buf.append(crc8(rb))
                buf.append(0xFF)
            res = _post_batch(bytes(buf))
            bytes_sent += len(buf)
            last_notify = str(res.get("lastNotifyHex") or last_notify)
            sent_rows = end_r
            if progress_callback:
                progress_callback(sent_rows, total_rows)
            if end_r < total_rows and band_cooldown_s > 0:
                time.sleep(float(band_cooldown_s))

        # 3. End lattice + feed paper
        tail = bytearray(proto.make_lattice_end_cmd())
        if feed_lines > 0:
            tail += proto.make_feed_cmd(feed_lines)
        res = _post_batch(bytes(tail))
        bytes_sent += len(tail)
        last_notify = str(res.get("lastNotifyHex") or last_notify)

        elapsed_s = max(0.001, time.perf_counter() - t0)
        return PrintResult(
            ok=True,
            protocol="bridge_classic",
            total_rows=total_rows,
            bytes_sent=bytes_sent,
            elapsed_s=elapsed_s,
            last_notify_hex=last_notify,
            firmware=decode_notify_firmware(last_notify),
        )


def build_bridge_url(mode: str, tailscale_ip: str = "", default_port: int = 9100) -> str:
    """Resolve bridge HTTP URL from connection mode ('usb' or 'tailscale')."""
    if (mode or "usb").strip().lower() == "usb":
        return f"http://127.0.0.1:{default_port}"
    raw = (tailscale_ip or "").strip()
    if not raw:
        raise PrinterDriverError("Vui lòng nhập IP Tailscale của điện thoại (ví dụ: 100.x.y.z).")
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    parsed = urllib.parse.urlparse(raw)
    host = parsed.hostname or ""
    if not host:
        raise PrinterDriverError(f"IP Tailscale không hợp lệ: {tailscale_ip!r}")
    port = parsed.port or default_port
    return f"{parsed.scheme}://{host}:{port}"


async def probe_direct_ble(
    address: str = "",
    name_hint: str = "X5",
    timeout: float = 6.0,
    status_callback: Optional[Callable[[str], None]] = None,
) -> BlePrinterDevice:
    """
    Search and verify direct BLE connection to a thermal printer from PC.
    If address is empty or 'auto', scans for nearby printers matching name_hint/prefixes.
    """
    target_addr = (address or "").strip()
    target_name = (name_hint or "").strip()

    if not target_addr or target_addr.lower() == "auto":
        if status_callback:
            status_callback("Đang quét tìm máy in BLE quanh máy tính...")
        devices = await scan_ble_printers(timeout=timeout)
        if not devices:
            raise PrinterDriverError("Không tìm thấy máy in BLE nào quanh máy tính. Hãy bật nguồn máy in X5.")
        chosen = devices[0]
        target_addr = chosen.address
        target_name = chosen.name
    else:
        chosen = BlePrinterDevice(name=target_name or "X5", address=target_addr, protocol_hint=detect_protocol(target_name))

    if status_callback:
        status_callback(f"Đang kiểm tra kết nối Bluetooth PC tới {target_name} ({target_addr})...")

    if not HAS_BLEAK or BleakClient is None:
        raise PrinterDriverError("Thư viện bleak chưa sẵn sàng trên hệ thống.")

    async with BleakClient(target_addr, timeout=timeout) as client:
        if not client.is_connected:
            raise PrinterDriverError(f"Không thể kết nối Bluetooth tới máy in {target_addr}.")
        # Send 0-paper ping 0xA8 to verify 2-way BLE
        ping = ClassicCatPrinterProtocol.make_ping_cmd()
        char_uuids = _extract_char_uuids(client)
        write_uuid = "0000ae01-0000-1000-8000-00805f9b34fb"
        for u in char_uuids:
            if "ae01" in u:
                write_uuid = u
                break
        await client.write_gatt_char(write_uuid, ping, response=False)

    return chosen


def probe_direct_ble_sync(
    address: str = "",
    name_hint: str = "X5",
    timeout: float = 6.0,
    status_callback: Optional[Callable[[str], None]] = None,
) -> BlePrinterDevice:
    """Synchronous wrapper for probe_direct_ble."""
    return asyncio.run(
        probe_direct_ble(
            address=address,
            name_hint=name_hint,
            timeout=timeout,
            status_callback=status_callback,
        )
    )


def create_vault_printer(
    mode: str,
    *,
    tailscale_ip: str = "",
    ble_address: str = "",
    printer_name: str = "X5",
    default_port: int = 9100,
) -> Union[BleThermalPrinter, HttpBridgePrinter]:
    """
    Factory returning either a BleThermalPrinter (direct PC BLE) or an
    HttpBridgePrinter (phone bridge via USB or Tailscale). Both provide print_ribbon_sync().
    """
    m = (mode or "direct_ble").strip().lower()
    if m in ("direct_ble", "ble", "pc_bluetooth", "direct"):
        addr = (ble_address or "").strip()
        if not addr or addr.lower() == "auto":
            devs = asyncio.run(scan_ble_printers(timeout=4.0))
            if not devs:
                raise PrinterDriverError(
                    "Không tìm thấy máy in BLE nào quanh máy tính. "
                    "Hãy bật nguồn máy in X5 hoặc dùng cầu nối điện thoại."
                )
            addr = devs[0].address
            printer_name = devs[0].name or printer_name
        return BleThermalPrinter(address=addr, device_name=printer_name)
    else:
        url = build_bridge_url(m, tailscale_ip=tailscale_ip, default_port=default_port)
        return HttpBridgePrinter(bridge_url=url)


def show_ram_preview(
    ribbon_img: Union[np.ndarray, Image.Image],
    parent: Optional[tk.Misc] = None,
    *,
    title: str = "PAPER-VOID — RAM Dry-Run Preview",
    tile_count: Optional[int] = None,
    cell_size: Optional[int] = None,
    initial_zoom: float = 1.0,
    blocking: bool = False,
) -> tk.Misc:
    """
    Open a scrollable Tkinter window displaying a 384-px thermal ribbon strictly
    from RAM via `ImageTk.PhotoImage(Image.fromarray(ribbon_img, mode='L'))`.

    Includes zoom presets (1x / 1.5x / 2x) and a synchronized cm ruler.
    Never writes any image or temporary file to disk.
    """
    gray_u8 = ribbon_to_grayscale_array(ribbon_img)
    base_h, base_w = gray_u8.shape
    length_cm = base_h / PX_PER_CM

    win: tk.Misc = tk.Toplevel(parent) if parent is not None else tk.Tk()
    win.title(title)
    win.geometry("920x740")
    win.configure(bg="#11161d")

    zoom_var = tk.DoubleVar(value=float(initial_zoom) if initial_zoom in (1.0, 1.5, 2.0) else 1.0)
    # Retain PhotoImage instances on the window object so Tkinter cannot GC them.
    win._photo_refs = []  # type: ignore[attr-defined]
    win._ribbon_gray = gray_u8  # type: ignore[attr-defined]

    header = tk.Frame(win, bg="#18202a", padx=12, pady=8)
    header.pack(fill=tk.X)

    meta_parts = [
        f"RAM Buffer: {base_w} × {base_h} px",
        f"Chiều dài giấy ước tính: {length_cm:.2f} cm",
    ]
    if tile_count is not None:
        meta_parts.append(f"Số mảnh (Tiles): {tile_count}")
    if cell_size is not None:
        meta_parts.append(f"Cell: {cell_size}×{cell_size} px")

    info_lbl = tk.Label(
        header,
        text="  |  ".join(meta_parts),
        bg="#18202a",
        fg="#e2e8f0",
        font=("Segoe UI", 10, "bold"),
    )
    info_lbl.pack(side=tk.LEFT)

    ctrl_frame = tk.Frame(header, bg="#18202a")
    ctrl_frame.pack(side=tk.RIGHT)

    tk.Label(
        ctrl_frame,
        text="Zoom:",
        bg="#18202a",
        fg="#94a3b8",
        font=("Segoe UI", 9, "bold"),
    ).pack(side=tk.LEFT, padx=(0, 6))

    body = tk.Frame(win, bg="#0d1117")
    body.pack(fill=tk.BOTH, expand=True)

    v_scroll = tk.Scrollbar(body, orient=tk.VERTICAL)
    v_scroll.pack(side=tk.RIGHT, fill=tk.Y)
    h_scroll = tk.Scrollbar(body, orient=tk.HORIZONTAL)
    h_scroll.pack(side=tk.BOTTOM, fill=tk.X)

    canvas = tk.Canvas(
        body,
        bg="#0d1117",
        highlightthickness=0,
        yscrollcommand=v_scroll.set,
        xscrollcommand=h_scroll.set,
    )
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    v_scroll.configure(command=canvas.yview)
    h_scroll.configure(command=canvas.xview)

    ruler_w = 70
    pad_top = 20
    pad_left = 18
    pad_bottom = 28
    max_slice_h = 4096

    def _render_canvas(*_args: Any) -> None:
        zoom = float(zoom_var.get())
        scaled_w = max(1, round(base_w * zoom))
        scaled_h = max(1, round(base_h * zoom))

        canvas.delete("all")
        win._photo_refs.clear()  # type: ignore[attr-defined]

        if zoom == 1.0:
            disp_img = Image.fromarray(gray_u8, mode="L")
        else:
            disp_img = Image.fromarray(gray_u8, mode="L").resize(
                (scaled_w, scaled_h), Image.Resampling.NEAREST
            )

        # Ruler strip background
        total_canvas_h = pad_top + scaled_h + pad_bottom
        total_canvas_w = ruler_w + pad_left + scaled_w + 32
        canvas.create_rectangle(0, 0, ruler_w, total_canvas_h, fill="#161b22", outline="")
        canvas.create_line(ruler_w, 0, ruler_w, total_canvas_h, fill="#30363d", width=1)

        # Draw cm & 0.5 cm ticks synchronized with thermal dot height
        px_per_cm_zoom = PX_PER_CM * zoom
        max_half_cm = int(length_cm * 2.0) + 1
        for half_idx in range(max_half_cm + 1):
            cm_val = half_idx * 0.5
            y_pos = pad_top + cm_val * px_per_cm_zoom
            if y_pos > pad_top + scaled_h + 2:
                break
            is_major = (half_idx % 2) == 0
            tick_len = 16 if is_major else 8
            tick_color = "#79c0ff" if is_major else "#6e7681"
            canvas.create_line(ruler_w - tick_len, y_pos, ruler_w, y_pos, fill=tick_color, width=1)
            if is_major:
                canvas.create_text(
                    ruler_w - 20,
                    y_pos,
                    text=f"{int(round(cm_val))} cm",
                    anchor="e",
                    fill="#c9d1d9",
                    font=("Consolas", 8, "bold"),
                )

        paper_x = ruler_w + pad_left
        canvas.create_rectangle(
            paper_x - 2,
            pad_top - 2,
            paper_x + scaled_w + 2,
            pad_top + scaled_h + 2,
            fill="#ffffff",
            outline="#38bdf8",
            width=1,
        )

        # Slice into <= 4096px RAM PhotoImage bands for long ribbons
        for y0 in range(0, scaled_h, max_slice_h):
            y1 = min(scaled_h, y0 + max_slice_h)
            band = disp_img if (y0 == 0 and y1 == scaled_h) else disp_img.crop((0, y0, scaled_w, y1))
            photo = ImageTk.PhotoImage(band, master=win)
            win._photo_refs.append(photo)  # type: ignore[attr-defined]
            canvas.create_image(paper_x, pad_top + y0, anchor="nw", image=photo)

        canvas.configure(scrollregion=(0, 0, total_canvas_w, total_canvas_h))

    for factor, label in ((1.0, "1×"), (1.5, "1.5×"), (2.0, "2×")):
        tk.Radiobutton(
            ctrl_frame,
            text=label,
            variable=zoom_var,
            value=factor,
            command=_render_canvas,
            indicatoron=False,
            bg="#263140",
            fg="#e2e8f0",
            selectcolor="#0284c7",
            activebackground="#334155",
            activeforeground="#ffffff",
            padx=10,
            pady=3,
            bd=0,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        ).pack(side=tk.LEFT, padx=2)

    tk.Button(
        ctrl_frame,
        text="Đóng",
        command=win.destroy,
        bg="#334155",
        fg="#f8fafc",
        activebackground="#475569",
        relief=tk.FLAT,
        padx=10,
        pady=3,
        font=("Segoe UI", 9, "bold"),
        cursor="hand2",
    ).pack(side=tk.LEFT, padx=(10, 0))

    def _on_mousewheel(event: tk.Event) -> None:
        if getattr(event, "delta", 0):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_destroy(event: tk.Event) -> None:
        # Zeroize the preview's private RAM copy when its window closes.
        if event.widget is win:
            gray_u8.fill(0)
            win._photo_refs.clear()  # type: ignore[attr-defined]

    canvas.bind("<MouseWheel>", _on_mousewheel)
    win.bind("<Escape>", lambda _e: win.destroy())
    win.bind("<Destroy>", _on_destroy, add="+")
    _render_canvas()

    if blocking and parent is None:
        win.mainloop()
    return win
