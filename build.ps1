# Сборка LCD Studio. По умолчанию в dist\LCD Studio; -Dist задаёт другую папку
param([string]$Dist = "")
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$py = "$root\.venv\Scripts\python.exe"
if (-not $Dist) { $Dist = "$root\dist" }
New-Item -ItemType Directory -Force "$root\build" | Out-Null
if (-not (Test-Path "$root\lhm\LibreHardwareMonitorLib.dll")) {
    & $py "$root\tools\fetch_lhm.py" "$root\lhm"
    if ($LASTEXITCODE) { throw "Не удалось скачать LibreHardwareMonitorLib" }
}

$exclude = "tkinter", "PySide6.QtWebEngineCore", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore",
    "PySide6.QtMultimedia", "PySide6.QtPdf", "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtNetwork"
$piArgs = @("-m", "PyInstaller", "--noconfirm", "--windowed", "--name", "LCD Studio", "--icon", "$root\icon.ico",
    "--distpath", $Dist, "--workpath", "$root\build", "--specpath", "$root\build",
    # датчики: LibreHardwareMonitor (.NET) через pythonnet
    "--add-data", "$root\lhm;lhm", "--hidden-import", "clr", "--collect-all", "pythonnet", "--collect-all", "clr_loader")
foreach ($m in $exclude) { $piArgs += "--exclude-module", $m }
# PyInstaller пишет предупреждения в stderr, PowerShell 5.1 считает это ошибкой
$ErrorActionPreference = "Continue"
& $py @piArgs "$root\run.pyw" 2>&1 | Out-File "$root\build\pyinstaller.log" -Encoding utf8
$ErrorActionPreference = "Stop"
if ($LASTEXITCODE) { throw "PyInstaller завершился с ошибкой, см. build\pyinstaller.log" }

# Qt тянет модули, которые приложению не нужны
$int = "$Dist\LCD Studio\_internal"
$junk = "PySide6\opengl32sw.dll", "PySide6\Qt6Quick.dll", "PySide6\Qt6Qml.dll", "PySide6\Qt6QmlModels.dll",
    "PySide6\Qt6QmlMeta.dll", "PySide6\Qt6QmlWorkerScript.dll", "PySide6\Qt6Pdf.dll", "PySide6\Qt6VirtualKeyboard.dll",
    "PySide6\Qt6OpenGL.dll", "PySide6\Qt6Network.dll", "PySide6\translations",
    "PySide6\plugins\imageformats\qpdf.dll", "PySide6\plugins\platforminputcontexts", "PySide6\plugins\networkinformation",
    "PySide6\plugins\tls", "PIL\_avif.cp314-win_amd64.pyd"
foreach ($j in $junk) { Remove-Item "$int\$j" -Recurse -Force -ErrorAction SilentlyContinue }

# SDK USB-экрана и драйвер libusb из комплекта LCD Control (не в git — это файлы производителя)
$vendorSrc = if ($env:LCDSTUDIO_VENDOR_SRC) { $env:LCDSTUDIO_VENDOR_SRC } else { "$root\..\inspection\app" }
if (Test-Path "$vendorSrc\dll\x64\MSDISPLAYSDKWRRAPER.dll") {
    $v = "$Dist\LCD Studio\vendor"
    New-Item -ItemType Directory -Force $v | Out-Null
    Copy-Item "$vendorSrc\dll\x64\MSDISPLAYSDKWRRAPER.dll", "$vendorSrc\dll\x64\AicUsbDisplay.dll" $v
    Copy-Item "$vendorSrc\libusb" $v -Recurse -Force
} else {
    Write-Host "Внимание: SDK экрана не найден в $vendorSrc — USB-экран заработает только при установленной LCD Control"
}

$mb = [math]::Round((Get-ChildItem "$Dist\LCD Studio" -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
Write-Host "Готово: $Dist\LCD Studio\LCD Studio.exe ($mb МБ)"
