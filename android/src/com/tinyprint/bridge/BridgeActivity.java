package com.tinyprint.bridge;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.Vibrator;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.util.Enumeration;

public class BridgeActivity extends Activity {
    private TextView lblStatusTitle;
    private TextView lblStatusDetail;
    private TextView lblNetwork;
    private Button btnConnect;
    private Button btnDisconnect;
    private Button btnTestPrint;
    private Button btnOpenScanner;
    private final Handler timerHandler = new Handler(Looper.getMainLooper());
    private Runnable refreshRunnable;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // Ensure background service is running
        Intent svc = new Intent(this, BleBridgeService.class);
        try {
            startForegroundService(svc);
        } catch (Exception e) {
            try { startService(svc); } catch (Exception ignored) {}
        }

        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(Color.parseColor("#0F172A")); // slate-900
        scroll.setFillViewport(true);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(40, 48, 40, 48);
        scroll.addView(root);

        // Header
        TextView title = new TextView(this);
        title.setText("PAPER-VOID Bridge");
        title.setTextColor(Color.WHITE);
        title.setTextSize(TypedValue.COMPLEX_UNIT_SP, 22);
        title.setTypeface(null, android.graphics.Typeface.BOLD);
        title.setGravity(Gravity.CENTER_HORIZONTAL);

        TextView subtitle = new TextView(this);
        subtitle.setText("Cầu nối in nhiệt X5 (BLE) & Quét thẻ quang học");
        subtitle.setTextColor(Color.parseColor("#94A3B8")); // slate-400
        subtitle.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        subtitle.setGravity(Gravity.CENTER_HORIZONTAL);
        subtitle.setPadding(0, 6, 0, 28);

        root.addView(title);
        root.addView(subtitle);

        // Card 1: X5 Printer Status & Toggle
        LinearLayout cardPrinter = makeCard();
        TextView card1Title = makeCardLabel("MÁY IN NHIỆT X5 (BLUETOOTH BLE)");
        cardPrinter.addView(card1Title);

        lblStatusTitle = new TextView(this);
        lblStatusTitle.setText("○ ĐANG KIỂM TRA...");
        lblStatusTitle.setTextSize(TypedValue.COMPLEX_UNIT_SP, 16);
        lblStatusTitle.setTypeface(null, android.graphics.Typeface.BOLD);
        lblStatusTitle.setTextColor(Color.parseColor("#F59E0B")); // amber-500
        lblStatusTitle.setPadding(0, 10, 0, 4);

        lblStatusDetail = new TextView(this);
        lblStatusDetail.setText("Chưa kết nối");
        lblStatusDetail.setTextColor(Color.parseColor("#CBD5E1"));
        lblStatusDetail.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        lblStatusDetail.setPadding(0, 0, 0, 16);

        cardPrinter.addView(lblStatusTitle);
        cardPrinter.addView(lblStatusDetail);

        LinearLayout btnRow = new LinearLayout(this);
        btnRow.setOrientation(LinearLayout.HORIZONTAL);

