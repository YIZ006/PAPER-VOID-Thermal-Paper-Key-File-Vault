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

public class BridgeActivity extends Activity {
    private TextView statusView;

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
        statusView.setText("HTTP Bridge running on 127.0.0.1:9100\nReady for ADB forward.");
        statusView.setTextColor(Color.parseColor("#93C5FD"));
        statusView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
        statusView.setGravity(Gravity.CENTER);
        statusView.setPadding(0, 24, 0, 0);

        layout.addView(title);
        layout.addView(statusView);

        Button stopBtn = new Button(this);
        stopBtn.setText("Tắt Bridge & Thoát");
        stopBtn.setTextColor(Color.WHITE);
        stopBtn.setBackgroundColor(Color.parseColor("#DC2626"));
        stopBtn.setPadding(48, 24, 48, 24);
        LinearLayout.LayoutParams btnLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        btnLp.topMargin = 48;
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

    @Override
    protected void onResume() {
        super.onResume();
        if (statusView != null) {
            statusView.setText(BleBridgeService.getQuickSummary());
        }
    }
}
