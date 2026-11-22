$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$BuildDir = Join-Path $Root "build"
$ClassesDir = Join-Path $BuildDir "classes"
$DexDir = Join-Path $BuildDir "dex"
New-Item -ItemType Directory -Force -Path $ClassesDir, $DexDir | Out-Null

$SdkRoot = Join-Path $env:LOCALAPPDATA "Android\Sdk"
$BuildTools = Join-Path $SdkRoot "build-tools\35.0.0"
$AndroidJar = Join-Path $SdkRoot "platforms\android-35\android.jar"
$Adb = Join-Path $SdkRoot "platform-tools\adb.exe"

$JdkBin = $null
if ($env:JAVA_HOME -and (Test-Path (Join-Path $env:JAVA_HOME "bin\javac.exe"))) {
    $JdkBin = Join-Path $env:JAVA_HOME "bin"
} else {
    $AutoJavac = Get-ChildItem -Path (Join-Path $env:USERPROFILE ".gradle\jdks") -Filter "javac.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($AutoJavac) {
        $JdkBin = Split-Path -Parent $AutoJavac.FullName
    } else {
        $PathJavac = Get-Command javac.exe -ErrorAction SilentlyContinue
        if ($PathJavac) { $JdkBin = Split-Path -Parent $PathJavac.Source }
    }
}
if (-not $JdkBin) { throw "JDK not found. Please install JDK 11+ and set JAVA_HOME." }
$Javac = Join-Path $JdkBin "javac.exe"
$Keytool = Join-Path $JdkBin "keytool.exe"

Write-Host "1) Compiling Java sources..."
$JavaFiles = Get-ChildItem -Path (Join-Path $Root "src") -Recurse -Filter "*.java" | ForEach-Object { $_.FullName }
& $Javac --release 11 -cp $AndroidJar -d $ClassesDir @JavaFiles
if ($LASTEXITCODE -ne 0) { throw "javac failed" }

Write-Host "2) Converting classes to classes.dex via d8..."
$ClassFiles = Get-ChildItem -Path $ClassesDir -Recurse -Filter "*.class" | ForEach-Object { $_.FullName }
& (Join-Path $BuildTools "d8.bat") --min-api 26 --lib $AndroidJar --output $DexDir @ClassFiles
if ($LASTEXITCODE -ne 0) { throw "d8 failed" }

Write-Host "3) Compiling resources & linking manifest with aapt2..."
$UnsignedApk = Join-Path $BuildDir "unsigned.apk"
if (Test-Path $UnsignedApk) { Remove-Item $UnsignedApk -Force }
$ResZip = Join-Path $BuildDir "res.zip"
if (Test-Path $ResZip) { Remove-Item $ResZip -Force }
$ResDir = Join-Path $Root "res"
if (Test-Path $ResDir) {
    & (Join-Path $BuildTools "aapt2.exe") compile --dir $ResDir -o $ResZip
    if ($LASTEXITCODE -ne 0) { throw "aapt2 compile failed" }
    & (Join-Path $BuildTools "aapt2.exe") link -I $AndroidJar --manifest (Join-Path $Root "AndroidManifest.xml") -o $UnsignedApk $ResZip
} else {
    & (Join-Path $BuildTools "aapt2.exe") link -I $AndroidJar --manifest (Join-Path $Root "AndroidManifest.xml") -o $UnsignedApk
}
if ($LASTEXITCODE -ne 0) { throw "aapt2 link failed" }

Write-Host "4) Adding classes.dex into APK..."
$DexPath = Join-Path $DexDir "classes.dex"
py -3.12 -c "import zipfile; z=zipfile.ZipFile(r'$UnsignedApk','a',compression=zipfile.ZIP_DEFLATED); z.write(r'$DexPath','classes.dex'); z.close()"
if ($LASTEXITCODE -ne 0) { throw "Adding classes.dex failed" }

Write-Host "5) Zipaligning..."
$AlignedApk = Join-Path $BuildDir "aligned.apk"
& (Join-Path $BuildTools "zipalign.exe") -f 4 $UnsignedApk $AlignedApk
if ($LASTEXITCODE -ne 0) { throw "zipalign failed" }

Write-Host "6) Signing APK..."
$Keystore = Join-Path $BuildDir "debug.keystore"
if (-not (Test-Path $Keystore)) {
    & $Keytool -genkeypair -v -keystore $Keystore -storepass android -alias androiddebugkey -keypass android -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=TinyPrintBridge,O=Local,C=VN"
    if ($LASTEXITCODE -ne 0) { throw "keytool failed" }
}
$SignedApk = Join-Path $BuildDir "TinyPrintBridge.apk"
& (Join-Path $BuildTools "apksigner.bat") sign --ks $Keystore --ks-pass pass:android --key-pass pass:android --out $SignedApk $AlignedApk
if ($LASTEXITCODE -ne 0) { throw "apksigner failed" }

Write-Host "7) Detecting ADB device..."
$DevicesRaw = (& $Adb devices) | Where-Object { $_ -match "\tdevice$" }
$TargetSerial = $null
if ($DevicesRaw) {
    # Prefer USB device (serial without colon/port)
    $UsbDev = $DevicesRaw | Where-Object { $_ -notmatch ":\d+" } | Select-Object -First 1
    if ($UsbDev) {
        $TargetSerial = ($UsbDev -split "\s+")[0].Trim()
    } else {
        $TargetSerial = ($DevicesRaw[0] -split "\s+")[0].Trim()
    }
    Write-Host "Using target ADB device: $TargetSerial"
}

$AdbCmd = @()
if ($TargetSerial) {
    $AdbCmd = @("-s", $TargetSerial)
}

Write-Host "8) Installing APK via ADB..."
$InstallOut = & $Adb @AdbCmd install -r $SignedApk 2>&1
$InstallOut | Out-String | Write-Host
if ($LASTEXITCODE -ne 0) { throw "adb install failed: $InstallOut" }

Write-Host "9) Granting runtime permissions..."
$Perms = @(
    "android.permission.BLUETOOTH_SCAN",
    "android.permission.BLUETOOTH_CONNECT",
    "android.permission.ACCESS_FINE_LOCATION",
    "android.permission.ACCESS_COARSE_LOCATION",
    "android.permission.POST_NOTIFICATIONS",
    "android.permission.CAMERA"
)
foreach ($perm in $Perms) {
    & $Adb @AdbCmd shell pm grant com.tinyprint.bridge $perm 2>&1 | Out-Null
}

Write-Host "10) Starting BleBridgeService and forwarding tcp:9100..."
& $Adb @AdbCmd shell am start-foreground-service -n com.tinyprint.bridge/.BleBridgeService
& $Adb @AdbCmd shell am start -W -n com.tinyprint.bridge/.BridgeActivity
& $Adb @AdbCmd forward tcp:9100 tcp:9100
Write-Host "DONE! Bridge ready at http://127.0.0.1:9100"
