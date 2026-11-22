package com.tinyprint.bridge;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.os.Bundle;
import android.util.TypedValue;
import android.view.Gravity;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.util.Enumeration;

public class BridgeActivity extends Activity {
    private TextView statusView;
    private TextView ipView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setGravity(Gravity.CENTER);
        layout.setBackgroundColor(Color.parseColor("#111827"));
        layout.setPadding(48, 48, 48, 48);

        TextView title = new TextView(this);
        title.setText("TinyPrint BLE Bridge");
        title.setTextColor(Color.WHITE);
        title.setTextSize(TypedValue.COMPLEX_UNIT_SP, 22);
        title.setGravity(Gravity.CENTER);

        statusView = new TextView(this);
        statusView.setText("Đang khởi động cầu nối BLE...");
        statusView.setTextColor(Color.parseColor("#93C5FD"));
        statusView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
        statusView.setGravity(Gravity.CENTER);
        statusView.setPadding(0, 20, 0, 16);

        ipView = new TextView(this);
        ipView.setText(getNetworkSummary());
        ipView.setTextColor(Color.parseColor("#86EFAC")); // emerald light
        ipView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        ipView.setGravity(Gravity.CENTER);
        ipView.setPadding(32, 24, 32, 24);
        ipView.setBackgroundColor(Color.parseColor("#1F2937"));

        layout.addView(title);
        layout.addView(statusView);
        layout.addView(ipView);

        Button stopBtn = new Button(this);
        stopBtn.setText("Tắt Bridge & Thoát");
        stopBtn.setTextColor(Color.WHITE);
        stopBtn.setBackgroundColor(Color.parseColor("#DC2626"));
        stopBtn.setPadding(48, 24, 48, 24);
        LinearLayout.LayoutParams btnLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        btnLp.topMargin = 40;
        stopBtn.setOnClickListener(v -> {
            Intent stopSvc = new Intent(this, BleBridgeService.class);
            stopSvc.setAction(BleBridgeService.ACTION_STOP);
            startService(stopSvc);
            finishAffinity();
        });
        layout.addView(stopBtn, btnLp);

        setContentView(layout);

        Intent svc = new Intent(this, BleBridgeService.class);
        startForegroundService(svc);
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
                            tailscale.append("• Tailscale: ").append(ip).append(":9100\n");
                        } else if (!ip.startsWith("127.")) {
                            wifi.append("• Wi-Fi LAN: ").append(ip).append(":9100\n");
                        }
                    }
                }
            }
        } catch (Exception ignored) {
        }

        StringBuilder sb = new StringBuilder("📡 ĐỊA CHỈ KẾT NỐI KHÔNG DÂY:\n\n");
        if (tailscale.length() > 0) sb.append(tailscale);
        if (wifi.length() > 0) sb.append(wifi);
        if (tailscale.length() == 0 && wifi.length() == 0) {
            sb.append("• Cáp USB: 127.0.0.1:9100 (qua ADB)\n");
        }
        sb.append("\n(Cổng 9100 luôn mở nền có WakeLock chống tắt)");
        return sb.toString().trim();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (statusView != null) {
            statusView.setText(BleBridgeService.getQuickSummary());
        }
        if (ipView != null) {
            ipView.setText(getNetworkSummary());
        }
    }
}
