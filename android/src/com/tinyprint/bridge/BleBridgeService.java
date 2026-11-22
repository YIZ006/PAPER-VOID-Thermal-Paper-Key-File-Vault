package com.tinyprint.bridge;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattDescriptor;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.bluetooth.BluetoothStatusCodes;
import android.bluetooth.le.BluetoothLeScanner;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanResult;
import android.bluetooth.le.ScanSettings;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.util.Log;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.net.wifi.WifiManager;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

public class BleBridgeService extends Service {
    private static final String TAG = "BleBridgeService";
    public static final String ACTION_STOP = "com.tinyprint.bridge.ACTION_STOP";
    private static final int PORT = 9100;
    private static final String CHANNEL_ID = "tinyprint_bridge";

    private static final String UUID_SVC_AE30 = "0000ae30-0000-1000-8000-00805f9b34fb";
    private static final String UUID_SVC_AE00 = "0000ae00-0000-1000-8000-00805f9b34fb";
    private static final String UUID_SVC_FF00 = "0000ff00-0000-1000-8000-00805f9b34fb";
    private static final String UUID_SVC_AB00 = "0000ab00-0000-1000-8000-00805f9b34fb";

    private static final String UUID_CHR_AE01 = "0000ae01-0000-1000-8000-00805f9b34fb";
    private static final String UUID_CHR_AE02 = "0000ae02-0000-1000-8000-00805f9b34fb";
    private static final String UUID_CHR_AE04 = "0000ae04-0000-1000-8000-00805f9b34fb";
    private static final String UUID_CHR_FF02 = "0000ff02-0000-1000-8000-00805f9b34fb";
    private static final String UUID_CHR_AB01 = "0000ab01-0000-1000-8000-00805f9b34fb";
    private static final UUID UUID_CCCD = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb");

    private static volatile boolean sConnected = false;
    private static volatile String sDeviceName = "";
    private static volatile String sDeviceMac = "";
    private static volatile int sMtu = 23;
    private static volatile String sLastError = "";

    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private volatile boolean running = false;
    private ServerSocket serverSocket;
    private Thread serverThread;

    private BluetoothAdapter btAdapter;
    private final Object gattLock = new Object();
    private BluetoothGatt currentGatt;
    private BluetoothGattCharacteristic writeChar;
    private BluetoothGattCharacteristic notifyChar;

    private PowerManager.WakeLock wakeLock;
    private WifiManager.WifiLock wifiLock;
    private volatile String serviceUuid = "";
    private volatile String writeUuid = "";
    private volatile String notifyUuid = "";
    private volatile String lastNotifyHex = "";
    private volatile List<String> discoveredServices = Collections.emptyList();
    private final AtomicBoolean discoverStarted = new AtomicBoolean(false);
    private volatile CountDownLatch connectLatch;
    private volatile CountDownLatch writeLatch;

    private static volatile BleBridgeService sInstance = null;
    public static BleBridgeService getInstance() {
        return sInstance;
    }

    public static boolean isPrinterConnected() {
        return sConnected;
    }

    public static String getPrinterName() {
        return sDeviceName;
    }

    public static String getPrinterMac() {
        return sDeviceMac;
    }

    public static int getPrinterMtu() {
        return sMtu;
    }

    public void triggerConnect(String mac, String name, Runnable onDone) {
        new Thread(() -> {
            handleConnect(mac, name);
            if (onDone != null) {
                mainHandler.post(onDone);
            }
        }, "TriggerConnect").start();
    }

    public void triggerDisconnect(Runnable onDone) {
        closeGatt();
        if (onDone != null) {
            mainHandler.post(onDone);
        }
    }

    private static final int[] CRC8_TABLE = {
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
        0xFA, 0xFD, 0xF4, 0xF3
    };

    public static byte crc8(byte[] data) {
        int crc = 0;
        if (data != null) {
            for (byte b : data) {
                crc = CRC8_TABLE[(crc ^ (b & 0xFF)) & 0xFF];
            }
        }
        return (byte) crc;
    }

    public static byte[] framePacket(int cmd, byte[] payload) {
        int len = payload == null ? 0 : payload.length;
        byte[] pkt = new byte[8 + len];
        pkt[0] = 0x51;
        pkt[1] = 0x78;
        pkt[2] = (byte) cmd;
        pkt[3] = 0x00;
        pkt[4] = (byte) (len & 0xFF);
        pkt[5] = (byte) ((len >> 8) & 0xFF);
        if (len > 0) {
            System.arraycopy(payload, 0, pkt, 6, len);
        }
        pkt[6 + len] = crc8(payload == null ? new byte[0] : payload);
        pkt[7 + len] = (byte) 0xFF;
        return pkt;
    }

