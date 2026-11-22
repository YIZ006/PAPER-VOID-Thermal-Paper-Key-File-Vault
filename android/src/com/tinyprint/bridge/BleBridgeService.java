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
import android.content.pm.ServiceInfo;
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
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(1, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE);
        } else {
            startForeground(1, notif);
        }
    }

    private synchronized void startHttpServer() {
        if (running) return;
        running = true;
        serverThread = new Thread(() -> {
            try {
                serverSocket = new ServerSocket(PORT, 16, InetAddress.getByName("0.0.0.0"));
                while (running) {
                    Socket sock = serverSocket.accept();
                    new Thread(() -> handleClient(sock), "BridgeClient").start();
                }
            } catch (IOException e) {
                sLastError = "ServerSocket: " + e.getMessage();
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
