package com.tinyprint.bridge;

import android.Manifest;
import android.animation.ValueAnimator;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.ImageFormat;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.PorterDuff;
import android.graphics.PorterDuffXfermode;
import android.graphics.Rect;
import android.graphics.RectF;
import android.graphics.YuvImage;
import android.hardware.Camera;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.View;
import android.view.WindowManager;
import android.view.animation.DecelerateInterpolator;
import android.view.animation.OvershootInterpolator;
import android.widget.FrameLayout;
import android.widget.TextView;

import java.io.ByteArrayOutputStream;
import java.lang.ref.WeakReference;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

@SuppressWarnings("deprecation")
public class ScannerActivity extends Activity implements SurfaceHolder.Callback, Camera.PreviewCallback {
    private static final int REQ_CAMERA = 101;
    private static final long FRAME_INTERVAL_MS = 160L;

    private static volatile byte[] sLatestJpeg = null;
    private static final AtomicInteger sFrameSeq = new AtomicInteger(0);
    private static volatile boolean sActive = false;
    private static volatile WeakReference<ScannerActivity> sInstance = new WeakReference<>(null);

    private SurfaceView surfaceView;
    private ViewfinderOverlayView overlayView;
    private TextView closeBtn;
    private Camera camera;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private volatile long lastEncodeMs = 0L;
    private volatile boolean recognized = false;
    private volatile boolean encoding = false;

    public static byte[] getLatestJpeg() {
        return sLatestJpeg;
    }

    public static int getFrameSeq() {
        return sFrameSeq.get();
    }

    public static boolean isScannerActive() {
        return sActive;
    }

    public static void resetSharedState() {
        sLatestJpeg = null;
        sFrameSeq.set(0);
    }

    public static void notifyRecognized(final String vaultId) {
        final ScannerActivity act = sInstance.get();
        if (act != null) {
            act.runOnUiThread(() -> act.showRecognizedState(vaultId));
        }
    }

    public static void requestStop() {
        final ScannerActivity act = sInstance.get();
        if (act != null) {
            act.runOnUiThread(act::finish);
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(
                WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                        | WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
                        | WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED
        );
        sInstance = new WeakReference<>(this);
        sActive = true;
        recognized = false;
        sLatestJpeg = null;

        // Ensure bridge service is alive for both USB & Tailscale
        try {
            startForegroundService(new Intent(this, BleBridgeService.class));
        } catch (Exception ignored) {
        }

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);

        surfaceView = new SurfaceView(this);
        surfaceView.getHolder().addCallback(this);
        root.addView(surfaceView, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT
        ));

