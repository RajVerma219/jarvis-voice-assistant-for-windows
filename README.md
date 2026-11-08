# Jarvis - Voice Assistant for Windows

Local, fast voice assistant for Windows. Wake word: **"Jarvis"**. Supports mixed English/Hindi commands. **No cloud AI/APIs** (local `faster-whisper` + `Vosk` + `pyttsx3`).

## Run (recommended)
```powershell
cd C:\Users\music\jarvis-assistant
.\start_jarvis.bat
# or: .\.venv312\Scripts\python.exe jarvis.py
```

## Run always in background (no terminal window)
Use this so closing a terminal won’t stop Jarvis. It runs `pythonw.exe` hidden and writes logs to `jarvis_background.log`.

- **Start hidden now:** double-click `run_jarvis_background.vbs` (or run `wscript.exe "C:\Users\music\jarvis-assistant\run_jarvis_background.vbs"`).
- **Stop background Jarvis:** run `powershell -ExecutionPolicy Bypass -File .\stop_jarvis_background.ps1` (stops Jarvis processes launched from this folder).
- **Auto-start at Windows sign-in:** run `powershell -ExecutionPolicy Bypass -File .\install_jarvis_startup.ps1`. It creates `Jarvis Assistant.lnk` in your Startup folder. Remove that shortcut to disable auto-start.
- **Logs:** `C:\Users\music\jarvis-assistant\jarvis_background.log`
- For debugging, set `JARVIS_DEBUG_AUDIO=1` before starting (or edit the VBS env vars).

> One running instance is enough. If you use Startup + manual start, stop the old one first (`stop_jarvis_background.ps1`) to avoid duplicate mic listeners.

## Speed tuning (`jarvis.py`)
- `WHISPER_DEVICE="cuda"` (GPU, fastest if available) / `"cpu"`
- `WHISPER_MODEL_SIZE="tiny"` fastest, `"base"` balanced (default), `"small"` more accurate
- `SILENCE_AFTER_SPEECH=0.35` (lower = faster response; too low may cut speech)
- `WAKE_SPEECH_RMS_THRESH` / `SPEECH_RMS_THRESH`: lower = hears faster; raise if noisy/false wakes
- **Microphone**: always selects `USB PnP Audio Device` when detected (prefers WASAPI). Override: `set JARVIS_DEVICE=<index|substring>`; system default: `set JARVIS_DEVICE=default`. Preference list: `set JARVIS_MIC_PREFER=usb pnp audio device`

## Commands
- `Jarvis, open YouTube` / `open YouTube Music` / `open YouTube Studio` (Chrome)
- `Jarvis, play music` / `gaana chalao`
- `Jarvis, open I drive`, `go to Program Files`, `open/close Chrome`
- `Jarvis, volume 40`, `screenshot`, `lock PC` (shutdown/restart ask confirmation)

## Tablet control (ADB, Android/Samsung)
Configured for Galaxy Tab A 2017 (SM-T385) on the same Wi‑Fi via ADB.

- Requires Android Platform Tools (`adb`) and Wireless debugging/ADB enabled. Default config: `JARVIS_TABLET_ADB=C:\Users\music\platform-tools\adb.exe`, `JARVIS_TABLET_SERIAL=192.168.1.2:5555` (override with env vars).
- Reconnect if needed: `adb connect 192.168.1.2:5555` (tablet IP may change; reserve it in your router).
- Example voice commands: `Jarvis, tablet status`, `tablet home`, `tablet back`, `tablet open YouTube [query]`, `tablet close YouTube` / `tablet force stop Chrome`, `tablet play pause`, `tablet volume up/down`, `tablet wake`, `tablet screen off`, `tablet reconnect`, `tablet open settings/chrome`.
- Notes: tablet must be reachable on Wi‑Fi; some actions depend on lock state; unlock/swipe is best-effort and may vary by device/security settings. Closing uses ADB `am force-stop` (the app stops; unsaved in-app state may be lost).

## Build Jarvis.exe (dedicated Windows app)
Requires .venv312 with dependencies installed.

`powershell
cd C:\Users\music\jarvis-assistant
powershell -ExecutionPolicy Bypass -File .\build_jarvis_exe.ps1
`

- Output: dist\Jarvis\Jarvis.exe (onedir). Keep the whole dist\Jarvis\ folder together.
- Keep osk-model-* folders beside Jarvis.exe (or in its working directory).
- ADB/tablet: place platform-tools\adb.exe beside Jarvis.exe (or dist\Jarvis\platform-tools\adb.exe) or set JARVIS_TABLET_ADB.
- Build is --windowed (no console). For debugging, rebuild without --windowed.