        btnConnect = new Button(this);
        btnConnect.setText("⚡ Nối Máy In X5");
        btnConnect.setTextColor(Color.WHITE);
        btnConnect.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        btnConnect.setTypeface(null, android.graphics.Typeface.BOLD);
        applyRoundedBackground(btnConnect, Color.parseColor("#2563EB"), 12);
        LinearLayout.LayoutParams lpBtn1 = new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1.0f);
        lpBtn1.rightMargin = 12;

        btnDisconnect = new Button(this);
        btnDisconnect.setText("Ngắt Kết Nối");
        btnDisconnect.setTextColor(Color.parseColor("#E2E8F0"));
        btnDisconnect.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        applyRoundedBackground(btnDisconnect, Color.parseColor("#334155"), 12);
        LinearLayout.LayoutParams lpBtn2 = new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1.0f);

        btnConnect.setOnClickListener(v -> onClickConnect());
        btnDisconnect.setOnClickListener(v -> onClickDisconnect());

        btnRow.addView(btnConnect, lpBtn1);
        btnRow.addView(btnDisconnect, lpBtn2);
        cardPrinter.addView(btnRow);
        root.addView(cardPrinter);

        // Card 2: Interactive Actions (Test Print + Camera Scanner)
        LinearLayout cardTools = makeCard();
        TextView card2Title = makeCardLabel("CÔNG CỤ ĐIỀU KHIỂN CHỦ ĐỘNG");
        cardTools.addView(card2Title);

        btnTestPrint = new Button(this);
        btnTestPrint.setText("🖨 In Thử Nghiệm 3cm (Test Strip)");
        btnTestPrint.setTextColor(Color.WHITE);
        btnTestPrint.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        btnTestPrint.setTypeface(null, android.graphics.Typeface.BOLD);
        applyRoundedBackground(btnTestPrint, Color.parseColor("#0D9488"), 12); // teal-600
        LinearLayout.LayoutParams lpTools1 = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lpTools1.topMargin = 12;
        lpTools1.bottomMargin = 10;
        btnTestPrint.setOnClickListener(v -> onClickTestPrint());
        cardTools.addView(btnTestPrint, lpTools1);

        btnOpenScanner = new Button(this);
        btnOpenScanner.setText("📷 Mở Camera Quét Thẻ Khóa");
        btnOpenScanner.setTextColor(Color.WHITE);
        btnOpenScanner.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        btnOpenScanner.setTypeface(null, android.graphics.Typeface.BOLD);
        applyRoundedBackground(btnOpenScanner, Color.parseColor("#4F46E5"), 12); // indigo-600
        LinearLayout.LayoutParams lpTools2 = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        btnOpenScanner.setOnClickListener(v -> {
            vibrate(40);
            startActivity(new Intent(this, ScannerActivity.class));
        });
        cardTools.addView(btnOpenScanner, lpTools2);
        root.addView(cardTools);

        // Card 3: Network Endpoints (USB + Tailscale + LAN)
        LinearLayout cardNet = makeCard();
        TextView card3Title = makeCardLabel("ĐỊA CHỈ TRUY CẬP TỪ MÁY TÍNH");
        cardNet.addView(card3Title);

        lblNetwork = new TextView(this);
        lblNetwork.setText(getNetworkSummary());
        lblNetwork.setTextColor(Color.parseColor("#86EFAC")); // emerald-300
        lblNetwork.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        lblNetwork.setTypeface(android.graphics.Typeface.MONOSPACE);
        lblNetwork.setPadding(0, 10, 0, 4);
        cardNet.addView(lblNetwork);
        root.addView(cardNet);

        // Footer Stop Button
        Button btnStopAll = new Button(this);
        btnStopAll.setText("🛑 Tắt Cầu Nối & Thoát");
        btnStopAll.setTextColor(Color.WHITE);
        btnStopAll.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        applyRoundedBackground(btnStopAll, Color.parseColor("#DC2626"), 12);
        LinearLayout.LayoutParams lpStop = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lpStop.topMargin = 16;
        lpStop.bottomMargin = 24;
        btnStopAll.setOnClickListener(v -> {
            vibrate(60);
            Intent stopSvc = new Intent(this, BleBridgeService.class);
            stopSvc.setAction(BleBridgeService.ACTION_STOP);
            try { startService(stopSvc); } catch (Exception ignored) {}
            finishAffinity();
        });
        root.addView(btnStopAll, lpStop);

        setContentView(scroll);

        // Setup timer auto-refresh
        refreshRunnable = new Runnable() {
            @Override
            public void run() {
                updateUI();
                timerHandler.postDelayed(this, 1200);
            }
        };
    }

    private void onClickConnect() {
        vibrate(40);
        Toast.makeText(this, "Đang quét tìm & ghép nối máy in X5…", Toast.LENGTH_SHORT).show();
        lblStatusTitle.setText("⚡ Đang ghép nối X5...");
        lblStatusTitle.setTextColor(Color.parseColor("#38BDF8")); // sky-400
        BleBridgeService svc = BleBridgeService.getInstance();
        if (svc != null) {
            svc.triggerConnect(null, "X5", () -> {
                vibrate(80);
                updateUI();
                if (BleBridgeService.isPrinterConnected()) {
                    Toast.makeText(this, "Ghép nối X5 thành công!", Toast.LENGTH_SHORT).show();
                } else {
                    Toast.makeText(this, "Không tìm thấy X5! Hãy bật nguồn máy in.", Toast.LENGTH_LONG).show();
                }
            });
        }
    }

    private void onClickDisconnect() {
        vibrate(30);
        BleBridgeService svc = BleBridgeService.getInstance();
        if (svc != null) {
            svc.triggerDisconnect(() -> {
                updateUI();
                Toast.makeText(this, "Đã ngắt kết nối máy in.", Toast.LENGTH_SHORT).show();
            });
        }
    }

    private void onClickTestPrint() {
        vibrate(40);
        BleBridgeService svc = BleBridgeService.getInstance();
        if (svc == null || !BleBridgeService.isPrinterConnected()) {
            Toast.makeText(this, "Chưa kết nối máy in X5! Hãy bấm [⚡ Nối Máy In X5] trước.", Toast.LENGTH_SHORT).show();
            return;
        }
        Toast.makeText(this, "Đang gửi dải in thử nghiệm 3cm tới X5...", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            String res = svc.printTestStrip();
            runOnUiThread(() -> {
                vibrate(100);
                if (res != null && res.contains("\"ok\":true")) {
                    Toast.makeText(this, "✔ In mẫu thử nghiệm thành công!", Toast.LENGTH_SHORT).show();
                } else {
                    Toast.makeText(this, "Kết quả in: " + res, Toast.LENGTH_SHORT).show();
                }
            });
        }, "TestPrintThread").start();
    }

    private void updateUI() {
        boolean connected = BleBridgeService.isPrinterConnected();
        String name = BleBridgeService.getPrinterName();
        String mac = BleBridgeService.getPrinterMac();
        int mtu = BleBridgeService.getPrinterMtu();

        if (connected) {
            lblStatusTitle.setText("● ĐÃ KẾT NỐI MÁY IN X5");
            lblStatusTitle.setTextColor(Color.parseColor("#22C55E")); // green-500
            lblStatusDetail.setText("Thiết bị: " + (name.isEmpty() ? "X5" : name) + " (" + mac + ") • MTU=" + mtu + "\nSẵn sàng nhận lệnh in từ máy tính & điện thoại!");
            btnConnect.setVisibility(View.GONE);
            btnDisconnect.setVisibility(View.VISIBLE);
        } else {
            lblStatusTitle.setText("○ CHƯA KẾT NỐI MÁY IN X5");
            lblStatusTitle.setTextColor(Color.parseColor("#F59E0B")); // amber-500
            lblStatusDetail.setText("Dịch vụ cầu nối đang mở. Chạm nút dưới để tự động ghép nối với máy in X5.");
            btnConnect.setVisibility(View.VISIBLE);
            btnDisconnect.setVisibility(View.GONE);
        }

        if (lblNetwork != null) {
            lblNetwork.setText(getNetworkSummary());
        }
    }

    private LinearLayout makeCard() {
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(32, 28, 32, 28);
        applyRoundedBackground(card, Color.parseColor("#1E293B"), 16);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.bottomMargin = 20;
        card.setLayoutParams(lp);
        return card;
    }

    private TextView makeCardLabel(String text) {
        TextView tv = new TextView(this);
        tv.setText(text);
        tv.setTextColor(Color.parseColor("#64748B")); // slate-500
        tv.setTextSize(TypedValue.COMPLEX_UNIT_SP, 11);
        tv.setTypeface(null, android.graphics.Typeface.BOLD);
        return tv;
    }

    private void applyRoundedBackground(View view, int color, int radiusDp) {
        GradientDrawable gd = new GradientDrawable();
        gd.setColor(color);
        float px = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, radiusDp, getResources().getDisplayMetrics());
        gd.setCornerRadius(px);
        view.setBackground(gd);
    }

    private void vibrate(long ms) {
        try {
            Vibrator vib = (Vibrator) getSystemService(Context.VIBRATOR_SERVICE);
            if (vib != null && vib.hasVibrator()) {
                vib.vibrate(ms);
            }
        } catch (Exception ignored) {
        }
    }

    private static String getNetworkSummary() {
        StringBuilder tailscale = new StringBuilder();
        StringBuilder wifi = new StringBuilder();
        try {
            Enumeration<NetworkInterface> interfaces = NetworkInterface.getNetworkInterfaces();
            while (interfaces != null && interfaces.hasMoreElements()) {
                NetworkInterface nif = interfaces.nextElement();
                if (nif.isLoopback() || !nif.isUp()) continue;
                Enumeration<InetAddress> addrs = nif.getInetAddresses();
                while (addrs.hasMoreElements()) {
                    InetAddress a = addrs.nextElement();
                    if (a instanceof Inet4Address && !a.isLoopbackAddress()) {
                        String ip = a.getHostAddress();
                        if (ip.startsWith("100.")) {
                            tailscale.append("🌐 Tailscale: http://").append(ip).append(":9100\n");
                        } else if (!ip.startsWith("127.")) {
                            wifi.append("📶 Wi-Fi LAN: http://").append(ip).append(":9100\n");
                        }
                    }
                }
            }
        } catch (Exception ignored) {
        }

        StringBuilder sb = new StringBuilder();
        sb.append("🔌 Cáp USB:   http://127.0.0.1:9100\n");
        if (tailscale.length() > 0) sb.append(tailscale);
        if (wifi.length() > 0) sb.append(wifi);
        return sb.toString().trim();
    }

    @Override
    protected void onResume() {
        super.onResume();
        updateUI();
        timerHandler.postDelayed(refreshRunnable, 1200);
    }

    @Override
    protected void onPause() {
        super.onPause();
        timerHandler.removeCallbacks(refreshRunnable);
    }
}
