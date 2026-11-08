@echo off
setlocal
cd /d "%~dp0"
set HF_HUB_DISABLE_SYMLINKS_WARNING=1
REM CUDA Whisper probe on this PC reported missing cuBLAS runtime (cublas64_12.dll) and can hang on Processing.
REM Default to CPU for reliable local operation. Set JARVIS_WHISPER_DEVICE=cuda to force GPU attempt.
if not defined JARVIS_WHISPER_DEVICE set JARVIS_WHISPER_DEVICE=cpu
REM Whisper: "small" is much more human-like/accurate for EN/HI than tiny (CPU: slower but usable). Override: set JARVIS_WHISPER_MODEL=tiny
if not defined JARVIS_WHISPER_MODEL set JARVIS_WHISPER_MODEL=small
if not defined JARVIS_DEBUG_AUDIO set JARVIS_DEBUG_AUDIO=1
REM Wake tuning: lower = easier wake in noisy rooms (e.g. 90). Override with set JARVIS_WAKE_MIN_RMS=90
if not defined JARVIS_WAKE_MIN_RMS set JARVIS_WAKE_MIN_RMS=90
REM USB PnP mic: prefer WDM-KS (id 48) when it carries signal; fallback 31 (WASAPI resampled). Override: set JARVIS_DEVICE=48/31/2/default
if not defined JARVIS_DEVICE set JARVIS_DEVICE=48
echo Starting Jarvis (WHISPER_DEVICE=%JARVIS_WHISPER_DEVICE%, MODEL=%JARVIS_WHISPER_MODEL%, DEVICE=%JARVIS_DEVICE%)...
if exist ".venv312\Scripts\python.exe" (
  ".venv312\Scripts\python.exe" jarvis.py
) else (
  python jarvis.py
)
pause
