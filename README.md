# Jarvis — Voice Assistant for Windows

Jarvis is a local voice assistant for Windows (Python) with wake-word activation, English/Hindi command support, PC automation, YouTube/YouTube Music playback control, and optional Android tablet control over ADB. It is designed to run locally (speech recognition uses local models; no cloud AI API is required for core command handling).

> **Status:** personal/local project. Features depend on your mic, installed apps, Chrome, ADB setup, and device/network state.

## Features (current)

### Wake word & speech
- **Wake word:** `Jarvis` (with common mishearing aliases/corrections in `jarvis.py`).
- **Listening flow:** listens for wake word, gives audible/TTS acknowledgement (“Yes, I'm listening.” / Hindi equivalent depending on mode), then captures a command.
- **Speech stack:** local `faster-whisper` (when available) + `vosk` fallback; `pyttsx3` for TTS.
- **Language modes:** `auto` (mixed English/Hindi), `en`, `hi` (configurable in code/env behavior; see `jarvis.py`).
- **Audio tuning:** configurable mic device, RMS/wake thresholds, Whisper device/model, debug audio flags (see Environment variables).

### PC / Windows commands
Examples implemented in `execute_command` (exact behavior depends on recognized text):
- **Apps/system:** open/close common apps (Chrome, etc.), lock PC; shutdown/restart flows include confirmation handling where implemented.
- **Media keys:** play/pause/next/previous via Windows media key events (useful for YouTube in Chrome or active media session).
- **YouTube/Chrome playback toggles (PC):** commands like pause/resume/toggle play for YouTube/Chrome contexts (routes to media toggle where appropriate; avoids hijacking “play <query>” requests).
- **YouTube (PC):** open YouTube/search; “play <query> on YouTube” attempts to resolve a top video ID and open a direct `watch?v=...` URL in Chrome; falls back to search if resolution fails.
- **YouTube Music (PC):** open/play/search YouTube Music in Chrome (direct watch/radio URL when resolvable, otherwise search).
- **Files/folders:** open common locations (Downloads, Documents, Desktop, etc.), basic file/folder/open-path handling.
- **Web/search:** Google/search helpers (as implemented).
- **Input/utility:** type text, screenshots, volume/brightness/system helpers where supported by dependencies/device.

> Command phrases are normalized (including some Hindi/Hinglish aliases). If a command is misheard, check the `[Command]` / debug output and add/adjust ASR corrections.

### Android tablet control (ADB)
Tablet control is implemented for Android devices reachable via ADB (tested target in config: Galaxy Tab A 2017 / SM-T385; works with other Android devices if ADB commands are supported).

- **Transport:** ADB USB or Wi‑Fi (`adb connect <ip>:5555`).
- **Device targeting:** uses configured serial (`JARVIS_TABLET_SERIAL`) with dynamic ADB resolution and fallback behavior (tries reconnect / other connected device where applicable). Set `JARVIS_TABLET_ADB` if needed.
- **Core commands:** `tablet status`, `tablet reconnect`, `tablet home`, `tablet back`, `tablet recent`, `tablet wake`, `tablet screen off` / `lock tablet`, `tablet unlock` (best-effort swipe/keyevent).
- **Apps/URLs:** `tablet open YouTube [query]`, `tablet open Chrome`, `tablet open settings`, `tablet open <app>` (known mappings + package lookup fallback), `tablet close <app>` / `force stop` (uses `am force-stop`; unsaved state may be lost), `tablet open <url>`.
- **Media:** `tablet play pause` / pause/resume/toggle intents, next/previous (keyevents). YouTube/Chrome playback toggle intents on tablet route to media keyevent when appropriate.
- **Volume:** absolute and relative percent-based media volume, e.g. `tablet volume 50`, `tablet volume 100`, `tablet volume up 10`, `tablet volume down 20`, `tablet awaz 25`, `tablet mute` (implementation reads/sets Android media stream via ADB and falls back when needed).
- **YouTube/YouTube Music on tablet:** “play <query> on tablet” defaults to YouTube on tablet; direct video playback attempts `vnd.youtube:<id>` / `m.youtube.com/watch?v=...` intents; YouTube Music commands use YT Music intents/URLs when applicable.
- **Aliases:** you can say **`tablet`** or **`tab`** (normalized).

**Tablet limitations:** tablet must be on the same network (for Wi‑Fi ADB), ADB authorized, and often awake/unlocked for UI actions. Wireless ADB can drop; tablet IP may change. Not all Android OEM builds behave identically.

## Requirements

- **OS:** Windows 10/11 (project uses Windows audio/TTS/system APIs and `.bat`/PowerShell/VBS helpers).
- **Python:** 3.11/3.12 recommended for local venv use (repo includes `.venv312` usage patterns). Some dependencies may behave differently on newer Python versions.
- **Microphone:** working input device; optional USB mic preferred by default config.
- **Google Chrome:** recommended for YouTube/YouTube Music PC flows.
- **ADB (optional):** required only for Android tablet control (`platform-tools`).

## Setup (from source)

```powershell
cd C:\Users\music\jarvis-assistant
python -m venv .venv312
.\.venv312\Scripts\python.exe -m pip install --upgrade pip
.\.venv312\Scripts\python.exe -m pip install -r requirements.txt
```

### Vosk models
`jarvis.py` expects Vosk model folders (see `VOSK_MODELS` / `ensure_model` logic). Place/extract the configured models in the app directory, e.g.:
- `vosk-model-small-en-us-0.15`
- `vosk-model-small-hi-0.22`

Large model folders are gitignored.

### Faster Whisper
Whisper model download/loading is handled by `faster-whisper` when used. First run may take time depending on model/device.

## Run

### Terminal (recommended for debugging)
```powershell
cd C:\Users\music\jarvis-assistant
.\start_jarvis.bat
# or
.\.venv312\Scripts\python.exe jarvis.py
```

### Background (no terminal window)
- **Start hidden:** `run_jarvis_background.vbs` (uses `pythonw.exe`, logs to `jarvis_background.log`).
- **Stop:** `powershell -ExecutionPolicy Bypass -File .\stop_jarvis_background.ps1`
- **Auto-start at sign-in:** `powershell -ExecutionPolicy Bypass -File .\install_jarvis_startup.ps1` (creates Startup shortcut; remove it to disable).
- **Logs:** `jarvis_background.log`

Run **only one** Jarvis instance at a time (duplicate mic listeners cause conflicts).

## Packaged EXE (PyInstaller)

Build:
```powershell
cd C:\Users\music\jarvis-assistant
powershell -ExecutionPolicy Bypass -File .\build_jarvis_exe.ps1
```

- Output: `dist\Jarvis\Jarvis.exe` (onedir, windowed). Keep the whole `dist\Jarvis\` folder together.
- Copy/keep `vosk-model-*` folders beside `Jarvis.exe` (or in working dir expected by `get_app_dir()` behavior).
- For tablet control, include `platform-tools\adb.exe` (+ required DLLs) as `dist\Jarvis\platform-tools\adb.exe`, or set `JARVIS_TABLET_ADB`.
- `dist\Jarvis\Run Jarvis (safe).bat` can launch with safer defaults (CPU/base/default mic) when present.
- Diagnostics: `jarvis_exe.log` (startup/runtime diagnostics added for frozen/EXE runs).

`build/`, `dist/`, and `*.spec` are gitignored; build artifacts are generated locally.

## Environment variables (common)

| Variable | Purpose |
|---|---|
| `JARVIS_DEVICE` | Mic device index/name/`default`. Scripts may set defaults; EXE-safe code uses default mic when frozen unless overridden. |
| `JARVIS_MIC_PREFER` | Preferred mic substrings (e.g. `usb pnp audio,usb pnp`). |
| `JARVIS_WHISPER_MODEL` | Whisper model size (e.g. `tiny`, `base`, `small`). EXE defaults are tuned for stability. |
| `JARVIS_WHISPER_DEVICE` | `cpu` or `cuda` (use `cpu` if CUDA/cuBLAS not configured). |
| `JARVIS_DEBUG_AUDIO` | Enable extra audio/debug output where supported. |
| `JARVIS_WAKE_MIN_RMS` | Wake sensitivity/noise tuning (used by startup scripts where configured). |
| `JARVIS_TABLET_ADB` | Path to `adb.exe`. |
| `JARVIS_TABLET_SERIAL` | Tablet ADB serial, e.g. `192.168.1.2:5555` (update when tablet IP changes). |

See `start_jarvis.bat`, `run_jarvis_background.vbs`, and `jarvis.py` for effective defaults.

## Command examples

> Always start with the wake word: **“Jarvis …”** (recognition aliases exist). Include **`tablet`/`tab`** when targeting the tablet.

### PC
- `Jarvis, open YouTube` / `open YouTube Music`
- `Jarvis, play <song> on YouTube`
- `Jarvis, play music` / `gaana chalao`
- `Jarvis, pause YouTube` / `resume YouTube` / `play pause` (media toggle behavior depends on active target)
- `Jarvis, open Chrome` / `open Downloads` / `open Documents`
- `Jarvis, screenshot` / `lock PC` / `volume 40`

### Tablet (ADB)
- `Jarvis, tablet status` / `tablet reconnect`
- `Jarvis, tablet open YouTube` / `tablet play <song/video> on YouTube`
- `Jarvis, tablet play <song> on YouTube Music`
- `Jarvis, play <anything> on tablet` (defaults to tablet YouTube playback flow)
- `Jarvis, tablet play pause` / `tablet next` / `tablet previous`
- `Jarvis, tablet volume 50` / `tablet volume 100` / `tablet volume up 10` / `tablet volume down 10`
- `Jarvis, tablet home` / `tablet back` / `tablet wake` / `tablet screen off`
- `Jarvis, tablet open Chrome` / `tablet open Settings` / `tablet close YouTube`

## Troubleshooting

### Jarvis not responding
- Confirm only one instance is running (terminal/VBS/EXE/Startup).
- Check mic permissions in Windows and selected input device (`JARVIS_DEVICE=default` is safest first).
- First run may be slow while speech models load.
- Review `jarvis_background.log` or `jarvis_exe.log`.
- For debugging: run from terminal (`start_jarvis.bat`) or enable debug flags.

### Wake word/commands misheard
- Check recognized text (`[Command]`/debug output). Add ASR corrections or command aliases in `jarvis.py`.
- Hindi/Hinglish phrases may need normalization rules; include the exact heard phrase when reporting issues.

### Tablet commands fail
- Run `adb devices` — target must show `device` (not `unauthorized`/`offline`).
- Reconnect Wi‑Fi ADB: `adb connect <tablet-ip>:5555`; update `JARVIS_TABLET_SERIAL` if IP changed.
- Ensure tablet and PC are on the same network; wireless debugging/ADB is enabled and authorized.
- Keep tablet awake/unlocked for UI-dependent actions. Check `jarvis_exe.log` / console `[Tablet ADB]` lines.

### YouTube/YouTube Music doesn’t play directly
- Verify playback works manually on the same device (PC Chrome or tablet YouTube/YT Music).
- **PC:** Chrome profile/extensions/ad blockers/autoplay/site sound settings can affect playback. Jarvis resolves top video IDs via network lookups when possible; if resolution fails it may open search results.
- **Tablet:** update YouTube/YT Music apps, check internet, account/region/restrictions, app cache, and media output/volume.
- ADB can open intents, but actual playback can still be blocked by app state, sign-in, network, or content restrictions.

## Repository layout

- `jarvis.py` — main assistant logic (wake word, STT/TTS, command parsing, PC/tablet/YouTube handlers).
- `start_jarvis.bat` — terminal launcher with common env defaults.
- `run_jarvis_background.vbs` — hidden background launcher (`pythonw`).
- `stop_jarvis_background.ps1` — stop background Jarvis processes from this folder.
- `install_jarvis_startup.ps1` — create Startup shortcut.
- `build_jarvis_exe.ps1` — PyInstaller build script.
- `_mic_diag.py` — microphone diagnostic helper.
- `requirements.txt` — Python dependencies.
- `.gitignore` — excludes venvs, models, logs, build artifacts, local config.

## Safety notes

- Voice control can trigger real actions (apps, URLs, system commands, ADB input). Use only on devices/networks you control.
- Shutdown/restart paths should require confirmation where implemented; review before enabling unattended use.
- Tablet `close` uses `am force-stop` (may lose unsaved app state).
- Do not commit secrets, tokens, or personal IP/device identifiers you don’t want public.

## License

No license file is currently included. Add a `LICENSE` file if you want to define reuse/distribution terms.
