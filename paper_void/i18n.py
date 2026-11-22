"""
PAPER-VOID Internationalization (i18n) Module.
Supports English (en), Vietnamese (vi), and Simplified Chinese (zh).
Auto-detects OS language and persists user preference.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

SETTINGS_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "PAPER-VOID"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "app_title": "PAPER-VOID — Thermal Paper-Key File Vault",
        "nav_vault": "Lock File (Vault)",
        "nav_unlock": "Unlock (Decrypt)",
        "nav_shred": "Shred File",
        "nav_printer": "X5 Printer (Phone)",
        "sidebar_status_disconnected": "○ X5: Phone not connected",
        "sidebar_status_connected": "● X5: Connected ({})",
        "btn_turn_on_bridge": "⚡ Start Phone App & Connect X5",
        "btn_turn_off_bridge": "⏹ Stop Phone App",
        "sidebar_tagline": "Hybrid AES-256-GCM\n1 Key Card ~8.4 cm",
        "btn_cancel_job": "Cancel Job",
        "toast_ready": "Ready — Hybrid Paper-Key Vault (1 ~8.4 cm card for any MB/GB file)",
        "toast_cancelled": "Task cancelled by user.",
        "toast_done": "Operation completed successfully.",
        "toast_error": "Error: {}",
        "toast_stopped_bridge": "Phone bridge and background service stopped.",
        "toast_connecting_bridge": "Connecting to phone bridge at {}...",
        "toast_connected_bridge": "Connected to phone app & X5 printer ({})!",
        "toast_locking": "Encrypting: {} ({})...",
        "toast_unlocking": "Decrypting: {}...",
        "toast_scanning_phone": "Waiting for phone live camera scanner (press Cancel to abort)...",
        "toast_scan_success": "Key Card verified: {} (Algorithm: {})",
        "toast_shredding": "Shredding: {}...",
        "toast_printing": "Sending Key Card to X5 printer (Energy: {}, Speed: {})...",
        "toast_print_done": "Key Card printed successfully!",

        # Vault Page
        "vault_page_title": "Paper-Key Vault",
        "vault_page_subtitle": "Stream-encrypt 1 MB/chunk (AES-256-GCM) → Print exactly 1 thermal Key Card (~8.4 cm) on X5",
        "vault_choose_file": "+ Choose File (MB / GB)",
        "vault_drop_hint": "Or drag and drop any file here (documents, videos, databases, zip, etc.)",
        "vault_file_selected": "File selected: {} ({})",
        "vault_2fa_label": "2FA Passphrase (optional):",
        "vault_2fa_placeholder": "Leave empty for 1-factor paper key only; or type a password for 2-factor security",
        "vault_btn_lock": "🔒 Encrypt 1 MB/chunk → Generate 8.4 cm Key Card",
        "vault_table_title": "Files Locked In This Session (Encrypted Containers & Key Cards)",
        "vault_th_filename": "File & Vault Name",
        "vault_th_size": "Size",
        "vault_th_status": "Card Status",
        "vault_th_actions": "Controls (Print & SHRED)",
        "vault_empty_history": "No files locked yet in this session.\nChoose a file above to encrypt it and generate a thermal Key Card.",
        "vault_status_card_ok": "Card: 384×670 px OK",
        "vault_btn_view": "👁 View",
        "vault_btn_print1": "🖨 Print 1",
        "vault_btn_print2": "🖨 Print 2",
        "vault_btn_shred": "🗑 SHRED",

        # Unlock Page
        "unlock_page_title": "Unlock Vault",
        "unlock_page_subtitle": "Select .pvvault file → Load Key Card via phone live camera / photo OR enter 3 lines of Base32 code",
        "unlock_step1": "Step 1 — Choose encrypted file (.pvvault)",
        "unlock_btn_browse_vault": "Choose .pvvault file…",
        "unlock_no_vault": "No .pvvault file selected",
        "unlock_step2": "Step 2 — Scan Key Card via Phone App (or Photo / Base32)",
        "unlock_conn_label": "Phone connection:",
        "unlock_conn_usb": "🔌 Via USB Cable (ADB)",
        "unlock_conn_tailscale": "🌐 Via Tailscale (Phone IP):",
        "unlock_tailscale_hint": " (e.g. 100.x.y.z)",
        "unlock_btn_scan_phone": "📱 Open Scanner App on Phone",
        "unlock_btn_browse_photo": "📷 Or Select Photo / Video",
        "unlock_key_unscanned": "Key card not scanned yet",
        "unlock_key_loaded": "✔ Key loaded: {}",
        "unlock_or_manual": "Or type the 3 lines of Base32 code printed on the card (11 groups × 5 chars, case-insensitive):",
        "unlock_2fa_label": "2FA Passphrase (if configured):",
        "unlock_btn_decrypt": "🔓 Decrypt 1 MB/chunk & Restore File",

        # Shred Page
        "shred_page_title": "Secure File Shredder (Permanent Destruction)",
        "shred_page_subtitle": "4-pass overwrite (random → 0xFF → random → 0x00) + fsync + truncate + double rename + bypass Recycle Bin",
        "shred_ssd_note": "⚠ SSD/NVMe note: Overwriting prevents recovery via Recycle Bin & Recuva, but full-disk BitLocker is recommended for wear-leveled SSDs.",
        "shred_btn_add": "+ Add Files to Shred…",
        "shred_btn_clear": "Clear Queue",
        "shred_drop_hint": "Or drag and drop files here to queue for SHREDDING",
        "shred_empty_queue": "Shred queue is empty. Every SHRED action requires 2-step confirmation (typing SHRED).",
        "shred_btn_action": "🗑 SHRED NOW",
        "shred_btn_remove": "Remove",
        "shred_not_exist": "File no longer exists",

        # Printer Page
        "printer_page_title": "Thermal Printer X5 (Phone Bridge)",
        "printer_page_subtitle": "One-click ADB forward, start TinyPrintBridge app on phone, and verify X5 printer connection",
        "printer_conn_section": "Phone Bridge Connection (USB / Tailscale)",
        "printer_status_disconnected": "Status: Not connected — click the button above to launch app on phone.",
        "printer_conn_direct_ble": "⚡ Direct PC Bluetooth (BLE)",
        "printer_ble_mac_label": "BLE MAC:",
        "printer_btn_scan_ble": "🔍 Scan BLE",
        "printer_btn_connect_direct": "⚡ Connect X5 (PC BLE)",
        "printer_btn_disconnect": "✔ Connected (Click to Disconnect)",
        "printer_btn_connect_bridge": "📱 Launch Phone & Connect X5",
        "printer_conn_usb": "🔌 USB Cable (127.0.0.1:9100)",
        "printer_conn_tailscale": "🌐 Tailscale IP:",
        "printer_name_label": "Printer Bluetooth Name:",
        "printer_head_settings": "Thermal Printhead Parameters (gentle 64 rows/burst with 0.28s cooldown)",
        "printer_energy_label": "Print Energy (Darkness):",
        "printer_speed_label": "Feed Speed:",
        "printer_btn_test": "🖨 Print Test Strip (X5)",

        # Dialogs & Prompts
        "dialog_confirm": "Confirm",
        "dialog_warning": "Warning",
        "dialog_error": "Error",
        "dialog_info": "Information",
        "dialog_missing_conn_title": "Missing Connection Info",
        "dialog_missing_key_title": "Missing Key",
        "dialog_missing_key_msg": "Please scan the Key Card or enter the 3 lines of Base32 recovery code.",
        "dialog_invalid_vault_title": "Invalid Vault File",
        "dialog_pick_src_file": "Select any file to lock (MB / GB)",
        "dialog_save_vault": "Select destination for encrypted vault (.pvvault)",
        "dialog_pick_vault_file": "Select .pvvault file to unlock",
        "dialog_pick_card_media": "Select Key Card photo (.jpg/.png) or video (.mp4/.mov)",
        "dialog_save_restored_dir": "Select output directory for restored file",
        "dialog_pick_shred_files": "Select files to permanently SHRED",
        "dialog_shred_confirm_step1": "Are you sure you want to PERMANENTLY DESTROY this file?\n\n{}\n\nData will be completely unrecoverable!",
        "dialog_shred_title": "⚠ PERMANENT SHRED CONFIRMATION",
        "dialog_shred_prompt": "You are about to PERMANENTLY DESTROY:\n\n{}\n\nData CANNOT BE RECOVERED after shredding.\nType '{}' to confirm destruction:",
        "dialog_shred_failed": "Shredding failed: {}",
        "dialog_shred_success": "Permanently shredded {} file(s) successfully.",
        "dialog_unlock_success": "Decryption complete!\nRestored file: {}\nSaved to: {}",
        "dialog_lock_success": "File locked successfully!\nEncrypted container: {}\nKey Card generated (~8.4 cm).",
    },

    "vi": {
        "app_title": "PAPER-VOID — Két Khóa Giấy Nhiệt Vật Lý",
        "nav_vault": "Khóa File (Vault)",
        "nav_unlock": "Mở Khóa (Unlock)",
        "nav_shred": "Hủy File (Shred)",
        "nav_printer": "Máy In X5 (ĐT)",
        "sidebar_status_disconnected": "○ X5: Chưa kết nối ĐT",
        "sidebar_status_connected": "● X5: Đã kết nối ({})",
        "btn_turn_on_bridge": "⚡ Bật App ĐT & Nối X5",
        "btn_turn_off_bridge": "⏹ Tắt App ĐT",
        "sidebar_tagline": "Hybrid AES-256-GCM\n1 Thẻ Khóa ~8.4 cm",
        "btn_cancel_job": "Hủy tác vụ",
        "toast_ready": "Sẵn sàng — Chỉ dùng Paper-Key Vault (1 thẻ ~8.4 cm cho mọi file MB/GB)",
        "toast_cancelled": "Tác vụ đã bị người dùng hủy.",
        "toast_done": "Thao tác hoàn tất thành công.",
        "toast_error": "Lỗi: {}",
        "toast_stopped_bridge": "Đã dừng app cầu nối và service trên điện thoại.",
        "toast_connecting_bridge": "Đang kết nối tới cầu nối tại {}...",
        "toast_connected_bridge": "Đã kết nối app điện thoại & máy in X5 ({})!",
        "toast_locking": "Đang khóa file: {} ({})...",
        "toast_unlocking": "Đang giải mã: {}...",
        "toast_scanning_phone": "Đang chờ quét thẻ từ app điện thoại (bấm Hủy để thoát)...",
        "toast_scan_success": "Đã nhận diện Thẻ Khóa: {} (Thuật toán: {})",
        "toast_shredding": "Đang hủy file an toàn: {}...",
        "toast_printing": "Đang in Thẻ Khóa ra máy in X5 (Nhiệt: {}, Tốc độ: {})...",
        "toast_print_done": "In Thẻ Khóa thành công!",

        # Vault Page
        "vault_page_title": "Paper-Key Vault",
        "vault_page_subtitle": "Mã hóa từng khối 1 MB (AES-256-GCM) → In duy nhất 1 Thẻ Khóa ~8.4 cm trên máy in nhiệt X5",
        "vault_choose_file": "+ Chọn File (MB / GB)",
        "vault_drop_hint": "Hoặc kéo-thả file bất kỳ vào đây (văn bản, video, database, zip...)",
        "vault_file_selected": "Đã chọn file: {} ({})",
        "vault_2fa_label": "Mật khẩu 2FA (tùy chọn):",
        "vault_2fa_placeholder": "Bỏ trống nếu chỉ dùng thẻ giấy; hoặc gõ mật khẩu để bảo vệ 2 lớp",
        "vault_btn_lock": "🔒 Mã Hóa 1 MB/khối → Tạo Thẻ 8.4 cm",
        "vault_table_title": "Danh Sách File Đã Khóa Trong Phiên (Container Mã Hóa & Thẻ Khóa)",
        "vault_th_filename": "Tên File & Vault",
        "vault_th_size": "Dung Lượng",
        "vault_th_status": "Trạng Thái Thẻ",
        "vault_th_actions": "Điều Khiển (In & SHRED)",
        "vault_empty_history": "Chưa có file nào được khóa trong phiên này.\nHãy chọn file ở trên để mã hóa và in Thẻ Khóa vật lý.",
        "vault_status_card_ok": "Thẻ: 384×670 px OK",
        "vault_btn_view": "👁 Xem",
        "vault_btn_print1": "🖨 In 1",
        "vault_btn_print2": "🖨 In 2",
        "vault_btn_shred": "🗑 SHRED",

        # Unlock Page
        "unlock_page_title": "Mở Khóa Vault",
        "unlock_page_subtitle": "Chọn file .pvvault → Nạp thẻ khóa từ Ảnh/Video điện thoại HOẶC gõ 3 hàng mã Base32 trong khung đen",
        "unlock_step1": "Bước 1 — Chọn file đã mã hóa (.pvvault)",
        "unlock_btn_browse_vault": "Chọn file .pvvault…",
        "unlock_no_vault": "Chưa chọn file .pvvault",
        "unlock_step2": "Bước 2 — Quét mã thẻ bằng App Điện Thoại (hoặc Ảnh / Base32)",
        "unlock_conn_label": "Kết nối ĐT:",
        "unlock_conn_usb": "🔌 Qua Cáp USB (ADB)",
        "unlock_conn_tailscale": "🌐 Qua Tailscale (IP ĐT):",
        "unlock_tailscale_hint": " (vd: 100.x.y.z)",
        "unlock_btn_scan_phone": "📱 Mở App Quét Mã Trên Điện Thoại",
        "unlock_btn_browse_photo": "📷 Hoặc Chọn Ảnh / Video",
        "unlock_key_unscanned": "Chưa quét thẻ khóa",
        "unlock_key_loaded": "✔ Đã nhận khóa: {}",
        "unlock_or_manual": "Hoặc gõ nối tiếp 3 hàng mã Base32 bên trong khung viền đen (11 cụm × 5 ký tự, không phân biệt hoa/thường):",
        "unlock_2fa_label": "Mật khẩu 2FA (nếu có):",
        "unlock_btn_decrypt": "🔓 Giải Mã 1 MB/khối & Khôi Phục File",

        # Shred Page
        "shred_page_title": "Hủy Diệt File (Secure Shred)",
        "shred_page_subtitle": "Ghi đè 4 lượt (urandom → 0xFF → urandom → 0x00) + fsync + truncate + đổi tên 2 lần + bỏ qua Thùng rác",
        "shred_ssd_note": "⚠ Lưu ý SSD/NVMe: Ghi đè chặn sạch Thùng rác & Recuva, nhưng SSD có wear-leveling nên nên bật BitLocker toàn ổ đĩa.",
        "shred_btn_add": "+ Thêm File Cần Hủy…",
        "shred_btn_clear": "Xóa danh sách chờ",
        "shred_drop_hint": "Hoặc kéo-thả file vào đây để đưa vào hàng chờ SHRED",
        "shred_empty_queue": "Hàng chờ hủy file đang trống. Mọi lệnh SHRED đều yêu cầu xác nhận 2 bước (gõ chữ SHRED).",
        "shred_btn_action": "🗑 SHRED NGAY",
        "shred_btn_remove": "Bỏ",
        "shred_not_exist": "Không còn tồn tại",

        # Printer Page
        "printer_page_title": "Máy In Nhiệt X5 (Cầu Nối Điện Thoại)",
        "printer_page_subtitle": "Bấm 1 nút để tự chạy ADB forward, mở app TinyPrintBridge trên điện thoại và kiểm tra kết nối máy in X5",
        "printer_conn_section": "Kết nối máy in nhiệt X5 (Bluetooth PC / Cầu nối điện thoại)",
        "printer_status_disconnected": "Trạng thái: Chưa kết nối — bấm nút bên dưới để ghép nối máy in X5.",
        "printer_conn_direct_ble": "⚡ Trực tiếp Bluetooth PC (BLE)",
        "printer_ble_mac_label": "Địa chỉ BLE:",
        "printer_btn_scan_ble": "🔍 Quét BLE",
        "printer_btn_connect_direct": "⚡ Ghép Nối X5 (PC BLE)",
        "printer_btn_disconnect": "✔ Đã Kết Nối (Bấm để ngắt)",
        "printer_btn_connect_bridge": "📱 Bật App ĐT & Nối X5",
        "printer_conn_usb": "🔌 Cáp USB (127.0.0.1:9100)",
        "printer_conn_tailscale": "🌐 Tailscale IP:",
        "printer_name_label": "Tên máy in:",
        "printer_head_settings": "Thông số đầu in nhiệt (đã tối ưu in từ từ 64 dòng/nhịp, nghỉ mát 0.28s)",
        "printer_energy_label": "Độ đậm nhiệt (Energy):",
        "printer_speed_label": "Tốc độ kéo giấy (Speed):",
        "printer_btn_test": "🖨 In Thẻ Mẫu Thử Nghiệm (X5)",

        # Dialogs & Prompts
        "dialog_confirm": "Xác nhận",
        "dialog_warning": "Cảnh báo",
        "dialog_error": "Lỗi",
        "dialog_info": "Thông báo",
        "dialog_missing_conn_title": "Thiếu thông tin kết nối",
        "dialog_missing_key_title": "Thiếu khóa",
        "dialog_missing_key_msg": "Hãy quét ảnh thẻ khóa hoặc gõ 3 hàng mã Base32 cứu hộ.",
        "dialog_invalid_vault_title": "File Vault không hợp lệ",
        "dialog_pick_src_file": "Chọn file bất kỳ cần khóa (MB / GB)",
        "dialog_save_vault": "Chọn nơi lưu file mã hóa (.pvvault)",
        "dialog_pick_vault_file": "Chọn file .pvvault cần mở khóa",
        "dialog_pick_card_media": "Chọn ảnh chụp (.jpg/.png) hoặc video (.mp4/.mov) Thẻ Khóa",
        "dialog_save_restored_dir": "Chọn thư mục lưu file gốc sau khi giải mã",
        "dialog_pick_shred_files": "Chọn file cần hủy diệt (SHRED)",
        "dialog_shred_confirm_step1": "Bạn có CHẮC CHẮN muốn tiêu hủy vĩnh viễn file này không?\n\n{}\n\nDữ liệu sẽ bị xóa hoàn toàn không thể khôi phục!",
        "dialog_shred_title": "⚠ XÁC NHẬN TIÊU HỦY VĨNH VIỄN",
        "dialog_shred_prompt": "Bạn sắp TIÊU HỦY VĨNH VIỄN:\n\n{}\n\nDữ liệu KHÔNG THỂ KHÔI PHỤC sau khi hủy.\nGõ '{}' để xác nhận tiêu hủy:",
        "dialog_shred_failed": "Hủy diệt thất bại: {}",
        "dialog_shred_success": "Đã hủy vĩnh viễn {} file an toàn.",
        "dialog_unlock_success": "Giải mã hoàn tất!\nFile gốc: {}\nLưu tại: {}",
        "dialog_lock_success": "Đã khóa file thành công!\nFile két mã hóa: {}\nThẻ Khóa in nhiệt đã tạo (dài: ~8.4 cm).",
    },

    "zh": {
        "app_title": "PAPER-VOID — 热敏纸质物理密钥文件保险箱",
        "nav_vault": "文件加密 (Vault)",
        "nav_unlock": "扫码解密 (Unlock)",
        "nav_shred": "安全粉碎 (Shred)",
        "nav_printer": "X5 打印机 (手机)",
        "sidebar_status_disconnected": "○ X5: 手机未连接",
        "sidebar_status_connected": "● X5: 已连接 ({})",
        "btn_turn_on_bridge": "⚡ 启动手机 App 并连接 X5",
        "btn_turn_off_bridge": "⏹ 关闭手机 App",
        "sidebar_tagline": "混合 AES-256-GCM\n1 张密钥卡 ~8.4 cm",
        "btn_cancel_job": "取消任务",
        "toast_ready": "就绪 — 纸质密钥保险箱（任意大小文件仅需 1 张 ~8.4 cm 实体纸卡）",
        "toast_cancelled": "用户已取消任务。",
        "toast_done": "操作成功完成。",
        "toast_error": "错误：{}",
        "toast_stopped_bridge": "已停止手机端桥接服务及后台进程。",
        "toast_connecting_bridge": "正在连接手机桥接服务：{}...",
        "toast_connected_bridge": "已成功连接手机 App 与 X5 打印机 ({})！",
        "toast_locking": "正在加密文件：{} ({})...",
        "toast_unlocking": "正在流式解密：{}...",
        "toast_scanning_phone": "等待手机实时扫码中（按取消退出）...",
        "toast_scan_success": "已成功识别密钥卡：{} (算法：{})",
        "toast_shredding": "正在执行安全粉碎：{}...",
        "toast_printing": "正在发送密钥卡至 X5 打印机 (浓度：{}，速度：{})...",
        "toast_print_done": "密钥卡打印完成！",

        # Vault Page
        "vault_page_title": "文件加密 — 纸质物理密钥保险箱",
        "vault_page_subtitle": "按 1 MB/块 流式 AES-256-GCM 加密任意文件 (.pvvault) → 打印 1 张约 8.4 cm 实体密钥卡",
        "vault_choose_file": "+ 选择加密文件 (MB / GB)",
        "vault_drop_hint": "或直接将任意文件拖放到此处（文档、视频、数据库、压缩包等）",
        "vault_file_selected": "已选文件：{} ({})",
        "vault_2fa_label": "2FA 双重口令（可选）：",
        "vault_2fa_placeholder": "留空则仅使用纸质实体密钥；输入密码则启用纸质+记忆双因素保护",
        "vault_btn_lock": "🔒 每块 1 MB 流式加密 → 生成 8.4 cm 纸卡",
        "vault_table_title": "本次会话已加密文件列表（密文容器与对应密钥卡）",
        "vault_th_filename": "文件名与容器",
        "vault_th_size": "文件大小",
        "vault_th_status": "卡片状态",
        "vault_th_actions": "操作控制 (打印与粉碎)",
        "vault_empty_history": "本次会话尚未加密任何文件。\n请在上方选择文件进行加密并生成实体热敏密钥卡。",
        "vault_status_card_ok": "纸卡：384×670 px 正常",
        "vault_btn_view": "👁 预览",
        "vault_btn_print1": "🖨 打印 1",
        "vault_btn_print2": "🖨 打印 2",
        "vault_btn_shred": "🗑 粉碎原文件",

        # Unlock Page
        "unlock_page_title": "解密文件 — 从热敏纸卡恢复原文件",
        "unlock_page_subtitle": "选择 .pvvault 文件 → 手机 App 实时扫码/图片导入 或 输入卡底 3 行 Base32 编码",
        "unlock_step1": "步骤 1 — 选择已加密文件 (.pvvault)",
        "unlock_btn_browse_vault": "选择 .pvvault 文件…",
        "unlock_no_vault": "尚未选择 .pvvault 文件",
        "unlock_step2": "步骤 2 — 使用手机 App 扫描纸卡（或照片 / Base32）",
        "unlock_conn_label": "手机连接方式：",
        "unlock_conn_usb": "🔌 USB 数据线 (ADB)",
        "unlock_conn_tailscale": "🌐 Tailscale (手机 IP)：",
        "unlock_tailscale_hint": " (例如 100.x.y.z)",
        "unlock_btn_scan_phone": "📱 启动手机扫码界面",
        "unlock_btn_browse_photo": "📷 或选择照片 / 视频文件",
        "unlock_key_unscanned": "尚未扫描密钥卡",
        "unlock_key_loaded": "✔ 已加载密钥：{}",
        "unlock_or_manual": "或直接输入黑框内的 3 行 Base32 编码（11 组 × 5 字符，不区分大小写）：",
        "unlock_2fa_label": "2FA 口令（如加密时已设置）：",
        "unlock_btn_decrypt": "🔓 流式解密 1 MB/块 并还原原文件",

        # Shred Page
        "shred_page_title": "安全文件粉碎器（物理覆写永久销毁）",
        "shred_page_subtitle": "4 次物理覆写 (随机数 → 0xFF → 随机数 → 0x00) + fsync 强制同步 + 截断 + 两次重命名 + 绕过回收站",
        "shred_ssd_note": "⚠ SSD 固态硬盘说明：覆写可彻底防止回收站及 Recuva 等常规恢复，配合系统 BitLocker 全盘加密效果最佳。",
        "shred_btn_add": "+ 添加待粉碎文件…",
        "shred_btn_clear": "清空等待列表",
        "shred_drop_hint": "或直接将待销毁文件拖拽到此处加入粉碎队列",
        "shred_empty_queue": "等待销毁队列为空。所有粉碎操作均需两步确认（手动输入 SHRED）。",
        "shred_btn_action": "🗑 立即粉碎",
        "shred_btn_remove": "移除",
        "shred_not_exist": "文件已不存在",

        # Printer Page
        "printer_page_title": "X5 热敏打印机（手机桥接端）",
        "printer_page_subtitle": "一键执行 ADB 端口转发，唤醒手机 TinyPrintBridge 服务并连接 X5 蓝牙打印机",
        "printer_conn_section": "X5 热敏打印机连接配置（PC蓝牙直连 / 手机桥接）",
        "printer_status_disconnected": "状态：未连接 — 点击下方按钮连接 X5 打印机。",
        "printer_conn_direct_ble": "⚡ PC蓝牙直连 (BLE)",
        "printer_ble_mac_label": "蓝牙MAC：",
        "printer_btn_scan_ble": "🔍 扫描蓝牙",
        "printer_btn_connect_direct": "⚡ 直连X5 (PC蓝牙)",
        "printer_btn_disconnect": "✔ 已连接 (点击断开)",
        "printer_btn_connect_bridge": "📱 启动手机App并连接X5",
        "printer_conn_usb": "🔌 USB 数据线 (127.0.0.1:9100)",
        "printer_conn_tailscale": "🌐 Tailscale IP：",
        "printer_name_label": "打印机蓝牙名称：",
        "printer_head_settings": "热敏打印头参数控制（已优化为每次走纸 64 行并降温 0.28 秒）",
        "printer_energy_label": "加热浓度 (Energy)：",
        "printer_speed_label": "走纸速度 (Speed)：",
        "printer_btn_test": "🖨 打印测试样张 (X5)",

        # Dialogs & Prompts
        "dialog_confirm": "确认",
        "dialog_warning": "警告",
        "dialog_error": "错误",
        "dialog_info": "提示",
        "dialog_missing_conn_title": "缺少连接信息",
        "dialog_missing_key_title": "未找到密钥",
        "dialog_missing_key_msg": "请先扫描纸质密钥卡或输入 3 行 Base32 应急恢复码。",
        "dialog_invalid_vault_title": "无效的 Vault 文件",
        "dialog_pick_src_file": "选择需加密锁定的任意文件 (MB / GB)",
        "dialog_save_vault": "选择加密容器保存路径 (.pvvault)",
        "dialog_pick_vault_file": "选择要解密的 .pvvault 文件",
        "dialog_pick_card_media": "选择密钥卡照片 (.jpg/.png) 或视频 (.mp4/.mov)",
        "dialog_save_restored_dir": "选择解密原文件保存目录",
        "dialog_pick_shred_files": "选择需永久粉碎的文件",
        "dialog_shred_confirm_step1": "您确定要【永久销毁】此文件吗？\n\n{}\n\n粉碎后数据绝对无法恢复！",
        "dialog_shred_title": "⚠ 永久粉碎确认",
        "dialog_shred_prompt": "您即将【永久销毁】以下文件：\n\n{}\n\n粉碎后数据将绝对无法恢复！\n请输入 '{}' 确认销毁：",
        "dialog_shred_failed": "粉碎失败：{}",
        "dialog_shred_success": "已成功永久粉碎 {} 个文件。",
        "dialog_unlock_success": "解密完成！\n已还原文件：{}\n保存路径：{}",
        "dialog_lock_success": "文件加密成功！\n密文容器：{}\n热敏实体密钥卡已生成（长度约 8.4 cm）。",
    }
}


def detect_default_language() -> str:
    """Detect default language: 1) saved setting 2) OS UI language 3) fallback to en."""
    # 1. Saved preference
    try:
        if SETTINGS_FILE.is_file():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            saved = data.get("language")
            if saved in STRINGS:
                return saved
    except Exception:
        pass

    # 2. Windows UI Language detection via Win32 API
    try:
        import ctypes
        windll = getattr(ctypes, "windll", None)
        if windll:
            lang_id = windll.kernel32.GetUserDefaultUILanguage()
            primary = lang_id & 0x3FF
            if primary == 0x2A:  # LANG_VIETNAMESE
                return "vi"
            elif primary == 0x04:  # LANG_CHINESE
                return "zh"
            elif primary == 0x09:  # LANG_ENGLISH
                return "en"
    except Exception:
        pass

    # 3. Standard locale fallback
    try:
        import locale
        loc = locale.getlocale()[0] or ""
        loc = loc.lower()
        if "vi" in loc:
            return "vi"
        if "zh" in loc:
            return "zh"
    except Exception:
        pass

    return "en"


def save_language_preference(lang: str) -> None:
    """Save selected language to user settings."""
    if lang not in STRINGS:
        return
    try:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        data = {}
        if SETTINGS_FILE.is_file():
            try:
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data["language"] = lang
        SETTINGS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def t(key: str, lang: str = "en", *args: Any) -> str:
    """Lookup translated string with fallback to en."""
    table = STRINGS.get(lang, STRINGS["en"])
    text = table.get(key) or STRINGS["en"].get(key, key)
    if args:
        try:
            return text.format(*args)
        except Exception:
            return text
    return text
