# Jarvis - Voice Assistant for Windows

Local, fast voice assistant for Windows. Wake word: **"Jarvis"**. Supports mixed English/Hindi commands. **No cloud AI/APIs** (local `faster-whisper` + `Vosk` + `pyttsx3`).

## Run (recommended)
```powershell
cd C:\Users\music\jarvis-assistant
.\start_jarvis.bat
# or: .\.venv312\Scripts\python.exe jarvis.py
```

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