        overlayView = new ViewfinderOverlayView(this);
        overlayView.setOnClickListener(v -> triggerAutoFocus());
        root.addView(overlayView, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT
        ));

        closeBtn = new TextView(this);
        closeBtn.setText("Đóng quét");
        closeBtn.setTextColor(Color.WHITE);
        closeBtn.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
        closeBtn.setGravity(Gravity.CENTER);
        closeBtn.setPadding(48, 22, 48, 22);
        closeBtn.setBackgroundColor(Color.parseColor("#66000000"));
        closeBtn.setOnClickListener(v -> finish());
        FrameLayout.LayoutParams btnLp = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.WRAP_CONTENT,
                FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL
        );
        btnLp.bottomMargin = 84;
        root.addView(closeBtn, btnLp);

        setContentView(root);

        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.CAMERA}, REQ_CAMERA);
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        recognized = false;
        sLatestJpeg = null;
        if (overlayView != null) {
            overlayView.resetToScanning();
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_CAMERA && grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            openCameraIfReady(surfaceView.getHolder());
        }
    }

    @Override
    public void surfaceCreated(SurfaceHolder holder) {
        openCameraIfReady(holder);
    }

    @Override
    public void surfaceChanged(SurfaceHolder holder, int format, int width, int height) {
        if (camera == null) {
            openCameraIfReady(holder);
        }
    }

    @Override
    public void surfaceDestroyed(SurfaceHolder holder) {
        releaseCamera();
    }

    @Override
    protected void onDestroy() {
        sActive = false;
        releaseCamera();
        sInstance = new WeakReference<>(null);
        super.onDestroy();
    }

    private void openCameraIfReady(SurfaceHolder holder) {
        if (camera != null || holder == null) return;
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) return;
        try {
            camera = Camera.open(Camera.CameraInfo.CAMERA_FACING_BACK);
            if (camera == null) {
                camera = Camera.open();
            }
            if (camera == null) return;
            camera.setDisplayOrientation(90);

            Camera.Parameters params = camera.getParameters();
            Camera.Size best = choosePreviewSize(params.getSupportedPreviewSizes(), 1280, 720);
            if (best != null) {
                params.setPreviewSize(best.width, best.height);
            }
            List<String> focusModes = params.getSupportedFocusModes();
            if (focusModes != null) {
                if (focusModes.contains(Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE)) {
                    params.setFocusMode(Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE);
                } else if (focusModes.contains(Camera.Parameters.FOCUS_MODE_AUTO)) {
                    params.setFocusMode(Camera.Parameters.FOCUS_MODE_AUTO);
                }
            }
            params.setPreviewFormat(ImageFormat.NV21);
            camera.setParameters(params);
            camera.setPreviewDisplay(holder);
            camera.setPreviewCallback(this);
            camera.startPreview();
        } catch (Exception e) {
            releaseCamera();
        }
    }

    private void triggerAutoFocus() {
        if (camera == null || recognized) return;
        try {
            camera.autoFocus(null);
        } catch (Exception ignored) {
        }
    }

    private static Camera.Size choosePreviewSize(List<Camera.Size> sizes, int targetW, int targetH) {
        if (sizes == null || sizes.isEmpty()) return null;
        Camera.Size best = sizes.get(0);
        int bestDiff = Integer.MAX_VALUE;
        for (Camera.Size s : sizes) {
            if (s.width < 640 || s.width > 1920) continue;
            int diff = Math.abs(s.width - targetW) + Math.abs(s.height - targetH);
            if (diff < bestDiff) {
                bestDiff = diff;
                best = s;
            }
        }
        return best;
    }

    private void releaseCamera() {
        if (camera != null) {
            try {
                camera.setPreviewCallback(null);
                camera.stopPreview();
            } catch (Exception ignored) {
            }
            try {
                camera.release();
            } catch (Exception ignored) {
            }
            camera = null;
        }
    }

    @Override
    public void onPreviewFrame(final byte[] data, Camera cam) {
        if (recognized || encoding || data == null || cam == null) return;
        long now = System.currentTimeMillis();
        if (now - lastEncodeMs < FRAME_INTERVAL_MS) return;
        lastEncodeMs = now;
        encoding = true;

        final Camera.Size size;
        try {
            size = cam.getParameters().getPreviewSize();
        } catch (Exception e) {
            encoding = false;
            return;
        }
        final byte[] copy = new byte[data.length];
        System.arraycopy(data, 0, copy, 0, data.length);

        new Thread(() -> {
            try {
                YuvImage yuv = new YuvImage(copy, ImageFormat.NV21, size.width, size.height, null);
                ByteArrayOutputStream baos = new ByteArrayOutputStream(65536);
                yuv.compressToJpeg(new Rect(0, 0, size.width, size.height), 82, baos);
                sLatestJpeg = baos.toByteArray();
                sFrameSeq.incrementAndGet();
            } catch (Throwable ignored) {
            } finally {
                encoding = false;
            }
        }, "ScannerJpeg").start();
    }

    private void showRecognizedState(String vaultId) {
        if (recognized) return;
        recognized = true;
        if (overlayView != null) {
            overlayView.triggerRecognized(vaultId);
        }
        if (closeBtn != null) {
            closeBtn.setVisibility(View.GONE);
        }
        try {
            Vibrator vib = (Vibrator) getSystemService(Context.VIBRATOR_SERVICE);
            if (vib != null && vib.hasVibrator()) {
                if (Build.VERSION.SDK_INT >= 26) {
                    vib.vibrate(VibrationEffect.createOneShot(85, VibrationEffect.DEFAULT_AMPLITUDE));
                } else {
                    vib.vibrate(85);
                }
            }
        } catch (Exception ignored) {
        }
        mainHandler.postDelayed(this::finish, 1650L);
    }

    /**
     * Custom Canvas viewfinder: smooth zoom-in + rounded-corner border animation,
     * laser scan bar while searching, and emerald green zoom-snap + checkmark on lock!
     */
    private static class ViewfinderOverlayView extends View {
        private final Paint scrimPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint clearPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint borderPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint cornerPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint glowPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint scanLinePaint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint pillPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint textPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint subTextPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint checkPaint = new Paint(Paint.ANTI_ALIAS_FLAG);

        private float boxScale = 1.0f;
        private float scanProgress = 0.0f;
        private boolean isRecognized = false;
        private String recognizedVaultId = "";
        private ValueAnimator pulseAnim;
        private ValueAnimator scanAnim;

        ViewfinderOverlayView(Context ctx) {
            super(ctx);
            setLayerType(LAYER_TYPE_SOFTWARE, null);

            scrimPaint.setColor(Color.parseColor("#9A000000"));
            clearPaint.setXfermode(new PorterDuffXfermode(PorterDuff.Mode.CLEAR));

            borderPaint.setStyle(Paint.Style.STROKE);
            borderPaint.setStrokeWidth(4f);
            borderPaint.setColor(Color.parseColor("#6638BDF8"));

            cornerPaint.setStyle(Paint.Style.STROKE);
            cornerPaint.setStrokeWidth(10f);
            cornerPaint.setStrokeCap(Paint.Cap.ROUND);
            cornerPaint.setColor(Color.parseColor("#38BDF8"));

            glowPaint.setStyle(Paint.Style.FILL);
            glowPaint.setColor(Color.parseColor("#2E22C55E"));

            scanLinePaint.setStyle(Paint.Style.STROKE);
            scanLinePaint.setStrokeWidth(5f);
            scanLinePaint.setColor(Color.parseColor("#CC38BDF8"));

            pillPaint.setStyle(Paint.Style.FILL);
            pillPaint.setColor(Color.parseColor("#CC111827"));

            textPaint.setColor(Color.WHITE);
            textPaint.setTextAlign(Paint.Align.CENTER);
            textPaint.setTextSize(42f);
            textPaint.setFakeBoldText(true);

            subTextPaint.setColor(Color.parseColor("#D1FAE5"));
            subTextPaint.setTextAlign(Paint.Align.CENTER);
            subTextPaint.setTextSize(32f);

            checkPaint.setStyle(Paint.Style.STROKE);
            checkPaint.setStrokeWidth(14f);
            checkPaint.setStrokeCap(Paint.Cap.ROUND);
            checkPaint.setStrokeJoin(Paint.Join.ROUND);
            checkPaint.setColor(Color.WHITE);

            startScanningAnimations();
        }

        private void startScanningAnimations() {
            stopAnimations();
            pulseAnim = ValueAnimator.ofFloat(0.95f, 1.03f);
            pulseAnim.setDuration(1100);
            pulseAnim.setRepeatMode(ValueAnimator.REVERSE);
            pulseAnim.setRepeatCount(ValueAnimator.INFINITE);
            pulseAnim.setInterpolator(new DecelerateInterpolator());
            pulseAnim.addUpdateListener(a -> {
                boxScale = (Float) a.getAnimatedValue();
                invalidate();
            });
            pulseAnim.start();

            scanAnim = ValueAnimator.ofFloat(0.08f, 0.92f);
            scanAnim.setDuration(1450);
            scanAnim.setRepeatMode(ValueAnimator.REVERSE);
            scanAnim.setRepeatCount(ValueAnimator.INFINITE);
            scanAnim.addUpdateListener(a -> {
                scanProgress = (Float) a.getAnimatedValue();
                invalidate();
            });
            scanAnim.start();
        }

        private void stopAnimations() {
            if (pulseAnim != null) {
                pulseAnim.cancel();
                pulseAnim = null;
            }
            if (scanAnim != null) {
                scanAnim.cancel();
                scanAnim = null;
            }
        }

        void resetToScanning() {
            isRecognized = false;
            recognizedVaultId = "";
            borderPaint.setColor(Color.parseColor("#6638BDF8"));
            cornerPaint.setColor(Color.parseColor("#38BDF8"));
            pillPaint.setColor(Color.parseColor("#CC111827"));
            startScanningAnimations();
            invalidate();
        }

        void triggerRecognized(String vaultId) {
            stopAnimations();
            isRecognized = true;
            recognizedVaultId = (vaultId != null) ? vaultId : "";
            borderPaint.setColor(Color.parseColor("#22C55E"));
            borderPaint.setStrokeWidth(7f);
            cornerPaint.setColor(Color.parseColor("#22C55E"));
            cornerPaint.setStrokeWidth(13f);
            pillPaint.setColor(Color.parseColor("#E616A34A"));

            // Zoom-in snap animation on lock!
            pulseAnim = ValueAnimator.ofFloat(1.12f, 0.90f, 1.02f);
            pulseAnim.setDuration(420);
            pulseAnim.setInterpolator(new OvershootInterpolator(1.6f));
            pulseAnim.addUpdateListener(a -> {
                boxScale = (Float) a.getAnimatedValue();
                invalidate();
            });
            pulseAnim.start();
        }

        @Override
        protected void onDetachedFromWindow() {
            stopAnimations();
            super.onDetachedFromWindow();
        }

        @Override
        protected void onDraw(Canvas canvas) {
            super.onDraw(canvas);
            float w = getWidth();
            float h = getHeight();
            if (w <= 0 || h <= 0) return;

            float baseW = Math.min(w * 0.80f, h * 0.56f);
            float baseH = baseW * 0.78f; // matches 64x48 (4:3) PV-RIBBON tile proportion
            float bw = baseW * boxScale;
            float bh = baseH * boxScale;
            float left = (w - bw) * 0.5f;
            float top = (h - bh) * 0.45f;
            float right = left + bw;
            float bottom = top + bh;
            float radius = 38f;
            RectF box = new RectF(left, top, right, bottom);

            // Dark scrim with clear rounded cutout
            canvas.drawRect(0, 0, w, h, scrimPaint);
            canvas.drawRoundRect(box, radius, radius, clearPaint);

            if (isRecognized) {
                canvas.drawRoundRect(box, radius, radius, glowPaint);
            }

            // Subtle rounded full border
            canvas.drawRoundRect(box, radius, radius, borderPaint);

            // Bold rounded corner brackets
            float arm = Math.min(bw, bh) * 0.16f;
            drawCorner(canvas, left, top, radius, arm, 1, 1);
            drawCorner(canvas, right, top, radius, arm, -1, 1);
            drawCorner(canvas, left, bottom, radius, arm, 1, -1);
            drawCorner(canvas, right, bottom, radius, arm, -1, -1);

            if (!isRecognized) {
                float sy = top + bh * scanProgress;
                canvas.drawLine(left + radius, sy, right - radius, sy, scanLinePaint);

                RectF pill = new RectF(w * 0.10f, top - 130f, w * 0.90f, top - 42f);
                canvas.drawRoundRect(pill, 44f, 44f, pillPaint);
                canvas.drawText("Đưa Thẻ Khóa PAPER-VOID vào khung", w * 0.5f, pill.centerY() + 14f, textPaint);
            } else {
                // Big green checkmark circle inside box
                float cx = box.centerX();
                float cy = box.centerY();
                Paint circlePaint = new Paint(Paint.ANTI_ALIAS_FLAG);
                circlePaint.setColor(Color.parseColor("#E616A34A"));
                canvas.drawCircle(cx, cy, 74f, circlePaint);

                Path tick = new Path();
                tick.moveTo(cx - 32f, cy + 2f);
                tick.lineTo(cx - 8f, cy + 28f);
                tick.lineTo(cx + 38f, cy - 26f);
                canvas.drawPath(tick, checkPaint);

                // Status badge above box
                RectF pill = new RectF(w * 0.10f, top - 160f, w * 0.90f, top - 42f);
                canvas.drawRoundRect(pill, 44f, 44f, pillPaint);
                canvas.drawText("✔  ĐÃ NHẬN MÃ THẺ", w * 0.5f, pill.top + 54f, textPaint);
                if (!recognizedVaultId.isEmpty()) {
                    canvas.drawText("ID: " + recognizedVaultId, w * 0.5f, pill.top + 98f, subTextPaint);
                }
            }
        }

        private void drawCorner(Canvas c, float x, float y, float r, float arm, int dx, int dy) {
            Path p = new Path();
            p.moveTo(x + dx * (r + arm), y);
            p.lineTo(x + dx * r, y);
            p.quadTo(x, y, x, y + dy * r);
            p.lineTo(x, y + dy * (r + arm));
            c.drawPath(p, cornerPaint);
        }
    }
}