    public String printTestStrip() {
        if (!sConnected || writeChar == null) {
            return "Chưa kết nối máy in X5!";
        }
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        try {
            out.write(framePacket(0xAF, new byte[] { (byte)0x20, (byte)0x4E }));
            out.write(framePacket(0xA4, new byte[] { (byte)0x33 }));
            out.write(framePacket(0xBE, new byte[] { (byte)0x01 }));

            byte[] solidRow = new byte[48];
            Arrays.fill(solidRow, (byte)0xFF);
            byte[] borderRow = new byte[48];
            borderRow[0] = (byte)0xFF;
            borderRow[47] = (byte)0xFF;
            for (int i = 1; i < 47; i++) {
                borderRow[i] = (byte)((i % 2 == 0) ? 0xAA : 0x55);
            }

            for (int r = 0; r < 36; r++) {
                byte[] row = (r < 3 || r >= 33) ? solidRow : borderRow;
                out.write(framePacket(0xA2, row));
            }

            out.write(framePacket(0xA1, new byte[] { 64, 0 }));
            byte[] fullCmd = out.toByteArray();
            return handleWrite(fullCmd, 120, 15);
        } catch (Exception e) {
            return "Lỗi in: " + e.getMessage();
        }
    }

    public static String getQuickSummary() {
        String base = "HTTP Bridge: 0.0.0.0:" + PORT + " (USB + Tailscale)\n";
        if (sConnected) {
            return base + "Connected: " + sDeviceName + " (" + sDeviceMac + ")\nMTU: " + sMtu;
        }
        if (sLastError != null && !sLastError.isEmpty()) {
            return base + "Disconnected (" + sLastError + ")";
        }
        return base + "Waiting for /connect...";
    }

