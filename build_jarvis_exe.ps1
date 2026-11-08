# Build Jarvis.exe (onedir, recommended for ML deps). Run from repo root.
# Output: dist\Jarvis\Jarvis.exe
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$py = Join-Path $root '.venv312\Scripts\python.exe'
if (!(Test-Path $py)) { throw "Missing $py" }
& $py -m PyInstaller --noconfirm --clean --name Jarvis --windowed --onedir `
  --collect-all faster_whisper `
  --collect-all ctranslate2 `
  --collect-all vosk `
  --collect-all sounddevice `
  --collect-all pyttsx3 `
  --collect-binaries vosk `
  --collect-binaries ctranslate2 `
  --hidden-import comtypes.stream `
  jarvis.py
Write-Output "Built: $root\dist\Jarvis\Jarvis.exe"
Write-Output "Keep Vosk model folders next to Jarvis.exe (or in working dir). ADB: set JARVIS_TABLET_ADB or place platform-tools\adb.exe beside Jarvis.exe."
