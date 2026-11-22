"""
PAPER-VOID — Hybrid Paper-Key Vault Desktop App (ServBay-inspired Light UI).

Uses ONLY the Hybrid Paper-Key Vault method:
- Streaming AES-256-GCM (1 MB chunks, atomic .part output, safe cancellation)
- Renders exactly 1 thermal Key Card (~8.4 cm, 1 PV-RIBBON v1 tile + Base32/CRC-16)
- One-click Android Phone Bridge launch & connect (TinyPrintBridge -> TinyPrint X5)
  with gentle thermal band pacing (64 rows + 0.28s cooldown)
- Unlock from phone photo/video (.jpg/.png/.mp4/.mov) OR manual 3-line Base32 code
- Dedicated Secure Shredder (4-pass overwrite + fsync + truncate + double rename)

Run:  py -3.12 d:/project/PrintTiny/paper_void/app.py
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Optional

import numpy as np
from PIL import Image, ImageTk

import codec
import printer_ble
import vault
import i18n

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    HAS_DND = True
except Exception:
    DND_FILES = None  # type: ignore[assignment]
    TkinterDnD = None  # type: ignore[assignment]
    HAS_DND = False

APP_TITLE = "PAPER-VOID — Paper-Key Vault"
SHRED_CONFIRM_WORD = "SHRED"

# Warm Cream (#FFFFCC) + Pure White (#FFFFFF) High-Contrast Palette
BG_WINDOW = "#FFFFCC"
BG_SIDEBAR = "#F5F2B8"
BG_CARD = "#FFFFFF"
BG_CARD_HOVER = "#FFFFFF"
BG_INPUT = "#FFFFFF"
BORDER_SUBTLE = "#C7C07E"
BORDER_CARD = "#D6CF8E"

FG_PRIMARY = "#111827"
FG_SECONDARY = "#374151"
FG_MUTED = "#57534E"

ACCENT_BLUE = "#1E293B"
ACCENT_BLUE_HOVER = "#0F172A"
ACCENT_BLUE_LIGHT = "#FFFFFF"
STATUS_GREEN = "#15803D"
STATUS_GREEN_BG = "#DCFCE7"
STATUS_AMBER = "#92400E"
STATUS_AMBER_BG = "#FEF3C7"
DANGER_RED = "#B91C1C"
DANGER_RED_BG = "#FEE2E2"
TOAST_BG = "#1F2937"
BTN_DISABLED_FG = "#6B7280"


def zeroize(buf: Any) -> None:
    if buf is None:
        return
    if isinstance(buf, bytearray):
        for i in range(0, len(buf), 1 << 16):
            end = min(len(buf), i + (1 << 16))
            buf[i:end] = bytes(end - i)
    elif isinstance(buf, np.ndarray) and buf.flags.writeable:
        buf.fill(0)


def format_bytes(n: float) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} GB"


def get_asset_path(filename: str) -> str:
    base = getattr(sys, "_MEIPASS", None)
    if base and os.path.isfile(os.path.join(base, "assets", filename)):
        return os.path.join(base, "assets", filename)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", "assets", filename),
        os.path.join(here, "assets", filename),
        os.path.join(os.getcwd(), "assets", filename),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return ""


def auto_detect_phone_ip() -> str:
    try:
        ts_exe = r"C:\Program Files\Tailscale\tailscale.exe"
        if not os.path.isfile(ts_exe):
            ts_exe = "tailscale"
        out = subprocess.check_output([ts_exe, "status"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            low = line.lower()
            if "android" in low or "meizu" in low or "phone" in low:
                parts = line.split()
                if parts:
                    return parts[0]
    except Exception:
        pass
    try:
        adb_bin = printer_ble._find_adb_binary()
        if adb_bin:
            out = subprocess.check_output([adb_bin, "devices"], text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                if ":5555" in line and "device" in line:
                    return line.split(":")[0].strip()
    except Exception:
        pass
    return "100.127.161.92"


@dataclass
class VaultSessionItem:
    source_path: str
    result: vault.VaultLockResult
    printed_copies: int = 0
    shredded: bool = False


class PaperVoidApp:
    """ServBay-style macOS light-mode desktop app for PAPER-VOID Paper-Key Vault."""

    def __init__(self, root: Optional[tk.Misc] = None) -> None:
        if root is None:
            root = self._create_root()
        self.root = root
        self.lang = i18n.detect_default_language()
        self.dnd_active = bool(HAS_DND and isinstance(root, TkinterDnD.Tk)) if HAS_DND else False

        self._events: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._closing = False
        self._after_id: Optional[str] = None
        self._toast_after_id: Optional[str] = None
        self._preview_windows: list[tk.Misc] = []

        # Active background workers
        self._job_thread: Optional[threading.Thread] = None
        self._job_cancel = threading.Event()
        self._bridge_connected = False
        self._bridge_printer_name = ""

        # Page state
        self._current_page = "vault"
        self._vault_src_path: Optional[str] = None
        self._vault_items: list[VaultSessionItem] = []

        self._unlock_vault_path: Optional[str] = None
        self._unlock_hdr: Optional[vault.VaultHeaderInfo] = None
        self._unlock_scanned_key: Optional[vault.PaperKeyRecord] = None

        self._shred_queue: list[str] = []

        # Shared printer/settings variables
        self.var_conn_mode = tk.StringVar(value="direct_ble")
        self.var_unlock_conn_mode = tk.StringVar(value="usb")
        self.var_ble_mac = tk.StringVar(value="F1:8B:B4:28:3E:99")
        self.var_tailscale_ip = tk.StringVar(value=auto_detect_phone_ip())
        self.var_bridge_url = tk.StringVar(value=printer_ble.DEFAULT_BRIDGE_URL)
        self.var_printer_name = tk.StringVar(value="X5")
        self.var_energy = tk.IntVar(value=20000)
        self.var_speed = tk.IntVar(value=30)

        self._build_style()
        self._build_layout()
        self._select_page("vault")
        self._pump_events()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    @staticmethod
    def _create_root() -> tk.Tk:
        if HAS_DND:
            try:
                return TkinterDnD.Tk()
            except Exception:
                pass
        return tk.Tk()

    def _build_style(self) -> None:
        self.root.title(i18n.t("app_title", self.lang))
        self.root.geometry("1040x680")
        self.root.minsize(920, 600)
        self.root.configure(bg=BG_WINDOW)

        # Load Pair 5 desktop app icon for window title bar & taskbar
        icon_file = get_asset_path("icon_desktop.png")
        if icon_file and os.path.isfile(icon_file):
            try:
                img = Image.open(icon_file)
                self._app_icon_img = ImageTk.PhotoImage(img.resize((32, 32), Image.Resampling.LANCZOS))
                self.root.iconphoto(True, self._app_icon_img)
                self._brand_icon_img = ImageTk.PhotoImage(img.resize((28, 28), Image.Resampling.LANCZOS))
            except Exception:
                self._app_icon_img = None
                self._brand_icon_img = None
        else:
            self._app_icon_img = None
            self._brand_icon_img = None

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Horizontal.TProgressbar", troughcolor="#D6D6DB",
                        background=ACCENT_BLUE, bordercolor="#D6D6DB", thickness=6)

    # ------------------------------------------------------------------ Layout Shell

    def _build_layout(self) -> None:
        shell = tk.Frame(self.root, bg=BG_WINDOW)
        shell.pack(fill=tk.BOTH, expand=True)

        # Left Sidebar (ServBay style)
        self.sidebar = tk.Frame(shell, bg=BG_SIDEBAR, width=215)
        self.sidebar.pack(side=tk.LEFT, fill=tk.Y)
        self.sidebar.pack_propagate(False)

        # Vertical 1px divider
        tk.Frame(shell, bg=BORDER_SUBTLE, width=1).pack(side=tk.LEFT, fill=tk.Y)

        # Right Main Content Area
        self.main_wrap = tk.Frame(shell, bg=BG_WINDOW)
        self.main_wrap.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._build_sidebar()

        # Page container
        self.page_host = tk.Frame(self.main_wrap, bg=BG_WINDOW)
        self.page_host.pack(fill=tk.BOTH, expand=True, padx=24, pady=(18, 8))

        # Bottom bar: slim progress + ServBay-style floating toast pill
        bottom = tk.Frame(self.main_wrap, bg=BG_WINDOW, height=44)
        bottom.pack(fill=tk.X, padx=24, pady=(0, 14))
        bottom.pack_propagate(False)

        self.progress = ttk.Progressbar(bottom, orient=tk.HORIZONTAL, mode="determinate", maximum=100)
        self.progress.pack(fill=tk.X, pady=(0, 6))

        toast_row = tk.Frame(bottom, bg=BG_WINDOW)
        toast_row.pack(fill=tk.X)
        self.var_toast = tk.StringVar(value=i18n.t("toast_ready", self.lang))
        self.toast_lbl = tk.Label(
            toast_row,
            textvariable=self.var_toast,
            bg=TOAST_BG,
            fg="#FFFFFF",
            font=("Segoe UI", 9),
            padx=14,
            pady=4,
        )
        self.toast_lbl.pack(side=tk.LEFT)

        self.btn_cancel_job = tk.Button(
            toast_row,
            text=i18n.t("btn_cancel_job", self.lang),
            command=self.cancel_active_job,
            bg="#FFFFFF",
            fg=DANGER_RED,
            disabledforeground=BTN_DISABLED_FG,
            activebackground=DANGER_RED_BG,
            activeforeground=DANGER_RED,
            relief=tk.SOLID,
            bd=1,
            font=("Segoe UI", 9, "bold"),
            padx=10,
            pady=3,
            cursor="hand2",
            state=tk.DISABLED,
        )
        self.btn_cancel_job.pack(side=tk.RIGHT)

        # Build pages
        self.pages: dict[str, tk.Frame] = {}
        self.pages["vault"] = self._build_vault_page(self.page_host)
        self.pages["unlock"] = self._build_unlock_page(self.page_host)
        self.pages["shred"] = self._build_shred_page(self.page_host)
        self.pages["printer"] = self._build_printer_page(self.page_host)

    def switch_language(self, new_lang: str) -> None:
        if new_lang not in ("en", "vi", "zh") or new_lang == self.lang:
            return
        self.lang = new_lang
        i18n.save_language_preference(new_lang)
        self.root.title(i18n.t("app_title", self.lang))

        # Update language selector buttons appearance
        for c, b in self._lang_btns.items():
            active = (c == self.lang)
            b.configure(
                bg=ACCENT_BLUE if active else "#FFFFFF",
                fg="#FFFFFF" if active else FG_PRIMARY,
                font=("Segoe UI", 8, "bold" if active else "normal"),
            )

        # Update sidebar texts
        nav_labels = {
            "vault": i18n.t("nav_vault", self.lang),
            "unlock": i18n.t("nav_unlock", self.lang),
            "shred": i18n.t("nav_shred", self.lang),
            "printer": i18n.t("nav_printer", self.lang),
        }
        icons = {"vault": "🔐", "unlock": "🔓", "shred": "🗑", "printer": "📱"}
        for k, (_, lbl, _) in self._nav_rows.items():
            lbl.configure(text=f"  {icons.get(k, '')}   {nav_labels.get(k, '')}")

        mode = self.var_conn_mode.get()
        btn_txt = i18n.t("printer_btn_connect_direct", self.lang) if mode in ("direct_ble", "ble") else i18n.t("printer_btn_connect_bridge", self.lang)
        self.btn_quick_connect.configure(text=btn_txt)
        self.btn_quick_stop.configure(text=i18n.t("btn_turn_off_bridge", self.lang))
        self.lbl_sidebar_tagline.configure(text=i18n.t("sidebar_tagline", self.lang))
        self.btn_cancel_job.configure(text=i18n.t("btn_cancel_job", self.lang))

        if self._bridge_connected:
            self.var_sidebar_status.set(i18n.t("sidebar_status_connected", self.lang, self._bridge_printer_name or "X5"))
        else:
            self.var_sidebar_status.set(i18n.t("sidebar_status_disconnected", self.lang))

        # Rebuild all 4 pages
        for frame in self.pages.values():
            frame.destroy()
        self.pages.clear()
        self.pages["vault"] = self._build_vault_page(self.page_host)
        self.pages["unlock"] = self._build_unlock_page(self.page_host)
        self.pages["shred"] = self._build_shred_page(self.page_host)
        self.pages["printer"] = self._build_printer_page(self.page_host)

        # Restore state in rebuilt pages
        if self._vault_src_path:
            size_txt = format_bytes(os.path.getsize(self._vault_src_path)) if os.path.isfile(self._vault_src_path) else ""
            self.var_vault_src.set(i18n.t("vault_file_selected", self.lang, os.path.basename(self._vault_src_path), size_txt))
        if self._vault_items:
            self._render_vault_rows()

        if self._unlock_vault_path:
            self.var_unlock_vault.set(os.path.basename(self._unlock_vault_path))
        if self._unlock_scanned_key:
            self.var_unlock_key_status.set(i18n.t("unlock_key_loaded", self.lang, self._unlock_scanned_key.vault_id_hex[:12] + "…"))

        if self._shred_queue:
            self._render_shred_rows()

        self._select_page(self._current_page)
        self.set_toast(i18n.t("toast_ready", self.lang))

    def _build_sidebar(self) -> None:
        # Brand header (ServBay logo look)
        brand = tk.Frame(self.sidebar, bg=BG_SIDEBAR, pady=14, padx=16)
        brand.pack(fill=tk.X)

        if getattr(self, "_brand_icon_img", None) is not None:
            tk.Label(brand, image=self._brand_icon_img, bg=BG_SIDEBAR).pack(side=tk.LEFT, padx=(0, 8))
        else:
            logo = tk.Canvas(brand, width=28, height=28, bg=BG_SIDEBAR, highlightthickness=0)
            logo.pack(side=tk.LEFT, padx=(0, 8))
            logo.create_polygon(4, 10, 14, 3, 24, 10, 14, 17, fill="#15803D", outline="")
            logo.create_polygon(4, 18, 14, 11, 24, 18, 14, 25, fill="#1E293B", outline="")

        tk.Label(
            brand, text="PAPER-VOID", bg=BG_SIDEBAR, fg=FG_PRIMARY,
            font=("Georgia", 13, "bold"),
        ).pack(side=tk.LEFT)

        # Language Switcher: [🌐 EN | VI | 中文]
        lang_box = tk.Frame(self.sidebar, bg=BG_SIDEBAR, padx=16, pady=2)
        lang_box.pack(fill=tk.X, pady=(0, 8))
        self._lang_btns = {}
        for lcode, llabel in [("en", "EN"), ("vi", "VI"), ("zh", "中文")]:
            active = (lcode == self.lang)
            btn = tk.Button(
                lang_box,
                text=llabel,
                command=lambda c=lcode: self.switch_language(c),
                bg=ACCENT_BLUE if active else "#FFFFFF",
                fg="#FFFFFF" if active else FG_PRIMARY,
                activebackground=ACCENT_BLUE_HOVER,
                activeforeground="#FFFFFF",
                font=("Segoe UI", 8, "bold" if active else "normal"),
                relief=tk.SOLID,
                bd=1,
                padx=6,
                pady=1,
                cursor="hand2",
            )
            btn.pack(side=tk.LEFT, padx=(0, 4))
            self._lang_btns[lcode] = btn

        # Nav items
        self._nav_rows: dict[str, tuple[tk.Frame, tk.Label, tk.Frame]] = {}
        nav_defs = [
            ("vault", "🔐", i18n.t("nav_vault", self.lang)),
            ("unlock", "🔓", i18n.t("nav_unlock", self.lang)),
            ("shred", "🗑", i18n.t("nav_shred", self.lang)),
            ("printer", "📱", i18n.t("nav_printer", self.lang)),
        ]
        nav_box = tk.Frame(self.sidebar, bg=BG_SIDEBAR)
        nav_box.pack(fill=tk.X, pady=(4, 0))

        for key, icon, text in nav_defs:
            row = tk.Frame(nav_box, bg=BG_SIDEBAR, cursor="hand2")
            row.pack(fill=tk.X, pady=2)
            lbl = tk.Label(
                row,
                text=f"  {icon}   {text}",
                bg=BG_SIDEBAR,
                fg=FG_SECONDARY,
                font=("Segoe UI", 10),
                anchor="w",
                padx=10,
                pady=8,
                cursor="hand2",
            )
            lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
            indicator = tk.Frame(row, bg=BG_SIDEBAR, width=3)
            indicator.pack(side=tk.RIGHT, fill=tk.Y, pady=5)

            for w in (row, lbl):
                w.bind("<Button-1>", lambda _e, k=key: self._select_page(k))
            self._nav_rows[key] = (row, lbl, indicator)

        # Spacer
        tk.Frame(self.sidebar, bg=BG_SIDEBAR).pack(fill=tk.BOTH, expand=True)

        # Bottom sidebar: Live X5 status + One-click "Bật App ĐT & Kết Nối X5"
        foot = tk.Frame(self.sidebar, bg=BG_SIDEBAR, padx=14, pady=14)
        foot.pack(fill=tk.X)

        self.var_sidebar_status = tk.StringVar(value=i18n.t("sidebar_status_disconnected", self.lang))
        self.lbl_sidebar_status = tk.Label(
            foot,
            textvariable=self.var_sidebar_status,
            bg="#FFFFFF",
            fg=FG_PRIMARY,
            font=("Segoe UI", 9, "bold"),
            padx=8,
            pady=5,
            anchor="w",
            relief=tk.SOLID,
            bd=1,
        )
        self.lbl_sidebar_status.pack(fill=tk.X, pady=(0, 8))

        self.btn_quick_connect = tk.Button(
            foot,
            text=i18n.t("printer_btn_connect_direct", self.lang),
            command=self.connect_printer,
            bg=ACCENT_BLUE,
            fg="#FFFFFF",
            activebackground=ACCENT_BLUE_HOVER,
            activeforeground="#FFFFFF",
            relief=tk.FLAT,
            font=("Segoe UI", 9, "bold"),
            pady=6,
            cursor="hand2",
        )
        self.btn_quick_connect.pack(fill=tk.X, pady=(0, 6))

        self.btn_quick_stop = tk.Button(
            foot,
            text=i18n.t("btn_turn_off_bridge", self.lang),
            command=self.stop_phone_bridge,
            bg=DANGER_RED,
            fg="#FFFFFF",
            activebackground="#991B1B",
            activeforeground="#FFFFFF",
            relief=tk.FLAT,
            font=("Segoe UI", 9, "bold"),
            pady=5,
            cursor="hand2",
        )
        self.btn_quick_stop.pack(fill=tk.X)

        self.lbl_sidebar_tagline = tk.Label(
            foot,
            text=i18n.t("sidebar_tagline", self.lang),
            bg=BG_SIDEBAR,
            fg=FG_MUTED,
            font=("Segoe UI", 8),
            justify=tk.CENTER,
        )
        self.lbl_sidebar_tagline.pack(pady=(10, 0))

    def _select_page(self, key: str) -> None:
        self._current_page = key
        for k, (row, lbl, ind) in self._nav_rows.items():
            active = k == key
            bg = "#FFFFFF" if active else BG_SIDEBAR
            fg = FG_PRIMARY if active else FG_SECONDARY
            font = ("Segoe UI", 10, "bold") if active else ("Segoe UI", 10)
            row.configure(bg=bg)
            lbl.configure(bg=bg, fg=fg, font=font)
            ind.configure(bg="#CA8A04" if active else bg)

        for k, frame in self.pages.items():
            if k == key:
                frame.pack(fill=tk.BOTH, expand=True)
            else:
                frame.pack_forget()

    def _make_page_header(
        self,
        parent: tk.Frame,
        title: str,
        subtitle: str,
        refresh_cmd: Optional[Callable[[], None]] = None,
    ) -> None:
        hdr = tk.Frame(parent, bg=BG_WINDOW)
        hdr.pack(fill=tk.X, pady=(0, 12))
        top = tk.Frame(hdr, bg=BG_WINDOW)
        top.pack(fill=tk.X)
        tk.Label(
            top, text=title, bg=BG_WINDOW, fg=FG_PRIMARY, font=("Segoe UI", 18, "bold")
        ).pack(side=tk.LEFT)
        if refresh_cmd is not None:
            tk.Button(
                top,
                text="↻",
                command=refresh_cmd,
                bg=ACCENT_BLUE,
                fg="#FFFFFF",
                activebackground=ACCENT_BLUE_HOVER,
                activeforeground="#FFFFFF",
                relief=tk.FLAT,
                font=("Segoe UI", 10, "bold"),
                width=2,
                cursor="hand2",
            ).pack(side=tk.LEFT, padx=(10, 0), pady=(4, 0))
        tk.Label(
            hdr, text=subtitle, bg=BG_WINDOW, fg=FG_MUTED, font=("Segoe UI", 9)
        ).pack(anchor="w", pady=(2, 0))

    # ------------------------------------------------------------------ Page 1: Khóa File (Vault)

    def _build_vault_page(self, parent: tk.Frame) -> tk.Frame:
        page = tk.Frame(parent, bg=BG_WINDOW)
        self._make_page_header(
            page,
            i18n.t("vault_page_title", self.lang),
            i18n.t("vault_page_subtitle", self.lang),
            refresh_cmd=self.connect_phone_bridge,
        )

        # Top white card: Pick file + optional 2FA + Encrypt button
        card = tk.Frame(page, bg=BG_CARD, highlightbackground=BORDER_CARD, highlightthickness=1, padx=14, pady=12)
        card.pack(fill=tk.X, pady=(0, 14))

        r1 = tk.Frame(card, bg=BG_CARD)
        r1.pack(fill=tk.X)

        self.btn_pick_vault_src = tk.Button(
            r1,
            text=i18n.t("vault_choose_file", self.lang),
            command=self.pick_vault_source,
            bg="#FFFFFF",
            fg=FG_PRIMARY,
            activebackground="#FFFFCC",
            activeforeground=FG_PRIMARY,
            relief=tk.SOLID,
            bd=1,
            font=("Segoe UI", 9, "bold"),
            padx=12,
            pady=5,
            cursor="hand2",
        )
        self.btn_pick_vault_src.pack(side=tk.LEFT)

        self.var_vault_src = tk.StringVar(
            value=i18n.t("vault_drop_hint", self.lang)
        )
        self.lbl_vault_drop = tk.Label(
            r1,
            textvariable=self.var_vault_src,
            bg=BG_INPUT,
            fg=FG_SECONDARY,
            font=("Segoe UI", 9),
            anchor="w",
            padx=10,
            pady=6,
            highlightbackground=BORDER_SUBTLE,
            highlightthickness=1,
        )
        self.lbl_vault_drop.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 0))
        if self.dnd_active:
            try:
                self.lbl_vault_drop.drop_target_register(DND_FILES)
                self.lbl_vault_drop.dnd_bind("<<Drop>>", self._on_vault_drop)
            except Exception:
                pass

        r2 = tk.Frame(card, bg=BG_CARD)
        r2.pack(fill=tk.X, pady=(10, 0))

        tk.Label(r2, text=i18n.t("vault_2fa_label", self.lang), bg=BG_CARD, fg=FG_SECONDARY, font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self.var_lock_pw = tk.StringVar()
        tk.Entry(
            r2, textvariable=self.var_lock_pw, show="●", width=26,
            bg=BG_INPUT, fg=FG_PRIMARY, relief=tk.FLAT,
            highlightbackground=BORDER_SUBTLE, highlightthickness=1, font=("Segoe UI", 9),
        ).pack(side=tk.LEFT, padx=(6, 12), ipady=4)

        self.btn_lock_now = tk.Button(
            r2,
            text=i18n.t("vault_btn_lock", self.lang),
            command=self.start_vault_lock,
            bg=ACCENT_BLUE,
            fg="#FFFFFF",
            disabledforeground="#CBD5E1",
            activebackground=ACCENT_BLUE_HOVER,
            activeforeground="#FFFFFF",
            relief=tk.FLAT,
            font=("Segoe UI", 9, "bold"),
            padx=14,
            pady=6,
            cursor="hand2",
            state=tk.DISABLED,
        )
        self.btn_lock_now.pack(side=tk.RIGHT)

        # Table header (ServBay Package Name | Size | Vault ID | Status | Control)
        cols = tk.Frame(page, bg=BG_WINDOW, padx=14)
        cols.pack(fill=tk.X, pady=(0, 6))
        tk.Label(cols, text=i18n.t("vault_th_filename", self.lang), bg=BG_WINDOW, fg=FG_PRIMARY, font=("Segoe UI", 9, "bold"), width=30, anchor="w").pack(side=tk.LEFT)
        tk.Label(cols, text=i18n.t("vault_th_size", self.lang), bg=BG_WINDOW, fg=FG_PRIMARY, font=("Segoe UI", 9, "bold"), width=11, anchor="w").pack(side=tk.LEFT)
        tk.Label(cols, text=i18n.t("vault_th_status", self.lang), bg=BG_WINDOW, fg=FG_PRIMARY, font=("Segoe UI", 9, "bold"), width=18, anchor="w").pack(side=tk.LEFT)
        tk.Label(cols, text=i18n.t("vault_th_actions", self.lang), bg=BG_WINDOW, fg=FG_PRIMARY, font=("Segoe UI", 9, "bold"), anchor="e").pack(side=tk.RIGHT)

        # Scrollable list of ServBay white card rows
        list_wrap = tk.Frame(page, bg=BG_WINDOW)
        list_wrap.pack(fill=tk.BOTH, expand=True)

        self.vault_canvas = tk.Canvas(list_wrap, bg=BG_WINDOW, highlightthickness=0)
        v_sb = ttk.Scrollbar(list_wrap, orient=tk.VERTICAL, command=self.vault_canvas.yview)
        self.vault_rows_frame = tk.Frame(self.vault_canvas, bg=BG_WINDOW)
        self.vault_rows_frame.bind(
            "<Configure>",
            lambda _e: self.vault_canvas.configure(scrollregion=self.vault_canvas.bbox("all")),
        )
        self._vault_win_id = self.vault_canvas.create_window((0, 0), window=self.vault_rows_frame, anchor="nw")
        self.vault_canvas.bind(
            "<Configure>",
            lambda e: self.vault_canvas.itemconfigure(self._vault_win_id, width=e.width),
        )
        self.vault_canvas.configure(yscrollcommand=v_sb.set)
        self.vault_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        v_sb.pack(side=tk.RIGHT, fill=tk.Y)

        self._render_vault_rows()
        return page

    def _render_vault_rows(self) -> None:
        for child in self.vault_rows_frame.winfo_children():
            child.destroy()

        if not self._vault_items:
            empty = tk.Frame(
                self.vault_rows_frame, bg=BG_CARD, highlightbackground=BORDER_CARD,
                highlightthickness=1, padx=16, pady=24,
            )
            empty.pack(fill=tk.X, pady=4)
            tk.Label(
                empty,
                text="Chưa có file nào được khóa trong phiên này.\n"
                     "Chọn 1 file ở phía trên để mã hóa từng khối 1 MB và tạo Thẻ Khóa ~8.4 cm.",
                bg=BG_CARD, fg=FG_MUTED, font=("Segoe UI", 9), justify=tk.CENTER,
            ).pack()
            return

        for idx, item in enumerate(self._vault_items):
            res = item.result
            row = tk.Frame(
                self.vault_rows_frame, bg=BG_CARD,
                highlightbackground=BORDER_CARD, highlightthickness=1,
                padx=12, pady=8,
            )
            row.pack(fill=tk.X, pady=4)

            name_disp = res.filename if len(res.filename) <= 28 else res.filename[:20] + "…" + res.filename[-6:]
            tk.Label(
                row, text=f"📄  {name_disp}", bg=BG_CARD, fg=FG_PRIMARY,
                font=("Segoe UI", 9, "bold"), width=30, anchor="w",
            ).pack(side=tk.LEFT)

            tk.Label(
                row, text=format_bytes(res.orig_size), bg=BG_CARD, fg=FG_SECONDARY,
                font=("Segoe UI", 9), width=11, anchor="w",
            ).pack(side=tk.LEFT)

            if item.shredded:
                st_txt, st_fg = "Đã SHRED gốc ✓", FG_MUTED
            elif item.printed_copies > 0:
                st_txt, st_fg = f"Đã in {item.printed_copies} bản ●", STATUS_GREEN
            else:
                st_txt, st_fg = f"Sẵn sàng ({res.card_length_cm:.1f}cm) ●", STATUS_GREEN

            tk.Label(
                row, text=st_txt, bg=BG_CARD, fg=st_fg,
                font=("Segoe UI", 9, "bold"), width=18, anchor="w",
            ).pack(side=tk.LEFT)

            # Right controls (ServBay icon-style buttons)
            btns = tk.Frame(row, bg=BG_CARD)
            btns.pack(side=tk.RIGHT)

            tk.Button(
                btns, text=i18n.t("vault_btn_view", self.lang), command=lambda i=idx: self.preview_vault_item(i),
                bg="#FFFFFF", fg=FG_PRIMARY, activebackground="#FFFFCC",
                relief=tk.SOLID, bd=1, font=("Segoe UI", 8, "bold"), padx=8, pady=3, cursor="hand2",
            ).pack(side=tk.LEFT, padx=2)

            tk.Button(
                btns, text=i18n.t("vault_btn_print1", self.lang), command=lambda i=idx: self.print_vault_item(i, copies=1),
                bg=ACCENT_BLUE, fg="#FFFFFF", activebackground=ACCENT_BLUE_HOVER, activeforeground="#FFFFFF",
                relief=tk.FLAT, font=("Segoe UI", 8, "bold"), padx=9, pady=4, cursor="hand2",
            ).pack(side=tk.LEFT, padx=2)

            tk.Button(
                btns, text=i18n.t("vault_btn_print2", self.lang), command=lambda i=idx: self.print_vault_item(i, copies=2),
                bg=STATUS_GREEN, fg="#FFFFFF", activebackground="#166534", activeforeground="#FFFFFF",
                relief=tk.FLAT, font=("Segoe UI", 8, "bold"), padx=9, pady=4, cursor="hand2",
            ).pack(side=tk.LEFT, padx=2)

            shred_enabled = (not item.shredded) and os.path.isfile(item.source_path)
            tk.Button(
                btns,
                text=i18n.t("vault_btn_shred", self.lang),
                command=lambda i=idx: self.shred_vault_item(i),
                bg=DANGER_RED if shred_enabled else "#E5E7EB",
                fg="#FFFFFF" if shred_enabled else BTN_DISABLED_FG,
                disabledforeground=BTN_DISABLED_FG,
                activebackground="#991B1B",
                activeforeground="#FFFFFF",
                relief=tk.FLAT,
                font=("Segoe UI", 8, "bold"),
                padx=9,
                pady=4,
                cursor="hand2" if shred_enabled else "arrow",
                state=tk.NORMAL if shred_enabled else tk.DISABLED,
            ).pack(side=tk.LEFT, padx=(6, 0))

    # ------------------------------------------------------------------ Page 2: Mở Khóa (Unlock)

    def _build_unlock_page(self, parent: tk.Frame) -> tk.Frame:
        page = tk.Frame(parent, bg=BG_WINDOW)
        self._make_page_header(
            page,
            i18n.t("unlock_page_title", self.lang),
            i18n.t("unlock_page_subtitle", self.lang),
        )

        # Card 1: Select .pvvault
        c1 = tk.Frame(page, bg=BG_CARD, highlightbackground=BORDER_CARD, highlightthickness=1, padx=14, pady=12)
        c1.pack(fill=tk.X, pady=(0, 10))
        tk.Label(c1, text=i18n.t("unlock_step1", self.lang), bg=BG_CARD, fg=FG_PRIMARY,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 6))
        r1 = tk.Frame(c1, bg=BG_CARD)
        r1.pack(fill=tk.X)
        tk.Button(
            r1, text=i18n.t("unlock_btn_browse_vault", self.lang), command=self.pick_unlock_vault,
            bg="#FFFFFF", fg=FG_PRIMARY, activebackground="#FFFFCC",
            relief=tk.SOLID, bd=1, font=("Segoe UI", 9, "bold"), padx=12, pady=5, cursor="hand2",
        ).pack(side=tk.LEFT)
        self.var_unlock_vault = tk.StringVar(value=i18n.t("unlock_no_vault", self.lang))
        tk.Label(r1, textvariable=self.var_unlock_vault, bg=BG_CARD, fg=FG_SECONDARY,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=12)

        # Card 2: Key source (Phone Live Scanner via USB/Tailscale OR Photo/Video OR Base32)
        c2 = tk.Frame(page, bg=BG_CARD, highlightbackground=BORDER_CARD, highlightthickness=1, padx=14, pady=12)
        c2.pack(fill=tk.X, pady=(0, 10))
        tk.Label(c2, text=i18n.t("unlock_step2", self.lang), bg=BG_CARD, fg=FG_PRIMARY,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 8))

        # Connection mode selector: USB Cable vs Tailscale
        r2_conn = tk.Frame(c2, bg=BG_CARD)
        r2_conn.pack(fill=tk.X, pady=(0, 8))
        tk.Label(r2_conn, text=i18n.t("unlock_conn_label", self.lang), bg=BG_CARD, fg=FG_SECONDARY, font=("Segoe UI", 9)).pack(side=tk.LEFT)
        tk.Radiobutton(
            r2_conn, text=i18n.t("unlock_conn_usb", self.lang), variable=self.var_unlock_conn_mode, value="usb",
            bg=BG_CARD, fg=FG_PRIMARY, selectcolor=BG_CARD, activebackground=BG_CARD,
            font=("Segoe UI", 9, "bold"), cursor="hand2",
        ).pack(side=tk.LEFT, padx=(8, 10))
        tk.Radiobutton(
            r2_conn, text=i18n.t("unlock_conn_tailscale", self.lang), variable=self.var_unlock_conn_mode, value="tailscale",
            bg=BG_CARD, fg=FG_PRIMARY, selectcolor=BG_CARD, activebackground=BG_CARD,
            font=("Segoe UI", 9, "bold"), cursor="hand2",
        ).pack(side=tk.LEFT, padx=(0, 6))
        tk.Entry(
            r2_conn, textvariable=self.var_tailscale_ip, width=16,
            bg=BG_INPUT, fg=FG_PRIMARY, relief=tk.FLAT,
            highlightbackground=BORDER_SUBTLE, highlightthickness=1, font=("Consolas", 9),
        ).pack(side=tk.LEFT, ipady=3)
        tk.Button(
            r2_conn, text="⚡ Dò IP", command=self.on_click_detect_ip,
            bg="#FFFFFF", fg=FG_PRIMARY, activebackground="#FFFFCC",
            relief=tk.SOLID, bd=1, font=("Segoe UI", 8, "bold"), padx=6, pady=1, cursor="hand2",
        ).pack(side=tk.LEFT, padx=(4, 4))
        tk.Label(r2_conn, text=i18n.t("unlock_tailscale_hint", self.lang), bg=BG_CARD, fg=FG_MUTED, font=("Segoe UI", 8)).pack(side=tk.LEFT)

        r2a = tk.Frame(c2, bg=BG_CARD)
        r2a.pack(fill=tk.X, pady=(0, 8))
        self.btn_phone_scan = tk.Button(
            r2a, text=i18n.t("unlock_btn_scan_phone", self.lang), command=self.start_phone_live_scan,
            bg=ACCENT_BLUE, fg="#FFFFFF", activebackground=ACCENT_BLUE_HOVER, activeforeground="#FFFFFF",
            relief=tk.FLAT, font=("Segoe UI", 9, "bold"), padx=14, pady=6, cursor="hand2",
        )
        self.btn_phone_scan.pack(side=tk.LEFT)
        tk.Button(
            r2a, text=i18n.t("unlock_btn_browse_photo", self.lang), command=self.scan_unlock_key_media,
            bg=STATUS_GREEN, fg="#FFFFFF", activebackground="#166534", activeforeground="#FFFFFF",
            relief=tk.FLAT, font=("Segoe UI", 9, "bold"), padx=12, pady=6, cursor="hand2",
        ).pack(side=tk.LEFT, padx=(8, 0))
        self.var_unlock_key_status = tk.StringVar(value=i18n.t("unlock_key_unscanned", self.lang))
        tk.Label(r2a, textvariable=self.var_unlock_key_status, bg=BG_CARD, fg=FG_MUTED,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=10)

        tk.Label(
            c2,
            text=i18n.t("unlock_or_manual", self.lang),
            bg=BG_CARD, fg=FG_SECONDARY, font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(2, 4))
        self.var_unlock_base32 = tk.StringVar()
        tk.Entry(
            c2, textvariable=self.var_unlock_base32, bg=BG_INPUT, fg=FG_PRIMARY,
            relief=tk.FLAT, highlightbackground=BORDER_SUBTLE, highlightthickness=1,
            font=("Consolas", 10),
        ).pack(fill=tk.X, ipady=5)

        # Card 3: 2FA + Unlock button
        c3 = tk.Frame(page, bg=BG_CARD, highlightbackground=BORDER_CARD, highlightthickness=1, padx=14, pady=12)
        c3.pack(fill=tk.X)
        r3 = tk.Frame(c3, bg=BG_CARD)
        r3.pack(fill=tk.X)
        tk.Label(r3, text="Mật khẩu 2FA (nếu có):", bg=BG_CARD, fg=FG_SECONDARY, font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self.var_unlock_pw = tk.StringVar()
        tk.Entry(
            r3, textvariable=self.var_unlock_pw, show="●", width=28,
            bg=BG_INPUT, fg=FG_PRIMARY, relief=tk.FLAT,
            highlightbackground=BORDER_SUBTLE, highlightthickness=1, font=("Segoe UI", 9),
        ).pack(side=tk.LEFT, padx=8, ipady=4)

        self.btn_unlock_now = tk.Button(
            r3, text=i18n.t("unlock_btn_decrypt", self.lang), command=self.start_vault_unlock,
            bg=ACCENT_BLUE, fg="#FFFFFF", disabledforeground="#CBD5E1",
            activebackground=ACCENT_BLUE_HOVER, activeforeground="#FFFFFF",
            relief=tk.FLAT, font=("Segoe UI", 9, "bold"), padx=14, pady=6, cursor="hand2",
            state=tk.DISABLED,
        )
        self.btn_unlock_now.pack(side=tk.RIGHT)

        return page

    # ------------------------------------------------------------------ Page 3: Hủy File (Shred)

    def _build_shred_page(self, parent: tk.Frame) -> tk.Frame:
        page = tk.Frame(parent, bg=BG_WINDOW)
        self._make_page_header(
            page,
            i18n.t("shred_page_title", self.lang),
            i18n.t("shred_page_subtitle", self.lang),
        )

        # SSD warning banner
        warn = tk.Label(
            page,
            text=i18n.t("shred_ssd_note", self.lang),
            bg=STATUS_AMBER_BG, fg=STATUS_AMBER, font=("Segoe UI", 9),
            anchor="w", justify=tk.LEFT, wraplength=760, padx=12, pady=8,
        )
        warn.pack(fill=tk.X, pady=(0, 10))

        bar = tk.Frame(page, bg=BG_CARD, highlightbackground=BORDER_CARD, highlightthickness=1, padx=12, pady=10)
        bar.pack(fill=tk.X, pady=(0, 10))

        tk.Button(
            bar, text=i18n.t("shred_btn_add", self.lang), command=self.add_shred_files,
            bg=DANGER_RED, fg="#FFFFFF", activebackground="#991B1B", activeforeground="#FFFFFF",
            relief=tk.FLAT, font=("Segoe UI", 9, "bold"), padx=12, pady=6, cursor="hand2",
        ).pack(side=tk.LEFT)

        tk.Button(
            bar, text=i18n.t("shred_btn_clear", self.lang), command=self.clear_shred_queue,
            bg="#FFFFFF", fg=FG_PRIMARY, activebackground="#FFFFCC",
            relief=tk.SOLID, bd=1, font=("Segoe UI", 9, "bold"), padx=10, pady=5, cursor="hand2",
        ).pack(side=tk.LEFT, padx=8)

        self.lbl_shred_drop = tk.Label(
            bar,
            text=i18n.t("shred_drop_hint", self.lang) if self.dnd_active else "",
            bg=BG_CARD, fg=FG_MUTED, font=("Segoe UI", 9),
        )
        self.lbl_shred_drop.pack(side=tk.RIGHT)
        if self.dnd_active:
            try:
                self.lbl_shred_drop.drop_target_register(DND_FILES)
                self.lbl_shred_drop.dnd_bind("<<Drop>>", self._on_shred_drop)
            except Exception:
                pass

        self.shred_rows_frame = tk.Frame(page, bg=BG_WINDOW)
        self.shred_rows_frame.pack(fill=tk.BOTH, expand=True)
        self._render_shred_rows()
        return page

    def _render_shred_rows(self) -> None:
        for child in self.shred_rows_frame.winfo_children():
            child.destroy()

        if not self._shred_queue:
            empty = tk.Frame(
                self.shred_rows_frame, bg=BG_CARD, highlightbackground=BORDER_CARD,
                highlightthickness=1, padx=16, pady=24,
            )
            empty.pack(fill=tk.X)
            tk.Label(
                empty,
                text=i18n.t("shred_empty_queue", self.lang),
                bg=BG_CARD, fg=FG_MUTED, font=("Segoe UI", 9),
            ).pack()
            return

        for idx, fpath in enumerate(list(self._shred_queue)):
            exists = os.path.isfile(fpath)
            row = tk.Frame(
                self.shred_rows_frame, bg=BG_CARD, highlightbackground=BORDER_CARD,
                highlightthickness=1, padx=12, pady=8,
            )
            row.pack(fill=tk.X, pady=4)

            name = os.path.basename(fpath)
            sz_txt = format_bytes(os.path.getsize(fpath)) if exists else "Không còn tồn tại"
            tk.Label(row, text=f"📄  {name}", bg=BG_CARD, fg=FG_PRIMARY, font=("Segoe UI", 9, "bold"),
                     width=34, anchor="w").pack(side=tk.LEFT)
            tk.Label(row, text=sz_txt, bg=BG_CARD, fg=FG_MUTED, font=("Segoe UI", 9),
                     width=14, anchor="w").pack(side=tk.LEFT)

            tk.Button(
                row, text=i18n.t("shred_btn_action", self.lang), command=lambda p=fpath: self._execute_shred_from_queue(p),
                bg=DANGER_RED, fg="#FFFFFF", activebackground="#B91C1C", activeforeground="#FFFFFF",
                relief=tk.FLAT, font=("Segoe UI", 8, "bold"), padx=10, pady=4, cursor="hand2",
                state=tk.NORMAL if exists else tk.DISABLED,
            ).pack(side=tk.RIGHT)

            tk.Button(
                row, text=i18n.t("shred_btn_remove", self.lang), command=lambda i=idx: self._remove_shred_index(i),
                bg="#ECECF0", fg=FG_SECONDARY, activebackground="#DCDCE2",
                relief=tk.FLAT, font=("Segoe UI", 8), padx=8, pady=4, cursor="hand2",
            ).pack(side=tk.RIGHT, padx=(0, 6))

    # ------------------------------------------------------------------ Page 4: Máy In X5 (ĐT)

    def _build_printer_page(self, parent: tk.Frame) -> tk.Frame:
        page = tk.Frame(parent, bg=BG_WINDOW)
        self._make_page_header(
            page,
            i18n.t("printer_page_title", self.lang),
            i18n.t("printer_page_subtitle", self.lang),
            refresh_cmd=self.connect_phone_bridge,
        )

        # Card 1: One-click connect & status (properly wrapped, never clips!)
        c1 = tk.Frame(page, bg=BG_CARD, highlightbackground=BORDER_CARD, highlightthickness=1, padx=14, pady=14)
        c1.pack(fill=tk.X, pady=(0, 12))

        r1_top = tk.Frame(c1, bg=BG_CARD)
        r1_top.pack(fill=tk.X)
        tk.Label(r1_top, text=i18n.t("printer_conn_section", self.lang), bg=BG_CARD, fg=FG_PRIMARY,
                 font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        self.btn_page_disconnect = tk.Button(
            r1_top, text=i18n.t("btn_turn_off_bridge", self.lang), command=self.stop_phone_bridge,
            bg=DANGER_RED, fg="#FFFFFF", activebackground="#991B1B", activeforeground="#FFFFFF",
            relief=tk.FLAT, font=("Segoe UI", 9, "bold"), padx=12, pady=6, cursor="hand2",
        )
        self.btn_page_disconnect.pack(side=tk.RIGHT, padx=(6, 0))
        self.btn_page_connect = tk.Button(
            r1_top, text=i18n.t("printer_btn_connect_direct", self.lang), command=self.connect_printer,
            bg=ACCENT_BLUE, fg="#FFFFFF", activebackground=ACCENT_BLUE_HOVER, activeforeground="#FFFFFF",
            relief=tk.FLAT, font=("Segoe UI", 9, "bold"), padx=14, pady=6, cursor="hand2",
        )
        self.btn_page_connect.pack(side=tk.RIGHT)

        self.var_printer_detail = tk.StringVar(value=i18n.t("printer_status_disconnected", self.lang))
        tk.Label(
            c1, textvariable=self.var_printer_detail, bg=BG_CARD, fg=FG_SECONDARY,
            font=("Segoe UI", 9), anchor="w", justify=tk.LEFT, wraplength=700,
        ).pack(fill=tk.X, pady=(8, 4))

        # Row 2: Direct PC Bluetooth (BLE)
        r2 = tk.Frame(c1, bg=BG_CARD)
        r2.pack(fill=tk.X, pady=(8, 0))
        tk.Radiobutton(
            r2, text=i18n.t("printer_conn_direct_ble", self.lang), variable=self.var_conn_mode, value="direct_ble",
            command=self._update_conn_mode_ui,
            bg=BG_CARD, fg=FG_PRIMARY, selectcolor=BG_CARD, activebackground=BG_CARD,
            font=("Segoe UI", 9, "bold"), cursor="hand2",
        ).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(r2, text=i18n.t("printer_ble_mac_label", self.lang), bg=BG_CARD, fg=FG_SECONDARY, font=("Segoe UI", 9)).pack(side=tk.LEFT)
        tk.Entry(
            r2, textvariable=self.var_ble_mac, width=19, bg=BG_INPUT, fg=FG_PRIMARY,
            relief=tk.FLAT, highlightbackground=BORDER_SUBTLE, highlightthickness=1, font=("Consolas", 9),
        ).pack(side=tk.LEFT, padx=(4, 4), ipady=4)
        tk.Button(
            r2, text=i18n.t("printer_btn_scan_ble", self.lang), command=self.on_click_scan_ble,
            bg="#FFFFFF", fg=FG_PRIMARY, activebackground="#FFFFCC",
            relief=tk.SOLID, bd=1, font=("Segoe UI", 8, "bold"), padx=6, pady=1, cursor="hand2",
        ).pack(side=tk.LEFT, padx=(0, 14))

        tk.Label(r2, text=i18n.t("printer_name_label", self.lang), bg=BG_CARD, fg=FG_SECONDARY, font=("Segoe UI", 9)).pack(side=tk.LEFT)
        tk.Entry(
            r2, textvariable=self.var_printer_name, width=8, bg=BG_INPUT, fg=FG_PRIMARY,
            relief=tk.FLAT, highlightbackground=BORDER_SUBTLE, highlightthickness=1, font=("Consolas", 9),
        ).pack(side=tk.LEFT, padx=4, ipady=4)

        # Row 3: Phone Bridge (USB ADB & Tailscale)
        r3 = tk.Frame(c1, bg=BG_CARD)
        r3.pack(fill=tk.X, pady=(8, 0))
        tk.Radiobutton(
            r3, text=i18n.t("printer_conn_usb", self.lang), variable=self.var_conn_mode, value="usb",
            command=self._update_conn_mode_ui,
            bg=BG_CARD, fg=FG_PRIMARY, selectcolor=BG_CARD, activebackground=BG_CARD,
            font=("Segoe UI", 9, "bold"), cursor="hand2",
        ).pack(side=tk.LEFT, padx=(0, 12))
        tk.Radiobutton(
            r3, text=i18n.t("printer_conn_tailscale", self.lang), variable=self.var_conn_mode, value="tailscale",
            command=self._update_conn_mode_ui,
            bg=BG_CARD, fg=FG_PRIMARY, selectcolor=BG_CARD, activebackground=BG_CARD,
            font=("Segoe UI", 9, "bold"), cursor="hand2",
        ).pack(side=tk.LEFT, padx=(0, 6))
        tk.Entry(
            r3, textvariable=self.var_tailscale_ip, width=16, bg=BG_INPUT, fg=FG_PRIMARY,
            relief=tk.FLAT, highlightbackground=BORDER_SUBTLE, highlightthickness=1, font=("Consolas", 9),
        ).pack(side=tk.LEFT, ipady=4)
        tk.Button(
            r3, text="⚡ Dò IP", command=self.on_click_detect_ip,
            bg="#FFFFFF", fg=FG_PRIMARY, activebackground="#FFFFCC",
            relief=tk.SOLID, bd=1, font=("Segoe UI", 8, "bold"), padx=6, pady=1, cursor="hand2",
        ).pack(side=tk.LEFT, padx=(4, 12))

        # Card 2: Thermal safety controls
        c2 = tk.Frame(page, bg=BG_CARD, highlightbackground=BORDER_CARD, highlightthickness=1, padx=14, pady=14)
        c2.pack(fill=tk.X)
        tk.Label(c2, text=i18n.t("printer_head_settings", self.lang),
                 bg=BG_CARD, fg=FG_PRIMARY, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 10))

        e_row = tk.Frame(c2, bg=BG_CARD)
        e_row.pack(fill=tk.X, pady=4)
        tk.Label(e_row, text=i18n.t("printer_energy_label", self.lang), bg=BG_CARD, fg=FG_SECONDARY, font=("Segoe UI", 9), width=22, anchor="w").pack(side=tk.LEFT)
        self.lbl_energy = tk.Label(e_row, text="20000", bg=BG_CARD, fg=ACCENT_BLUE, font=("Consolas", 10, "bold"), width=7)
        self.scale_energy = ttk.Scale(e_row, from_=8000, to=38000, orient=tk.HORIZONTAL,
                                      command=self._on_energy_scale, length=320)
        self.scale_energy.set(20000)
        self.scale_energy.pack(side=tk.LEFT, padx=8)
        self.lbl_energy.pack(side=tk.LEFT)

        s_row = tk.Frame(c2, bg=BG_CARD)
        s_row.pack(fill=tk.X, pady=4)
        tk.Label(s_row, text=i18n.t("printer_speed_label", self.lang), bg=BG_CARD, fg=FG_SECONDARY, font=("Segoe UI", 9), width=22, anchor="w").pack(side=tk.LEFT)
        self.lbl_speed = tk.Label(s_row, text="30", bg=BG_CARD, fg=ACCENT_BLUE, font=("Consolas", 10, "bold"), width=7)
        self.scale_speed = ttk.Scale(s_row, from_=15, to=45, orient=tk.HORIZONTAL,
                                     command=self._on_speed_scale, length=320)
        self.scale_speed.set(30)
        self.scale_speed.pack(side=tk.LEFT, padx=8)
        self.lbl_speed.pack(side=tk.LEFT)

        btn_test_row = tk.Frame(c2, bg=BG_CARD)
        btn_test_row.pack(fill=tk.X, pady=(12, 0))
        tk.Button(
            btn_test_row,
            text=i18n.t("printer_btn_test", self.lang),
            command=self.print_test_strip,
            bg="#FFFFFF",
            fg=FG_PRIMARY,
            activebackground="#FFFFCC",
            relief=tk.SOLID,
            bd=1,
            font=("Segoe UI", 9, "bold"),
            padx=14,
            pady=4,
            cursor="hand2",
        ).pack(side=tk.LEFT)

        return page

    # ------------------------------------------------------------------ Helpers & Event Pump

    def _on_energy_scale(self, value: Any) -> None:
        val = int(round(float(value)))
        self.var_energy.set(val)
        self.lbl_energy.configure(text=str(val))

    def _on_speed_scale(self, value: Any) -> None:
        val = int(round(float(value)))
        self.var_speed.set(val)
        self.lbl_speed.configure(text=str(val))

    def set_toast(self, message: str, bg: str = TOAST_BG) -> None:
        self.var_toast.set(message)
        self.toast_lbl.configure(bg=bg)

    def post_event(self, kind: str, payload: Any = None) -> None:
        self._events.put((kind, payload))

    def _pump_events(self) -> None:
        if self._closing:
            return
        try:
            while True:
                kind, payload = self._events.get_nowait()
                handler = getattr(self, f"_ev_{kind}", None)
                if handler is not None:
                    handler(payload)
        except queue.Empty:
            pass
        self._after_id = self.root.after(60, self._pump_events)

    def _busy(self) -> bool:
        return self._job_thread is not None and self._job_thread.is_alive()

    def _refresh_controls(self) -> None:
        busy = self._busy()
        self.btn_cancel_job.configure(state=tk.NORMAL if busy else tk.DISABLED)
        have_src = self._vault_src_path is not None and os.path.isfile(self._vault_src_path)
        self.btn_lock_now.configure(state=tk.NORMAL if (have_src and not busy) else tk.DISABLED)
        have_enc = self._unlock_vault_path is not None and os.path.isfile(self._unlock_vault_path)
        self.btn_unlock_now.configure(state=tk.NORMAL if (have_enc and not busy) else tk.DISABLED)

    def cancel_active_job(self) -> None:
        if self._busy():
            self._job_cancel.set()
            self.set_toast("Đang dừng tác vụ an toàn…", STATUS_AMBER)

    # ------------------------------------------------------------------ Printer & Bridge Connect

    def _update_conn_mode_ui(self, *args: Any) -> None:
        mode = self.var_conn_mode.get()
        btn_txt = i18n.t("printer_btn_connect_direct", self.lang) if mode in ("direct_ble", "ble") else i18n.t("printer_btn_connect_bridge", self.lang)
        if hasattr(self, "btn_quick_connect"):
            self.btn_quick_connect.configure(text=btn_txt)
        if hasattr(self, "btn_page_connect"):
            self.btn_page_connect.configure(text=btn_txt)

    def on_click_detect_ip(self) -> None:
        ip = auto_detect_phone_ip()
        if ip:
            self.var_tailscale_ip.set(ip)
            if self._current_page == "unlock":
                self.var_unlock_conn_mode.set("tailscale")
            else:
                self.var_conn_mode.set("tailscale")
            self.set_toast(f"Đã tự dò được IP điện thoại: {ip}")
            self._update_conn_mode_ui()

    def on_click_scan_ble(self) -> None:
        if self._busy():
            return
        self.set_toast("Đang quét tìm máy in BLE quanh máy tính…", ACCENT_BLUE)
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)

        def worker() -> None:
            try:
                import asyncio
                devs = asyncio.run(printer_ble.scan_ble_printers(timeout=4.5))
                self.post_event("ble_scan_done", devs)
            except Exception as exc:
                self.post_event("bridge_err", f"Lỗi quét BLE: {exc}")

        self._job_thread = threading.Thread(target=worker, name="pv-ble-scan", daemon=True)
        self._job_thread.start()
        self._refresh_controls()

    def _ev_ble_scan_done(self, devs: list[Any]) -> None:
        self._job_thread = None
        self.progress.stop()
        self.progress.configure(mode="determinate", value=100)
        if not devs:
            self.set_toast("Không tìm thấy máy in BLE nào quanh máy tính. Hãy bật nguồn X5.", STATUS_AMBER)
            messagebox.showinfo("Quét BLE", "Không tìm thấy máy in BLE nào quanh máy tính. Hãy bật nguồn máy in X5.", parent=self.root)
            return
        first = devs[0]
        self.var_ble_mac.set(first.address)
        self.var_printer_name.set(first.name)
        self.var_conn_mode.set("direct_ble")
        names_str = ", ".join(f"{d.name} ({d.address})" for d in devs)
        self.set_toast(f"Đã tìm thấy {len(devs)} máy in BLE: {names_str}", STATUS_GREEN)
        self._update_conn_mode_ui()

    def connect_printer(self) -> None:
        if self._busy():
            return
        mode = self.var_conn_mode.get()
        if mode in ("direct_ble", "ble"):
            self.connect_direct_ble()
        else:
            self.connect_phone_bridge()

    def connect_direct_ble(self) -> None:
        if self._busy():
            return
        mac = self.var_ble_mac.get().strip()
        pname = self.var_printer_name.get().strip() or "X5"
        self.set_toast(f"Đang kết nối Bluetooth PC trực tiếp tới {pname}…", ACCENT_BLUE)
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)

        def worker() -> None:
            try:
                dev = printer_ble.probe_direct_ble_sync(
                    address=mac,
                    name_hint=pname,
                    status_callback=lambda msg: self.post_event("toast", (msg, ACCENT_BLUE)),
                )
                self.post_event("ble_direct_ok", dev)
            except Exception as exc:
                self.post_event("bridge_err", str(exc))

        self._job_thread = threading.Thread(target=worker, name="pv-ble-direct", daemon=True)
        self._job_thread.start()
        self._refresh_controls()

    def _ev_ble_direct_ok(self, dev: printer_ble.BlePrinterDevice) -> None:
        self._job_thread = None
        self.progress.stop()
        self.progress.configure(mode="determinate", value=100)
        self._bridge_connected = True
        self._bridge_printer_name = dev.name
        self.var_ble_mac.set(dev.address)
        self.var_sidebar_status.set(f"● {dev.name}: PC BLE (Sẵn sàng)")
        self.lbl_sidebar_status.configure(bg=STATUS_GREEN_BG, fg=STATUS_GREEN)
        self.var_printer_detail.set(
            f"● Trực tiếp Bluetooth PC tới máy in: {dev.name} ({dev.address}, RSSI={dev.rssi or '?'} dBm) — Sẵn sàng in!"
        )
        self.set_toast(f"Đã ghép nối trực tiếp Bluetooth PC tới {dev.name} ({dev.address})!", STATUS_GREEN)
        self._refresh_controls()

    def _resolve_bridge_url(self, for_unlock: bool = False) -> str:
        if for_unlock:
            mode = self.var_unlock_conn_mode.get()
        else:
            mode = self.var_conn_mode.get()
            if mode in ("direct_ble", "ble"):
                mode = "tailscale" if self.var_tailscale_ip.get().strip() else "usb"
        return printer_ble.build_bridge_url(
            mode,
            self.var_tailscale_ip.get(),
        )

    def connect_phone_bridge(self) -> None:
        if self._busy():
            return
        try:
            url = self._resolve_bridge_url(for_unlock=False)
        except Exception as exc:
            messagebox.showwarning("Thiếu thông tin kết nối", str(exc), parent=self.root)
            return
        pname = self.var_printer_name.get().strip() or "X5"
        self.set_toast("Đang bật app điện thoại & kết nối máy in X5…", ACCENT_BLUE)
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)

        def worker() -> None:
            try:
                bridge = printer_ble.HttpBridgePrinter(url)
                st = bridge.launch_and_connect(
                    printer_name=pname,
                    status_callback=lambda msg: self.post_event("toast", (msg, ACCENT_BLUE)),
                )
                self.post_event("bridge_ok", st)
            except Exception as exc:
                self.post_event("bridge_err", str(exc))

        self._job_thread = threading.Thread(target=worker, name="pv-bridge", daemon=True)
        self._job_thread.start()
        self._refresh_controls()

    def _ev_toast(self, payload: tuple[str, str]) -> None:
        msg, bg = payload
        self.set_toast(msg, bg)

    def _ev_bridge_ok(self, st: dict[str, Any]) -> None:
        self._job_thread = None
        self.progress.stop()
        self.progress.configure(mode="determinate", value=100)
        name = str(st.get("name") or "X5")
        mac = str(st.get("mac") or "")
        mtu = st.get("mtu", "?")
        self._bridge_connected = True
        self._bridge_printer_name = name
        self.var_sidebar_status.set(f"● {name}: Đã kết nối ĐT")
        self.lbl_sidebar_status.configure(bg=STATUS_GREEN_BG, fg=STATUS_GREEN)
        self.var_printer_detail.set(f"● Đã kết nối qua điện thoại: {name} ({mac}, MTU={mtu}) — sẵn sàng in thẻ 8.4 cm")
        self.set_toast(f"Kết nối thành công tới {name} qua app điện thoại!", STATUS_GREEN)
        self._refresh_controls()

    def _ev_bridge_err(self, message: str) -> None:
        self._job_thread = None
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self._bridge_connected = False
        self.var_sidebar_status.set("○ X5: Chưa kết nối ĐT")
        self.lbl_sidebar_status.configure(bg=DANGER_RED_BG, fg=DANGER_RED)
        self.var_printer_detail.set(f"✘ Chưa kết nối: {message}")
        self.set_toast("Không nối được app điện thoại — kiểm tra cáp USB / USB Debugging", DANGER_RED)
        self._refresh_controls()

    def stop_phone_bridge(self) -> None:
        """Stop TinyPrintBridge completely on the Android phone or disconnect direct BLE."""
        if self._busy():
            return
        mode = self.var_conn_mode.get()
        if mode in ("direct_ble", "ble"):
            self._bridge_connected = False
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.var_sidebar_status.set("○ X5: Chưa kết nối")
            self.lbl_sidebar_status.configure(bg="#EAEAEF", fg=FG_MUTED)
            self.var_printer_detail.set("Trạng thái: Chưa kết nối máy in Bluetooth PC.")
            self.set_toast("Đã ngắt trạng thái kết nối máy in PC.", STATUS_AMBER)
            self._refresh_controls()
            return
        try:
            url = self._resolve_bridge_url(for_unlock=False)
        except Exception as exc:
            messagebox.showwarning("Thiếu thông tin kết nối", str(exc), parent=self.root)
            return
        self.set_toast("Đang tắt TinyPrintBridge trên điện thoại…", STATUS_AMBER)

        def worker() -> None:
            try:
                bridge = printer_ble.HttpBridgePrinter(url)
                bridge.shutdown_phone_bridge()
                self.post_event("bridge_stopped", None)
            except Exception as exc:
                self.post_event("job_err", str(exc))

        self._job_thread = threading.Thread(target=worker, name="pv-bridge-stop", daemon=True)
        self._job_thread.start()
        self._refresh_controls()

    def _ev_bridge_stopped(self, _payload: Any) -> None:
        self._job_thread = None
        self._bridge_connected = False
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.var_sidebar_status.set("○ X5: Đã tắt App ĐT")
        self.lbl_sidebar_status.configure(bg="#EAEAEF", fg=FG_MUTED)
        self.var_printer_detail.set("Trạng thái: Đã tắt hẳn TinyPrintBridge trên điện thoại (ngắt BLE & đóng cổng 9100).")
        self.set_toast("Đã tắt hẳn TinyPrintBridge trên điện thoại!", STATUS_GREEN)
        self._refresh_controls()

    # ------------------------------------------------------------------ Vault Lock & Print

    def _on_vault_drop(self, event: Any) -> None:
        try:
            paths = self.root.tk.splitlist(event.data)
        except tk.TclError:
            paths = [str(event.data)]
        if paths:
            self._set_vault_source(str(paths[0]))

    def pick_vault_source(self) -> None:
        path = filedialog.askopenfilename(title="Chọn file bất kỳ cần khóa (MB / GB)")
        if path:
            self._set_vault_source(path)

    def _set_vault_source(self, path: str) -> None:
        abs_p = os.path.abspath(path)
        if not os.path.isfile(abs_p):
            return
        self._vault_src_path = abs_p
        sz = os.path.getsize(abs_p)
        self.var_vault_src.set(f"{os.path.basename(abs_p)}  ({format_bytes(sz)})")
        self.set_toast(f"Đã chọn {os.path.basename(abs_p)} ({format_bytes(sz)}) — bấm Mã Hóa để tạo thẻ 8.4 cm.")
        self._refresh_controls()

    def start_vault_lock(self) -> None:
        src = self._vault_src_path
        if not src or self._busy():
            return
        out_path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Chọn nơi lưu file mã hóa (.pvvault)",
            initialdir=os.path.dirname(src),
            initialfile=os.path.basename(src) + ".pvvault",
            defaultextension=".pvvault",
            filetypes=[("PAPER-VOID Vault", "*.pvvault"), ("All files", "*.*")],
        )
        if not out_path:
            return

        passphrase = self.var_lock_pw.get()
        self._job_cancel.clear()
        self.progress.configure(mode="determinate", maximum=100, value=0)
        self.set_toast("Đang mã hóa từng khối 1 MB (AES-256-GCM)…", ACCENT_BLUE)

        def worker() -> None:
            try:
                res = vault.lock_file_streaming(
                    src,
                    out_path,
                    passphrase=passphrase,
                    chunk_size=vault.DEFAULT_CHUNK_SIZE,
                    cell_size=6,
                    progress_callback=lambda d, t: self.post_event("progress", ("Mã hóa", d, t)),
                    cancel_event=self._job_cancel,
                )
                self.post_event("lock_done", (src, res))
            except vault.VaultCancelledError as exc:
                self.post_event("job_cancelled", str(exc))
            except Exception as exc:
                self.post_event("job_err", str(exc))

        self._job_thread = threading.Thread(target=worker, name="pv-lock", daemon=True)
        self._job_thread.start()
        self._refresh_controls()

    def _ev_progress(self, payload: tuple[str, int, int]) -> None:
        label, done, total = payload
        pct = (100.0 * done / max(1, total)) if total > 0 else 100.0
        self.progress.configure(maximum=100, value=pct)
        if label in ("Mã hóa", "Giải mã"):
            self.set_toast(f"{label}: {format_bytes(done)} / {format_bytes(total)} ({pct:.1f}%)", ACCENT_BLUE)
        else:
            self.set_toast(f"{label}: {done}/{total} dòng ({pct:.0f}%)", ACCENT_BLUE)

    def _ev_lock_done(self, payload: tuple[str, vault.VaultLockResult]) -> None:
        src, res = payload
        self._job_thread = None
        self.progress.configure(value=100)
        self._vault_items.insert(0, VaultSessionItem(source_path=src, result=res))
        self.var_lock_pw.set("")
        self._render_vault_rows()
        self.set_toast(
            f"Đã khóa {res.filename} ({format_bytes(res.orig_size)}) → Thẻ {res.card_length_cm:.2f} cm sẵn sàng in!",
            STATUS_GREEN,
        )
        self._refresh_controls()

    def preview_vault_item(self, idx: int) -> None:
        if not (0 <= idx < len(self._vault_items)):
            return
        res = self._vault_items[idx].result
        win = printer_ble.show_ram_preview(
            res.card_image,
            parent=self.root,
            title=f"PAPER-VOID Key Card — {res.filename} ({res.key_record.vault_id_hex})",
            tile_count=1,
            cell_size=6,
        )
        self._preview_windows.append(win)

    def print_vault_item(self, idx: int, copies: int = 1) -> None:
        if self._busy() or not (0 <= idx < len(self._vault_items)):
            return
        mode = self.var_conn_mode.get()
        tailscale_ip = self.var_tailscale_ip.get()
        ble_mac = self.var_ble_mac.get()
        pname = self.var_printer_name.get().strip() or "X5"

        item = self._vault_items[idx]
        energy = int(self.var_energy.get())
        speed = int(self.var_speed.get())
        self._job_cancel.clear()
        self.progress.configure(mode="determinate", maximum=100, value=0)

        target_desc = f"trực tiếp PC BLE ({ble_mac})" if mode in ("direct_ble", "ble") else "qua app điện thoại"
        self.set_toast(f"Đang in {copies} thẻ khóa (~8.4 cm) {target_desc}…", ACCENT_BLUE)

        def worker() -> None:
            img_copy = np.ascontiguousarray(item.result.card_image.copy())
            try:
                printer = printer_ble.create_vault_printer(
                    mode=mode,
                    tailscale_ip=tailscale_ip,
                    ble_address=ble_mac,
                    printer_name=pname,
                )
                total_rows = int(img_copy.shape[0]) * copies
                for c_idx in range(copies):
                    if self._job_cancel.is_set():
                        raise printer_ble.PrintCancelledError("Đã hủy lệnh in.")
                    off = c_idx * int(img_copy.shape[0])
                    printer.print_ribbon_sync(
                        img_copy,
                        energy=energy,
                        speed=speed,
                        band_rows=64,
                        band_cooldown_s=0.28,
                        progress_callback=lambda s, _t, o=off: self.post_event("progress", ("In X5", o + s, total_rows)),
                        cancel_event=self._job_cancel,
                        status_callback=lambda m: self.post_event("toast", (m, ACCENT_BLUE)),
                    )
                    if c_idx + 1 < copies:
                        self.post_event("toast", ("Nghỉ mát đầu in 3 giây trước khi in bản dự phòng #2…", STATUS_AMBER))
                        for _ in range(30):
                            if self._job_cancel.is_set():
                                raise printer_ble.PrintCancelledError("Đã hủy trước bản #2.")
                            time.sleep(0.1)
                self.post_event("print_done", (idx, copies))
            except printer_ble.PrintCancelledError as exc:
                self.post_event("job_cancelled", str(exc))
            except Exception as exc:
                self.post_event("job_err", str(exc))
            finally:
                zeroize(img_copy)

        self._job_thread = threading.Thread(target=worker, name="pv-print", daemon=True)
        self._job_thread.start()
        self._refresh_controls()

    def print_test_strip(self) -> None:
        """Print a quick 3-cm test strip directly or via phone bridge to verify printer."""
        if self._busy():
            return
        mode = self.var_conn_mode.get()
        tailscale_ip = self.var_tailscale_ip.get()
        ble_mac = self.var_ble_mac.get()
        pname = self.var_printer_name.get().strip() or "X5"
        energy = int(self.var_energy.get())
        speed = int(self.var_speed.get())

        self._job_cancel.clear()
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.set_toast("Đang in mẫu thử nghiệm X5…", ACCENT_BLUE)

        def worker() -> None:
            from PIL import ImageDraw
            import datetime
            test_img = Image.new("L", (384, 130), 255)
            draw = ImageDraw.Draw(test_img)
            draw.rectangle([6, 6, 378, 124], outline=0, width=2)
            draw.text((16, 16), "PAPER-VOID -- X5 TEST STRIP", fill=0)
            draw.text((16, 46), f"Mode: {mode.upper()} | Name: {pname}", fill=0)
            draw.text((16, 76), f"Date: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", fill=0)
            draw.text((16, 102), "Status: 203 DPI Thermal Link OK", fill=0)
            img_arr = np.array(test_img, dtype=np.uint8)

            try:
                printer = printer_ble.create_vault_printer(
                    mode=mode,
                    tailscale_ip=tailscale_ip,
                    ble_address=ble_mac,
                    printer_name=pname,
                )
                printer.print_ribbon_sync(
                    img_arr,
                    energy=energy,
                    speed=speed,
                    feed_lines=48,
                    cancel_event=self._job_cancel,
                    status_callback=lambda m: self.post_event("toast", (m, ACCENT_BLUE)),
                )
                self.post_event("toast", ("In mẫu thử nghiệm X5 thành công!", STATUS_GREEN))
            except Exception as exc:
                self.post_event("job_err", str(exc))

        self._job_thread = threading.Thread(target=worker, name="pv-test-print", daemon=True)
        self._job_thread.start()
        self._refresh_controls()

    def _ev_print_done(self, payload: tuple[int, int]) -> None:
        idx, copies = payload
        self._job_thread = None
        self.progress.configure(value=100)
        if 0 <= idx < len(self._vault_items):
            self._vault_items[idx].printed_copies += copies
            self._render_vault_rows()
        self.set_toast(f"Đã in xong {copies} bản Thẻ Khóa trên X5!", STATUS_GREEN)
        self._refresh_controls()

    def shred_vault_item(self, idx: int) -> None:
        if not (0 <= idx < len(self._vault_items)):
            return
        item = self._vault_items[idx]
        if self._confirm_and_shred(item.source_path):
            item.shredded = True
            self._render_vault_rows()

    # ------------------------------------------------------------------ Vault Unlock

    def pick_unlock_vault(self) -> None:
        path = filedialog.askopenfilename(
            title="Chọn file .pvvault cần mở khóa",
            filetypes=[("PAPER-VOID Vault", "*.pvvault"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            hdr = vault.inspect_vault_header(path)
        except Exception as exc:
            messagebox.showerror("File Vault không hợp lệ", str(exc), parent=self.root)
            return
        self._unlock_vault_path = os.path.abspath(path)
        self._unlock_hdr = hdr
        twofa = "CÓ 2FA" if hdr.has_passphrase else "Không 2FA"
        self.var_unlock_vault.set(
            f"{os.path.basename(path)}  |  ID: {hdr.vault_id_hex}  |  {format_bytes(hdr.orig_size)}  |  {twofa}"
        )
        self.set_toast(f"Đã nạp vault {hdr.vault_id_hex} ({twofa}).")
        self._refresh_controls()

    def start_phone_live_scan(self) -> None:
        """Open dedicated ScannerActivity on Android phone (USB ADB or Tailscale) and live-decode."""
        if self._busy():
            return
        try:
            url = self._resolve_bridge_url(for_unlock=True)
        except Exception as exc:
            messagebox.showwarning("Thiếu thông tin kết nối", str(exc), parent=self.root)
            return

        self._job_cancel.clear()
        self.var_unlock_key_status.set("Đang mở App quét mã trên điện thoại…")
        self.set_toast("Đang mở màn hình quét mã trên điện thoại… Đưa thẻ khóa vào khung!", ACCENT_BLUE)
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)

        def worker() -> None:
            bridge = printer_ble.HttpBridgePrinter(url)
            try:
                import cv2
                import scanner

                bridge.start_phone_scanner(
                    status_callback=lambda m: self.post_event("toast", (m, ACCENT_BLUE))
                )
                self.post_event("toast", ("App quét mã đã mở trên ĐT — đưa Thẻ Khóa vào khung bo viền!", ACCENT_BLUE))

                last_seq = -1
                seen_active = False
                deadline = time.monotonic() + 120.0
                while not self._job_cancel.is_set() and time.monotonic() < deadline:
                    jpg, seq, active = bridge.fetch_scanner_frame(timeout=3.0)
                    if active:
                        seen_active = True
                    elif seen_active:
                        self.post_event("job_cancelled", "Đã đóng màn hình quét trên điện thoại.")
                        return

                    if jpg and seq != last_seq:
                        last_seq = seq
                        arr = np.frombuffer(jpg, dtype=np.uint8)
                        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                        if frame is not None and frame.size > 0:
                            res = scanner.decode_frame(frame, draw_overlay=False)
                            for tile in res.decoded_tiles:
                                if tile.payload.startswith(b"PVK1"):
                                    rec = vault.unpack_key_record(tile.payload)
                                    if rec is not None:
                                        bridge.ack_phone_scanner(rec.vault_id_hex)
                                        self.post_event("phone_scan_ok", (rec, tile.rs_errors_corrected))
                                        return
                    time.sleep(0.14)

                bridge.stop_phone_scanner()
                self.post_event("job_cancelled", "Đã dừng quét từ điện thoại.")
            except Exception as exc:
                self.post_event("job_err", str(exc))

        self._job_thread = threading.Thread(target=worker, name="pv-phone-scan", daemon=True)
        self._job_thread.start()
        self._refresh_controls()

    def _ev_phone_scan_ok(self, payload: tuple[vault.PaperKeyRecord, int]) -> None:
        rec, rs_err = payload
        self._job_thread = None
        self.progress.stop()
        self.progress.configure(mode="determinate", value=100)
        self._unlock_scanned_key = rec
        self.var_unlock_key_status.set(f"✔ Đã nhận mã từ ĐT: ID {rec.vault_id_hex} (RS sửa {rs_err}B)")
        self.set_toast(f"✔ ĐÃ NHẬN MÃ THẺ từ điện thoại (ID: {rec.vault_id_hex})!", STATUS_GREEN)
        self._refresh_controls()

    def scan_unlock_key_media(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Chọn ảnh chụp (.jpg/.png) hoặc video (.mp4/.mov) Thẻ Khóa",
            filetypes=[
                ("Ảnh & Video điện thoại", "*.jpg *.jpeg *.png *.webp *.bmp *.mp4 *.mov *.avi *.mkv"),
                ("All files", "*.*"),
            ],
        )
        if not paths:
            return
        self.var_unlock_key_status.set("Đang đọc thẻ khóa từ ảnh/video…")
        self.set_toast("Đang giải mã thẻ khóa từ ảnh điện thoại…", ACCENT_BLUE)

        def worker() -> None:
            try:
                import scanner

                tile = scanner.decode_key_card_from_media(paths)
                if tile is None:
                    self.post_event("key_scan_done", (None, 0))
                    return
                rec = vault.unpack_key_record(tile.payload)
                self.post_event("key_scan_done", (rec, tile.rs_errors_corrected))
            except Exception as exc:
                self.post_event("job_err", str(exc))

        threading.Thread(target=worker, name="pv-key-scan", daemon=True).start()

    def _ev_key_scan_done(self, payload: tuple[Optional[vault.PaperKeyRecord], int]) -> None:
        rec, rs_err = payload
        if rec is None:
            self.var_unlock_key_status.set("✘ Không đọc được khung PVK1 — hãy chụp rõ 4 góc hoặc gõ 3 hàng Base32.")
            self.set_toast("Không tìm thấy mã thẻ trong ảnh — thử gõ tay 3 hàng Base32.", STATUS_AMBER)
            return
        self._unlock_scanned_key = rec
        self.var_unlock_key_status.set(f"✔ Đã đọc thẻ ID {rec.vault_id_hex} (RS sửa {rs_err}B)")
        self.set_toast(f"Đã nhận thẻ khóa {rec.vault_id_hex}!", STATUS_GREEN)

    def start_vault_unlock(self) -> None:
        vpath = self._unlock_vault_path
        if not vpath or self._busy():
            return
        code_txt = self.var_unlock_base32.get().strip()
        key_rec = self._unlock_scanned_key
        if key_rec is None and not code_txt:
            messagebox.showwarning("Thiếu khóa", "Hãy quét ảnh thẻ khóa hoặc gõ 3 hàng mã Base32 cứu hộ.", parent=self.root)
            return
        out_dir = filedialog.askdirectory(title="Chọn thư mục lưu file gốc sau khi giải mã")
        if not out_dir:
            return

        passphrase = self.var_unlock_pw.get()
        self._job_cancel.clear()
        self.progress.configure(mode="determinate", maximum=100, value=0)
        self.set_toast("Đang giải mã từng khối 1 MB & xác minh SHA-256…", ACCENT_BLUE)

        def worker() -> None:
            try:
                res = vault.unlock_file_streaming(
                    vpath,
                    out_dir,
                    key_record=key_rec if not code_txt else None,
                    recovery_code=code_txt or None,
                    passphrase=passphrase,
                    progress_callback=lambda d, t: self.post_event("progress", ("Giải mã", d, t)),
                    cancel_event=self._job_cancel,
                )
                self.post_event("unlock_done", res)
            except vault.VaultCancelledError as exc:
                self.post_event("job_cancelled", str(exc))
            except Exception as exc:
                self.post_event("job_err", str(exc))

        self._job_thread = threading.Thread(target=worker, name="pv-unlock", daemon=True)
        self._job_thread.start()
        self._refresh_controls()

    def _ev_unlock_done(self, res: vault.VaultUnlockResult) -> None:
        self._job_thread = None
        self.progress.configure(value=100)
        self.var_unlock_pw.set("")
        self.set_toast(f"Khôi phục thành công: {res.original_filename} (SHA-256 khớp 100%)", STATUS_GREEN)
        self._refresh_controls()
        messagebox.showinfo(
            "Mở khóa thành công",
            f"Đã khôi phục nguyên vẹn:\n{res.output_path}\n\n"
            f"Kích thước: {format_bytes(res.orig_size)}\nSHA-256:\n{res.sha256_hex}",
            parent=self.root,
        )

    # ------------------------------------------------------------------ Standalone Shredder

    def _on_shred_drop(self, event: Any) -> None:
        try:
            paths = self.root.tk.splitlist(event.data)
        except tk.TclError:
            paths = [str(event.data)]
        self._append_shred_paths([str(p) for p in paths])

    def add_shred_files(self) -> None:
        paths = filedialog.askopenfilenames(title="Chọn file cần hủy diệt (SHRED)")
        if paths:
            self._append_shred_paths(list(paths))

    def _append_shred_paths(self, paths: list[str]) -> None:
        added = 0
        for p in paths:
            ap = os.path.abspath(p)
            if os.path.isfile(ap) and ap not in self._shred_queue:
                self._shred_queue.append(ap)
                added += 1
        if added:
            self._render_shred_rows()
            self.set_toast(f"Đã thêm {added} file vào hàng chờ SHRED.")

    def clear_shred_queue(self) -> None:
        self._shred_queue.clear()
        self._render_shred_rows()

    def _remove_shred_index(self, idx: int) -> None:
        if 0 <= idx < len(self._shred_queue):
            self._shred_queue.pop(idx)
            self._render_shred_rows()

    def _execute_shred_from_queue(self, path: str) -> None:
        if self._confirm_and_shred(path):
            if path in self._shred_queue:
                self._shred_queue.remove(path)
            self._render_shred_rows()

    def _confirm_and_shred(self, path: str) -> bool:
        if not os.path.isfile(path):
            return False
        name = os.path.basename(path)
        ok1 = messagebox.askyesno(
            "Hủy diệt file — Bước 1/2",
            f"Bạn sắp ghi đè 4 lượt và XÓA VĨNH VIỄN (bỏ qua Thùng rác):\n\n{path}\n\n"
            "Hãy chắc chắn bạn đã in Thẻ Khóa và giữ file .pvvault!\nTiếp tục?",
            icon=messagebox.WARNING,
            default=messagebox.NO,
            parent=self.root,
        )
        if not ok1:
            return False
        if not self._ask_typed_shred(name):
            return False
        try:
            codec.secure_shred_file(path)
        except Exception as exc:
            messagebox.showerror("Hủy diệt thất bại", str(exc), parent=self.root)
            return False
        self.set_toast(f"Đã hủy diệt vĩnh viễn: {name}", STATUS_GREEN)
        return True

    def _ask_typed_shred(self, name: str) -> bool:
        dlg = tk.Toplevel(self.root)
        dlg.title("Xác nhận cuối — Bước 2/2")
        dlg.configure(bg=BG_CARD)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        result = {"ok": False}

        tk.Label(
            dlg,
            text=f"Gõ chính xác  {SHRED_CONFIRM_WORD}  để hủy diệt:\n{name}",
            bg=BG_CARD, fg=DANGER_RED, font=("Segoe UI", 10, "bold"),
            justify=tk.CENTER, padx=24, pady=14,
        ).pack()

        var = tk.StringVar()
        ent = tk.Entry(
            dlg, textvariable=var, width=22, justify=tk.CENTER,
            bg=BG_INPUT, fg=FG_PRIMARY, relief=tk.FLAT,
            highlightbackground=BORDER_SUBTLE, highlightthickness=1, font=("Consolas", 11, "bold"),
        )
        ent.pack(pady=4, ipady=4)

        row = tk.Frame(dlg, bg=BG_CARD)
        row.pack(pady=(10, 16))
        btn_ok = tk.Button(
            row, text="HỦY DIỆT", bg=DANGER_RED, fg="#FFFFFF",
            activebackground="#B91C1C", activeforeground="#FFFFFF",
            relief=tk.FLAT, font=("Segoe UI", 9, "bold"), padx=14, pady=5, state=tk.DISABLED,
        )

        def on_change(*_a: Any) -> None:
            btn_ok.configure(state=tk.NORMAL if var.get() == SHRED_CONFIRM_WORD else tk.DISABLED)

        def on_ok(*_a: Any) -> None:
            if var.get() == SHRED_CONFIRM_WORD:
                result["ok"] = True
                dlg.destroy()

        var.trace_add("write", on_change)
        btn_ok.configure(command=on_ok)
        btn_ok.pack(side=tk.LEFT, padx=6)
        tk.Button(
            row, text="Hủy bỏ", command=dlg.destroy,
            bg="#ECECF0", fg=FG_SECONDARY, relief=tk.FLAT, font=("Segoe UI", 9), padx=12, pady=5,
        ).pack(side=tk.LEFT, padx=6)

        ent.bind("<Return>", on_ok)
        ent.bind("<Escape>", lambda _e: dlg.destroy())
        dlg.grab_set()
        ent.focus_set()
        self.root.wait_window(dlg)
        return bool(result["ok"])

    # ------------------------------------------------------------------ Common Job Events & Close

    def _ev_job_cancelled(self, message: str) -> None:
        self._job_thread = None
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.set_toast(message, STATUS_AMBER)
        self._refresh_controls()

    def _ev_job_err(self, message: str) -> None:
        self._job_thread = None
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.set_toast(f"Lỗi: {message[:85]}", DANGER_RED)
        self._refresh_controls()
        messagebox.showerror("Lỗi PAPER-VOID", message, parent=self.root)

    def on_close(self) -> None:
        self._closing = True
        self._job_cancel.set()
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except tk.TclError:
                pass
        for item in self._vault_items:
            zeroize(item.result.card_image)
        for win in list(self._preview_windows):
            try:
                win.destroy()
            except tk.TclError:
                pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    PaperVoidApp().run()


if __name__ == "__main__":
    main()