    @Override
    public void onCreate() {
        super.onCreate();
        sInstance = this;
        BluetoothManager bm = (BluetoothManager) getSystemService(BLUETOOTH_SERVICE);
        if (bm != null) {
            btAdapter = bm.getAdapter();
        }
        createNotificationChannel();
        try {
            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            if (pm != null) {
                wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "TinyPrint:BridgeLock");
                wakeLock.acquire(12 * 60 * 60 * 1000L);
            }
        } catch (Exception ignored) {
        }
        try {
            WifiManager wm = (WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
            if (wm != null) {
                wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "TinyPrint:WifiLock");
                wifiLock.acquire();
            }
        } catch (Exception ignored) {
        }
        startForegroundCompat("Listening on 0.0.0.0:" + PORT + " (USB + Tailscale)");
        startHttpServer();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            shutdownNow();
            return START_NOT_STICKY;
        }
        startForegroundCompat("Listening on 0.0.0.0:" + PORT + " (USB + Tailscale)");
        startHttpServer();
        return START_STICKY;
    }

    private void shutdownNow() {
        running = false;
        ScannerActivity.requestStop();
        closeGatt();
        try {
            if (serverSocket != null) serverSocket.close();
        } catch (IOException ignored) {
        }
        stopForeground(true);
        stopSelf();
    }

    @Override
    public void onDestroy() {
        running = false;
        if (sInstance == this) {
            sInstance = null;
        }
        try {
            if (serverSocket != null) serverSocket.close();
        } catch (IOException ignored) {
        }
        if (wakeLock != null && wakeLock.isHeld()) {
            try { wakeLock.release(); } catch (Exception ignored) {}
            wakeLock = null;
        }
        if (wifiLock != null && wifiLock.isHeld()) {
            try { wifiLock.release(); } catch (Exception ignored) {}
            wifiLock = null;
        }
        closeGatt();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void createNotificationChannel() {
        NotificationChannel ch = new NotificationChannel(
                CHANNEL_ID,
                "TinyPrint BLE Bridge",
                NotificationManager.IMPORTANCE_LOW
        );
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null) {
            nm.createNotificationChannel(ch);
        }
    }

    private void startForegroundCompat(String text) {
        Intent stopIntent = new Intent(this, BleBridgeService.class);
        stopIntent.setAction(ACTION_STOP);
        int piFlags = PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE;
        PendingIntent stopPi = PendingIntent.getService(this, 10, stopIntent, piFlags);

        Notification notif = new Notification.Builder(this, CHANNEL_ID)
                .setContentTitle("TinyPrint BLE Bridge")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
                .setOngoing(true)
                .addAction(new Notification.Action.Builder(
                        android.R.drawable.ic_menu_close_clear_cancel, "Tắt Bridge", stopPi
                ).build())
                .build();
        try {
            if (Build.VERSION.SDK_INT >= 34) {
                int type = ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE;
                if (checkSelfPermission(android.Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED) {
                    type = ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC;
                }
                startForeground(1, notif, type);
            } else if (Build.VERSION.SDK_INT >= 29) {
                startForeground(1, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE);
            } else {
                startForeground(1, notif);
            }
        } catch (Throwable t) {
            Log.w(TAG, "startForeground typed failed, falling back: " + t.getMessage());
            try {
                startForeground(1, notif);
            } catch (Throwable t2) {
                Log.e(TAG, "startForeground fallback failed: " + t2.getMessage());
            }
        }
    }

    private synchronized void startHttpServer() {
        if (running) return;
        running = true;
        serverThread = new Thread(() -> {
            try {
                serverSocket = new ServerSocket(PORT, 16, InetAddress.getByName("0.0.0.0"));
                Log.i(TAG, "Bridge HTTP Server bound to 0.0.0.0:" + PORT);
                while (running) {
                    Socket sock = serverSocket.accept();
                    new Thread(() -> handleClient(sock), "BridgeClient").start();
                }
            } catch (IOException e) {
                sLastError = "ServerSocket: " + e.getMessage();
                Log.e(TAG, sLastError);
            }
        }, "BridgeServer");
        serverThread.start();
    }

    private void handleClient(Socket sock) {
        try (Socket client = sock) {
            client.setSoTimeout(30000);
            InputStream in = client.getInputStream();
            OutputStream out = client.getOutputStream();

            byte[] rawHeader = readHeaderBytes(in);
            if (rawHeader == null || rawHeader.length == 0) {
                return;
            }
            String headerText = new String(rawHeader, StandardCharsets.ISO_8859_1);
            String[] lines = headerText.split("\r\n");
            if (lines.length == 0) return;

            String[] reqParts = lines[0].split(" ");
            if (reqParts.length < 2) {
                sendJson(out, 400, "{\"error\":\"bad request\"}");
                return;
            }
            String method = reqParts[0].toUpperCase(Locale.ROOT);
            String target = reqParts[1];
            String path = target;
            Map<String, String> query = new HashMap<>();
            int qIdx = target.indexOf('?');
            if (qIdx >= 0) {
                path = target.substring(0, qIdx);
                parseQuery(target.substring(qIdx + 1), query);
            }

            int contentLength = 0;
            for (int i = 1; i < lines.length; i++) {
                int colon = lines[i].indexOf(':');
                if (colon > 0) {
                    String k = lines[i].substring(0, colon).trim().toLowerCase(Locale.ROOT);
                    String v = lines[i].substring(colon + 1).trim();
                    if ("content-length".equals(k)) {
                        try {
                            contentLength = Integer.parseInt(v);
                        } catch (NumberFormatException ignored) {
                        }
                    }
                }
            }

            byte[] body = readExact(in, contentLength);
            String resp;
            int code = 200;

            if ("GET".equals(method) && "/status".equals(path)) {
                resp = buildStatusJson();
            } else if ("GET".equals(method) && "/scan".equals(path)) {
                int ms = parseInt(query.get("ms"), 4000, 500, 15000);
                resp = handleScan(ms);
            } else if ("POST".equals(method) && "/connect".equals(path)) {
                String mac = query.get("mac");
                String name = query.get("name");
                if (name == null || name.isEmpty()) name = "X5";
                resp = handleConnect(mac, name);
            } else if ("POST".equals(method) && "/write".equals(path)) {
                int chunk = parseInt(query.get("chunk"), 120, 20, 512);
                int delay = parseInt(query.get("delay"), 15, 0, 500);
                resp = handleWrite(body, chunk, delay);
            } else if ("POST".equals(method) && "/disconnect".equals(path)) {
                closeGatt();
                resp = "{\"ok\":true}";
            } else if ("POST".equals(method) && "/scanner/start".equals(path)) {
                ScannerActivity.resetSharedState();
                Intent scanIntent = new Intent(this, ScannerActivity.class);
                scanIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
                mainHandler.post(() -> startActivity(scanIntent));
                resp = "{\"ok\":true}";
            } else if ("GET".equals(method) && "/scanner/frame".equals(path)) {
                byte[] jpg = ScannerActivity.getLatestJpeg();
                int seq = ScannerActivity.getFrameSeq();
                boolean active = ScannerActivity.isScannerActive();
                if (jpg == null || jpg.length == 0) {
                    sendBytes(out, 204, "image/jpeg", new byte[0],
                            "X-Frame-Seq: " + seq + "\r\nX-Scanner-Active: " + (active ? "1" : "0") + "\r\n");
                } else {
                    sendBytes(out, 200, "image/jpeg", jpg,
                            "X-Frame-Seq: " + seq + "\r\nX-Scanner-Active: " + (active ? "1" : "0") + "\r\n");
                }
                return;
            } else if ("POST".equals(method) && "/scanner/ack".equals(path)) {
                String vid = query.get("vault_id");
                ScannerActivity.notifyRecognized(vid != null ? vid : "");
                resp = "{\"ok\":true}";
            } else if ("POST".equals(method) && "/scanner/stop".equals(path)) {
                ScannerActivity.requestStop();
                resp = "{\"ok\":true}";
            } else if ("POST".equals(method) && "/shutdown".equals(path)) {
                sendJson(out, 200, "{\"ok\":true,\"shutdown\":true}");
                mainHandler.postDelayed(this::shutdownNow, 120L);
                return;
            } else {
                code = 404;
                resp = "{\"error\":\"not found\"}";
            }

            sendJson(out, code, resp);
        } catch (Throwable t) {
            sLastError = t.getClass().getSimpleName() + ": " + t.getMessage();
        }
    }

    private static byte[] readHeaderBytes(InputStream in) throws IOException {
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        int prev3 = -1, prev2 = -1, prev1 = -1;
        int b;
        while ((b = in.read()) != -1) {
            buf.write(b);
            if (prev3 == '\r' && prev2 == '\n' && prev1 == '\r' && b == '\n') {
                return buf.toByteArray();
            }
            prev3 = prev2;
            prev2 = prev1;
            prev1 = b;
            if (buf.size() > 32768) {
                throw new IOException("Header too large");
            }
        }
        return buf.size() > 0 ? buf.toByteArray() : null;
    }

    private static byte[] readExact(InputStream in, int len) throws IOException {
        if (len <= 0) return new byte[0];
        byte[] data = new byte[len];
        int off = 0;
        while (off < len) {
            int n = in.read(data, off, len - off);
            if (n < 0) throw new IOException("Unexpected EOF reading body");
            off += n;
        }
        return data;
    }

    private static void parseQuery(String qs, Map<String, String> out) {
        if (qs == null || qs.isEmpty()) return;
        for (String pair : qs.split("&")) {
            int eq = pair.indexOf('=');
            if (eq > 0) {
                String k = urlDecode(pair.substring(0, eq));
                String v = urlDecode(pair.substring(eq + 1));
                out.put(k, v);
            } else if (!pair.isEmpty()) {
                out.put(urlDecode(pair), "");
            }
        }
    }

    private static String urlDecode(String s) {
        try {
            return URLDecoder.decode(s, StandardCharsets.UTF_8.name());
        } catch (Exception e) {
            return s;
        }
    }

    private static int parseInt(String val, int def, int min, int max) {
        if (val == null || val.isEmpty()) return def;
        try {
            int n = Integer.parseInt(val);
            return Math.max(min, Math.min(max, n));
        } catch (NumberFormatException e) {
            return def;
        }
    }

    private static void sendJson(OutputStream out, int code, String json) throws IOException {
        byte[] body = json.getBytes(StandardCharsets.UTF_8);
        sendBytes(out, code, "application/json; charset=utf-8", body, "");
    }

    private static void sendBytes(OutputStream out, int code, String contentType, byte[] body, String extraHeaders) throws IOException {
        String reason = (code == 200) ? "OK" : (code == 204 ? "No Content" : (code == 400 ? "Bad Request" : "Not Found"));
        String hdr = "HTTP/1.1 " + code + " " + reason + "\r\n"
                + "Content-Type: " + contentType + "\r\n"
                + "Content-Length: " + body.length + "\r\n"
                + (extraHeaders != null ? extraHeaders : "")
                + "Connection: close\r\n\r\n";
        out.write(hdr.getBytes(StandardCharsets.US_ASCII));
        if (body.length > 0) {
            out.write(body);
        }
        out.flush();
    }

    private static class ScanEntry {
        String mac;
        String name;
        int rssi;
        List<String> uuids = new ArrayList<>();
    }

    private List<ScanEntry> scanBle(int ms, String stopMatchName, String stopMatchSuffix) {
        final Map<String, ScanEntry> map = new LinkedHashMap<>();
        if (btAdapter == null || !btAdapter.isEnabled()) {
            sLastError = "Bluetooth disabled or unavailable";
            return Collections.emptyList();
        }
        BluetoothLeScanner scanner = btAdapter.getBluetoothLeScanner();
        if (scanner == null) {
            sLastError = "BluetoothLeScanner unavailable";
            return Collections.emptyList();
        }
        final CountDownLatch stopLatch = new CountDownLatch(1);
        ScanCallback cb = new ScanCallback() {
            @Override
            public void onScanResult(int callbackType, ScanResult result) {
                BluetoothDevice dev = result.getDevice();
                if (dev == null) return;
                String mac = dev.getAddress();
                String name = null;
                if (result.getScanRecord() != null) {
                    name = result.getScanRecord().getDeviceName();
                }
                if (name == null || name.isEmpty()) {
                    try {
                        name = dev.getName();
                    } catch (SecurityException ignored) {
                    }
                }
                if (name == null) name = "";

                List<String> uuidList = new ArrayList<>();
                if (result.getScanRecord() != null && result.getScanRecord().getServiceUuids() != null) {
                    for (android.os.ParcelUuid pu : result.getScanRecord().getServiceUuids()) {
                        uuidList.add(pu.getUuid().toString());
                    }
                }

                synchronized (map) {
                    ScanEntry entry = map.get(mac);
                    if (entry == null) {
                        entry = new ScanEntry();
                        entry.mac = mac;
                        entry.name = name;
                        entry.rssi = result.getRssi();
                        entry.uuids = uuidList;
                        map.put(mac, entry);
                    } else {
                        if (!name.isEmpty()) entry.name = name;
                        entry.rssi = result.getRssi();
                        if (!uuidList.isEmpty()) entry.uuids = uuidList;
                    }
                }

                if (stopMatchName != null) {
                    boolean nameOk = !name.isEmpty() && name.toUpperCase(Locale.ROOT).startsWith(stopMatchName.toUpperCase(Locale.ROOT));
                    boolean macOk = stopMatchSuffix != null && mac.toUpperCase(Locale.ROOT).endsWith(stopMatchSuffix.toUpperCase(Locale.ROOT));
                    if (nameOk || macOk) {
                        stopLatch.countDown();
                    }
                }
            }

            @Override
            public void onScanFailed(int errorCode) {
                sLastError = "Scan failed: " + errorCode;
                stopLatch.countDown();
            }
        };

        ScanSettings settings = new ScanSettings.Builder()
                .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
                .build();
        try {
            scanner.startScan(null, settings, cb);
            stopLatch.await(ms, TimeUnit.MILLISECONDS);
        } catch (SecurityException se) {
            sLastError = "Permission error: " + se.getMessage();
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        } finally {
            try {
                scanner.stopScan(cb);
            } catch (Exception ignored) {
            }
        }

        List<ScanEntry> list;
        synchronized (map) {
            list = new ArrayList<>(map.values());
        }
        list.sort(Comparator.comparingInt((ScanEntry e) -> e.rssi).reversed());
        return list;
    }

    private String handleScan(int ms) {
        sLastError = "";
        List<ScanEntry> list = scanBle(ms, null, null);
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < list.size(); i++) {
            ScanEntry e = list.get(i);
            if (i > 0) sb.append(',');
            sb.append("{\"mac\":").append(jsonStr(e.mac))
              .append(",\"name\":").append(jsonStr(e.name))
              .append(",\"rssi\":").append(e.rssi)
              .append(",\"uuids\":").append(jsonStrList(e.uuids))
              .append('}');
        }
        sb.append(']');
        return sb.toString();
    }

    private String handleConnect(String mac, String targetName) {
        sLastError = "";
        if (btAdapter == null || !btAdapter.isEnabled()) {
            sLastError = "Bluetooth disabled";
            return buildStatusJson();
        }

        String targetMac = (mac != null && !mac.isEmpty()) ? mac.toUpperCase(Locale.ROOT) : null;
        String foundName = "";
        if (targetMac == null) {
            List<ScanEntry> found = scanBle(5000, targetName, "3E:99");
            for (ScanEntry e : found) {
                boolean nameOk = e.name != null && e.name.toUpperCase(Locale.ROOT).startsWith(targetName.toUpperCase(Locale.ROOT));
                boolean macOk = e.mac != null && e.mac.toUpperCase(Locale.ROOT).endsWith("3E:99");
                if (nameOk || macOk) {
                    targetMac = e.mac;
                    foundName = e.name;
                    break;
                }
            }
            if (targetMac == null) {
                sLastError = "Device '" + targetName + "' not found in 5s scan (" + found.size() + " devices seen)";
                return buildStatusJson();
            }
        }

        closeGatt();
        sDeviceMac = targetMac;
        sDeviceName = foundName;
        sMtu = 23;
        serviceUuid = "";
        writeUuid = "";
        notifyUuid = "";
        lastNotifyHex = "";
        discoveredServices = Collections.emptyList();
        discoverStarted.set(false);
        connectLatch = new CountDownLatch(1);

        try {
            BluetoothDevice dev = btAdapter.getRemoteDevice(targetMac);
            if (sDeviceName.isEmpty()) {
                try {
                    String dn = dev.getName();
                    if (dn != null) sDeviceName = dn;
                } catch (SecurityException ignored) {
                }
            }
            BluetoothGattCallback cb = new BluetoothGattCallback() {
                @Override
                public void onConnectionStateChange(BluetoothGatt gatt, int status, int newState) {
                    if (newState == BluetoothProfile.STATE_CONNECTED && status == BluetoothGatt.GATT_SUCCESS) {
                        sConnected = true;
                        startForegroundCompat("Connected: " + sDeviceName + " (" + sDeviceMac + ")");
                        try {
                            gatt.requestMtu(248);
                        } catch (SecurityException se) {
                            sLastError = "requestMtu SE: " + se.getMessage();
                        }
                        mainHandler.postDelayed(() -> {
                            if (discoverStarted.compareAndSet(false, true)) {
                                try {
                                    gatt.discoverServices();
                                } catch (Exception e) {
                                    sLastError = "discoverServices fallback: " + e.getMessage();
                                    releaseConnectLatch();
                                }
                            }
                        }, 1500);
                    } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                        sConnected = false;
                        writeChar = null;
                        notifyChar = null;
                        if (status != BluetoothGatt.GATT_SUCCESS && sLastError.isEmpty()) {
                            sLastError = "Disconnected status=" + status;
                        }
                        startForegroundCompat("Disconnected");
                        releaseConnectLatch();
                    } else if (status != BluetoothGatt.GATT_SUCCESS) {
                        sLastError = "Connection error status=" + status;
                        sConnected = false;
                        releaseConnectLatch();
                    }
                }

                @Override
                public void onMtuChanged(BluetoothGatt gatt, int mtu, int status) {
                    if (status == BluetoothGatt.GATT_SUCCESS) {
                        sMtu = mtu;
                    }
                    if (discoverStarted.compareAndSet(false, true)) {
                        try {
                            gatt.discoverServices();
                        } catch (Exception e) {
                            sLastError = "discoverServices: " + e.getMessage();
                            releaseConnectLatch();
                        }
                    }
                }

                @Override
                public void onServicesDiscovered(BluetoothGatt gatt, int status) {
                    boolean pendingDesc = false;
                    if (status == BluetoothGatt.GATT_SUCCESS) {
                        pendingDesc = selectCharacteristics(gatt);
                    } else {
                        sLastError = "Service discovery failed: " + status;
                    }
                    if (pendingDesc) {
                        mainHandler.postDelayed(() -> releaseConnectLatch(), 1500);
                    } else {
                        releaseConnectLatch();
                    }
                }

                @Override
                public void onDescriptorWrite(BluetoothGatt gatt, BluetoothGattDescriptor descriptor, int status) {
                    releaseConnectLatch();
                }

                @Override
                public void onCharacteristicWrite(BluetoothGatt gatt, BluetoothGattCharacteristic ch, int status) {
                    CountDownLatch l = writeLatch;
                    if (l != null) l.countDown();
                }

                @Override
                public void onCharacteristicChanged(BluetoothGatt gatt, BluetoothGattCharacteristic ch) {
                    @SuppressWarnings("deprecation")
                    byte[] val = ch.getValue();
                    if (val != null) {
                        lastNotifyHex = toHex(val);
                    }
                }

                @Override
                public void onCharacteristicChanged(BluetoothGatt gatt, BluetoothGattCharacteristic ch, byte[] value) {
                    if (value != null) {
                        lastNotifyHex = toHex(value);
                    }
                }
            };

            synchronized (gattLock) {
                currentGatt = dev.connectGatt(this, false, cb, BluetoothDevice.TRANSPORT_LE);
            }
            if (!connectLatch.await(10000, TimeUnit.MILLISECONDS)) {
                if (sLastError.isEmpty()) sLastError = "Connect/discovery timeout (10s)";
            }
        } catch (Throwable t) {
            sLastError = "Connect exception: " + t.getMessage();
        }
        return buildStatusJson();
    }

    private void releaseConnectLatch() {
        CountDownLatch l = connectLatch;
        if (l != null) l.countDown();
    }

    private boolean selectCharacteristics(BluetoothGatt gatt) {
        List<String> svcs = new ArrayList<>();
        BluetoothGattService chosenSvc = null;
        Map<String, BluetoothGattService> byUuid = new HashMap<>();

        for (BluetoothGattService s : gatt.getServices()) {
            String su = s.getUuid().toString().toLowerCase(Locale.ROOT);
            StringBuilder detail = new StringBuilder(su).append(" [");
            List<BluetoothGattCharacteristic> chars = s.getCharacteristics();
            for (int i = 0; i < chars.size(); i++) {
                BluetoothGattCharacteristic c = chars.get(i);
                if (i > 0) detail.append(", ");
                detail.append(c.getUuid().toString().toLowerCase(Locale.ROOT))
                      .append("(0x").append(Integer.toHexString(c.getProperties())).append(')');
            }
            detail.append(']');
            svcs.add(detail.toString());
            byUuid.put(su, s);
        }
        discoveredServices = svcs;

        for (String pref : Arrays.asList(UUID_SVC_AE30, UUID_SVC_AE00, UUID_SVC_FF00, UUID_SVC_AB00)) {
            if (byUuid.containsKey(pref)) {
                chosenSvc = byUuid.get(pref);
                break;
            }
        }

        BluetoothGattCharacteristic w = null;
        BluetoothGattCharacteristic n = null;

        if (chosenSvc != null) {
            serviceUuid = chosenSvc.getUuid().toString().toLowerCase(Locale.ROOT);
            w = findChar(chosenSvc, Arrays.asList(UUID_CHR_AE01, UUID_CHR_FF02, UUID_CHR_AB01));
            if (w == null) {
                for (BluetoothGattCharacteristic c : chosenSvc.getCharacteristics()) {
                    int p = c.getProperties();
                    if ((p & (BluetoothGattCharacteristic.PROPERTY_WRITE | BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE)) != 0) {
                        w = c;
                        break;
                    }
                }
            }
            n = findChar(chosenSvc, Arrays.asList(UUID_CHR_AE02, UUID_CHR_AE04));
            if (n == null) {
                for (BluetoothGattCharacteristic c : chosenSvc.getCharacteristics()) {
                    int p = c.getProperties();
                    if ((p & (BluetoothGattCharacteristic.PROPERTY_NOTIFY | BluetoothGattCharacteristic.PROPERTY_INDICATE)) != 0) {
                        n = c;
                        break;
                    }
                }
            }
        } else {
            for (BluetoothGattService s : gatt.getServices()) {
                for (BluetoothGattCharacteristic c : s.getCharacteristics()) {
                    int p = c.getProperties();
                    if (w == null && (p & (BluetoothGattCharacteristic.PROPERTY_WRITE | BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE)) != 0) {
                        chosenSvc = s;
                        serviceUuid = s.getUuid().toString().toLowerCase(Locale.ROOT);
                        w = c;
                    }
                }
            }
            if (chosenSvc != null) {
                for (BluetoothGattCharacteristic c : chosenSvc.getCharacteristics()) {
                    int p = c.getProperties();
                    if ((p & (BluetoothGattCharacteristic.PROPERTY_NOTIFY | BluetoothGattCharacteristic.PROPERTY_INDICATE)) != 0) {
                        n = c;
                        break;
                    }
                }
            }
        }

        writeChar = w;
        notifyChar = n;
        writeUuid = (w != null) ? w.getUuid().toString().toLowerCase(Locale.ROOT) : "";
        notifyUuid = (n != null) ? n.getUuid().toString().toLowerCase(Locale.ROOT) : "";

        boolean pendingDescriptor = false;
        if (n != null) {
            try {
                gatt.setCharacteristicNotification(n, true);
                BluetoothGattDescriptor desc = n.getDescriptor(UUID_CCCD);
                if (desc != null) {
                    if (Build.VERSION.SDK_INT >= 33) {
                        int rc = gatt.writeDescriptor(desc, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE);
                        pendingDescriptor = (rc == BluetoothStatusCodes.SUCCESS);
                    } else {
                        @SuppressWarnings("deprecation")
                        boolean ignored = desc.setValue(BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE);
                        @SuppressWarnings("deprecation")
                        boolean started = gatt.writeDescriptor(desc);
                        pendingDescriptor = started;
                    }
                }
            } catch (SecurityException se) {
                sLastError = "Notify setup SE: " + se.getMessage();
            }
        }
        if (w == null) {
            sLastError = "No writable characteristic found";
        }
        return pendingDescriptor;
    }

    private static BluetoothGattCharacteristic findChar(BluetoothGattService svc, List<String> prefs) {
        for (String pref : prefs) {
            for (BluetoothGattCharacteristic c : svc.getCharacteristics()) {
                if (pref.equalsIgnoreCase(c.getUuid().toString())) {
                    return c;
                }
            }
        }
        return null;
    }

    private String handleWrite(byte[] body, int maxChunk, int delayMs) {
        BluetoothGatt gatt;
        BluetoothGattCharacteristic ch;
        synchronized (gattLock) {
            gatt = currentGatt;
            ch = writeChar;
        }
        if (!sConnected || gatt == null || ch == null) {
            return "{\"ok\":false,\"error\":\"not connected or no write characteristic\"}";
        }

        long t0 = System.currentTimeMillis();
        int effectiveChunk = Math.min(maxChunk, Math.max(20, sMtu - 3));
        int props = ch.getProperties();
        int writeType = ((props & BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE) != 0)
                ? BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
                : BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT;

        int chunks = 0;
        int sent = 0;
        try {
            for (int off = 0; off < body.length; off += effectiveChunk) {
                int len = Math.min(effectiveChunk, body.length - off);
                byte[] slice = Arrays.copyOfRange(body, off, off + len);

                writeLatch = new CountDownLatch(1);
                boolean started = false;
                int lastRc = 0;
                for (int attempt = 0; attempt < 10; attempt++) {
                    if (Build.VERSION.SDK_INT >= 33) {
                        lastRc = gatt.writeCharacteristic(ch, slice, writeType);
                        started = (lastRc == BluetoothStatusCodes.SUCCESS);
                        if (started) break;
                        if (lastRc == BluetoothStatusCodes.ERROR_GATT_WRITE_REQUEST_BUSY) {
                            Thread.sleep(30);
                            continue;
                        }
                        return "{\"ok\":false,\"error\":\"writeCharacteristic rc=" + lastRc + "\",\"bytesSent\":" + sent + "}";
                    } else {
                        @SuppressWarnings("deprecation")
                        boolean vOk = ch.setValue(slice);
                        ch.setWriteType(writeType);
                        @SuppressWarnings("deprecation")
                        boolean wOk = gatt.writeCharacteristic(ch);
                        started = vOk && wOk;
                        if (started) break;
                        Thread.sleep(30);
                    }
                }
                if (!started) {
                    return "{\"ok\":false,\"error\":\"writeCharacteristic busy/failed rc=" + lastRc + "\",\"bytesSent\":" + sent + "}";
                }
                writeLatch.await(80, TimeUnit.MILLISECONDS);
                sent += len;
                chunks++;
                if (delayMs > 0) {
                    Thread.sleep(delayMs);
                }
            }
            Thread.sleep(100);
        } catch (SecurityException se) {
            return "{\"ok\":false,\"error\":" + jsonStr(se.getMessage()) + ",\"bytesSent\":" + sent + "}";
        } catch (InterruptedException ie) {
            Thread.currentThread().interrupt();
            return "{\"ok\":false,\"error\":\"interrupted\",\"bytesSent\":" + sent + "}";
        }

        long elapsed = System.currentTimeMillis() - t0;
        return "{\"ok\":true,\"bytesSent\":" + sent + ",\"chunks\":" + chunks
                + ",\"effectiveChunk\":" + effectiveChunk
                + ",\"ms\":" + elapsed
                + ",\"lastNotifyHex\":" + jsonStr(lastNotifyHex) + "}";
    }

    private void closeGatt() {
        synchronized (gattLock) {
            if (currentGatt != null) {
                try {
                    currentGatt.disconnect();
                } catch (Exception ignored) {
                }
                try {
                    currentGatt.close();
                } catch (Exception ignored) {
                }
                currentGatt = null;
            }
            writeChar = null;
            notifyChar = null;
            sConnected = false;
        }
    }

    private String buildStatusJson() {
        return "{\"connected\":" + sConnected
                + ",\"name\":" + jsonStr(sDeviceName)
                + ",\"mac\":" + jsonStr(sDeviceMac)
                + ",\"mtu\":" + sMtu
                + ",\"serviceUuid\":" + jsonStr(serviceUuid)
                + ",\"writeUuid\":" + jsonStr(writeUuid)
                + ",\"notifyUuid\":" + jsonStr(notifyUuid)
                + ",\"lastNotifyHex\":" + jsonStr(lastNotifyHex)
                + ",\"services\":" + jsonStrList(discoveredServices)
                + ",\"lastError\":" + jsonStr(sLastError) + "}";
    }

    private static String toHex(byte[] data) {
        StringBuilder sb = new StringBuilder(data.length * 2);
        for (byte b : data) {
            sb.append(String.format(Locale.ROOT, "%02x", b & 0xff));
        }
        return sb.toString();
    }

    private static String jsonStr(String s) {
        if (s == null) return "null";
        StringBuilder sb = new StringBuilder("\"");
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"' || c == '\\') sb.append('\\').append(c);
            else if (c == '\n') sb.append("\\n");
            else if (c == '\r') sb.append("\\r");
            else if (c < 0x20) sb.append(String.format(Locale.ROOT, "\\u%04x", (int) c));
            else sb.append(c);
        }
        sb.append('"');
        return sb.toString();
    }

    private static String jsonStrList(List<String> list) {
        StringBuilder sb = new StringBuilder("[");
        if (list != null) {
            for (int i = 0; i < list.size(); i++) {
                if (i > 0) sb.append(',');
                sb.append(jsonStr(list.get(i)));
            }
        }
        sb.append(']');
        return sb.toString();
    }
}
