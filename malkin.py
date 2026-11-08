import os
# Reduce noisy HF/Whisper warnings on Windows
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
import sys
import json
import time
import queue
import threading
import subprocess
import webbrowser
import shutil
import ctypes
import re
from pathlib import Path

import sounddevice as sd
import vosk
import pyttsx3
import pyautogui
import psutil
import requests
import screen_brightness_control as sbc

# Windows volume control
try:
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    PYCAW_AVAILABLE = True
except Exception:
    PYCAW_AVAILABLE = False

# Config
WAKE_WORD = "malkin"
WAKE_WORD_ALIASES = [
    # Primary
    "malkin", "malkin.", "malkin,", "malkin!",
    # English/accent phonetic variants (Vosk/Whisper may output these)
    "malkeen", "maalkeen", "maalkin", "maalkin", "malkin ji", "malkin ji",
    "mal kin", "maal kin", "mall kin", "mul kin", "mulkin", "mulkeen",
    "malikin", "malikan", "maalikan", "malkina", "malkina",
    "malken", "malkinay", "malkinay", "malkinn", "malkean", "maulkin",
    "malkyn", "malcin", "malquin", "malkein",
    # Hindi/Urdu pronunciation variants (romanized)
    "maal kin", "maal keen", "mal keen", "mul queen", "mal queen",
    "maalkin", "maalikin", "malkeena", "malkina ji",
    # Hindi/Desi accent approximations often heard by EN ASR
    "markin", "mar kin", "malkhin", "mal khin", "maalkhin",
    "milkin", "milk in", "malgin", "mal kin ji",
    # Hindi (Devanagari) - recognized when using Whisper/HI-capable paths
    "मालकिन", "मल्किन", "मालिकिन", "मालकिन जी", "मालकिन जी,", "माल्किन जी",
    "ऐ मालकिन", "हे मालकिन", "ओ मालकिन", "अरे मालकिन",
    "मालकीन", "मालक़िन",
    # Common ASR confusions to normalize toward wake intent (handled carefully in detection)
    "mall keen", "mall queen", "mal kine",
]
ASSISTANT_NAME = "Malkin"
# Common voice misrecognitions (ASR quirks) -> corrections applied before command parsing
ASR_CORRECTIONS = [
    # Malkin wake/name mishearings -> normalize
    (r'\b(mal\s*keen|maal\s*keen|ma+l\s*kin|mul\s*kin|mul\s*keen|mal\s*queen|mul\s*queen|mall\s*kin|mall\s*keen|markin|mar\s*kin|mal\s*khin|mal\s*gin|milkin|milk\s*in)\b', 'malkin'),
    (r'\b(malkin\s*ji|maal\s*kin\s*ji|mal\s*keen\s*ji)\b', 'malkin'),
    # YouTube variants
    (r'\b(your\s*tube|you\s*tube|your\s*cube|you\s*cube|u\s*tube|utube|you\s*too?b|yoo\s*tube|youtub|your\s*boob|you\s*boob)\b', 'youtube'),
    (r'\b(your\s*tube\s*music|you\s*tube\s*music|your\s*cube\s*music|youtube\s*musi[ck]|yt\s*musi[ck])\b', 'youtube music'),
    (r'\b(your\s*tube\s*studio|you\s*tube\s*studio|your\s*cube\s*studio|youtube\s*stud[yi]o|yt\s*studio)\b', 'youtube studio'),
    (r'\b(yt\s*music)\b', 'youtube music'),
    (r'\b(yt\s*studio)\b', 'youtube studio'),
    # App/command mishearings
    (r'\b(chromee?|krom|crome|go+gle\s*chrome)\b', 'chrome'),
    (r'\b(note\s*pad|not\s*paid)\b', 'notepad'),
    (r'\b(vs\s*code|visual\s*studio\s*code)\b', 'vscode'),
    (r'\b(file\s*explorer|explora)\b', 'explorer'),
]

def correct_asr_text(text):
    t = " " + (text or "").strip().lower() + " "
    for pat, repl in ASR_CORRECTIONS:
        t = re.sub(pat, repl, t, flags=re.IGNORECASE)
    t = re.sub(r'\s+', ' ', t).strip()
    # Second pass for combos created after first pass
    t = re.sub(r'\byoutube\s+musi[ck]\b', 'youtube music', t)
    t = re.sub(r'\byoutube\s+stud[yi]o\b', 'youtube studio', t)
    return t
LANGUAGE_MODE = "auto"  # "auto" (English+Hindi mixed via Whisper), "en", or "hi"
# Vosk fallback models (used if faster-whisper not available)
VOSK_MODELS = {
    "en": {"dir": "vosk-model-small-en-us-0.15", "url": "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"},
    "hi": {"dir": "vosk-model-small-hi-0.22", "url": "https://alphacephei.com/vosk/models/vosk-model-small-hi-0.22.zip"},
}
SAMPLE_RATE = 16000
DEVICE = None  # default mic
# Whisper settings for bilingual auto mode
WHISPER_MODEL_SIZE = "base"   # "tiny"/"base"/"small"/"medium" - base is faster on CPU
WHISPER_DEVICE = "cpu"        # force CPU for stability (set "cuda" to try GPU)
WHISPER_COMPUTE = "int8"      # CPU-friendly; use "int8_float16" for CUDA

# --- LLM "brain" (optional, for deeper reasoning/human-like replies) ---
# Set one of these environment variables to enable:
#   OPENAI_API_KEY   (uses OpenAI-compatible chat API)
#   GROQ_API_KEY     (uses Groq OpenAI-compatible API, fast)
# Optional overrides:
#   MALKIN_LLM_BASE_URL (e.g. http://localhost:11434/v1 for Ollama, or Groq/OpenAI base)
#   MALKIN_LLM_MODEL    (e.g. gpt-4o-mini, llama-3.1-8b-instant, qwen2.5:7b-instruct)
#   (legacy JARVIS_* names also supported)
LLM_ENABLED = True
LLM_TIMEOUT = 12
LLM_HISTORY_MAX = 8  # short conversational memory
LLM_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("GROQ_API_KEY") or os.getenv("MALKIN_LLM_API_KEY") or os.getenv("JARVIS_LLM_API_KEY") or ""
LLM_BASE_URL = os.getenv("MALKIN_LLM_BASE_URL") or os.getenv("JARVIS_LLM_BASE_URL") or (
    "https://api.groq.com/openai/v1" if os.getenv("GROQ_API_KEY") else "https://api.openai.com/v1"
)
LLM_MODEL = os.getenv("MALKIN_LLM_MODEL") or os.getenv("JARVIS_LLM_MODEL") or (
    "llama-3.1-8b-instant" if os.getenv("GROQ_API_KEY") else "gpt-4o-mini"
)
llm_history = []  # [{"role":"user/assistant","content":"..."}]

# TTS
tts_engine = pyttsx3.init()
tts_engine.setProperty('rate', 175)
try:
    tts_engine.setProperty('volume', 1.0)
except Exception:
    pass
voices = tts_engine.getProperty('voices') or []
# Prefer a female voice (pyttsx3/SAPI) when available
_tts_voice_id = None
for v in voices:
    nm = ((getattr(v, 'name', '') or '') + ' ' + (getattr(v, 'id', '') or '')).lower()
    if any(k in nm for k in ['zira', 'female', 'woman', 'heera', 'hazel', 'swara', 'neerja']):
        _tts_voice_id = v.id
        break
if not _tts_voice_id:
    # fallback heuristic
    for v in voices:
        nm = ((getattr(v, 'name', '') or '') + ' ' + (getattr(v, 'id', '') or '')).lower()
        if 'david' not in nm and 'male' not in nm:
            _tts_voice_id = v.id
            break
if _tts_voice_id:
    try:
        tts_engine.setProperty('voice', _tts_voice_id)
    except Exception:
        pass

# edge-tts for Hindi/English neural voices (better quality, female)
EDGE_TTS_AVAILABLE = False
try:
    import asyncio
    import edge_tts
    import tempfile
    EDGE_TTS_AVAILABLE = True
except Exception:
    EDGE_TTS_AVAILABLE = False

# faster-whisper for bilingual (auto) recognition
WHISPER_AVAILABLE = False
whisper_model = None
try:
    from faster_whisper import WhisperModel
    WHISPER_AVAILABLE = True
except Exception as e:
    print(f"[Malkin] faster-whisper not available ({e}); bilingual auto mode will use Vosk fallback.")

try:
    import numpy as np
except Exception:
    np = None

tts_lock = threading.Lock()

# --- Human-like behavior & context ---
import random

CONTEXT = {
    "last_app": None,
    "last_path": None,
    "last_action": None,
    "pending_confirm": None,  # {"action": callable, "prompt": str}
}

ACKS = {
    "en": ["On it.", "Sure.", "Got it.", "Alright.", "Doing that now.", "One sec."],
    "hi": ["Theek hai.", "Haan, kar raha hoon.", "Samajh gaya.", "Bilkul.", "Ek second."],
}
DONE_ACKS = {
    "en": ["Done.", "All set.", "There you go.", "Okay, done."],
    "hi": ["Ho gaya.", "Taiyaar hai.", "Kar diya."],
}
THINKING_ACKS = {
    "en": ["Let me check.", "One moment.", "Let me find that."],
    "hi": ["Ek minute dekh raha hoon.", "Main check karta hoon."],
}

def _pick(msgs, lang="en"):
    arr = msgs.get(lang) or msgs.get("en") or ["Okay."]
    return random.choice(arr)

def ack(lang="en"):
    speak(_pick(ACKS, lang), lang=lang)

def ack_thinking(lang="en"):
    speak(_pick(THINKING_ACKS, lang), lang=lang)

def set_context(**kwargs):
    for k, v in kwargs.items():
        CONTEXT[k] = v

def get_context(key):
    return CONTEXT.get(key)

def detect_response_lang(text):
    """Heuristic: if Devanagari chars present, treat as Hindi for TTS response."""
    if not text:
        return "en"
    for ch in text:
        if '\u0900' <= ch <= '\u097F':
            return "hi"
    return "en"

def _speak_edge_tts(text, lang="hi"):
    """Use edge-tts for Hindi/English. Saves to temp mp3 and plays."""
    voice = "hi-IN-SwaraNeural" if lang == "hi" else "en-US-AriaNeural"
    try:
        async def _run():
            communicate = edge_tts.Communicate(text, voice, rate="+12%")
            fd, path = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)
            await communicate.save(path)
            return path
        path = asyncio.run(_run())
        # guard against empty/invalid audio output
        try:
            if not os.path.exists(path) or os.path.getsize(path) < 1024:
                try: os.remove(path)
                except Exception: pass
                return False
        except Exception:
            return False
        try:
            ps_cmd = (
                "Add-Type -AssemblyName presentationCore; "
                f"$p = New-Object System.Windows.Media.MediaPlayer; "
                f"$p.Open([Uri]::new('{path.replace(os.sep, '/')}')); $p.Play(); "
                "Start-Sleep -Milliseconds 250; "
                "$t=0; while(-not $p.NaturalDuration.HasTimeSpan -and $t -lt 25){Start-Sleep -Milliseconds 80;$t++}; "
                "if($p.NaturalDuration.HasTimeSpan){$ms=[int][Math]::Min($p.NaturalDuration.TimeSpan.TotalMilliseconds, 5000); if($ms -gt 0){Start-Sleep -Milliseconds $ms}}; "
                "$p.Close()"
            )
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd],
                               capture_output=True, timeout=7)
            if r.returncode != 0:
                # fallback to local tts instead of startfile delays
                pass
        except Exception:
            pass
        try:
            os.remove(path)
        except Exception:
            pass
        return True
    except Exception as e:
        msg = str(e)
        if "No audio was received" in msg:
            return False
        print(f"edge-tts error: {e}")
        return False

def speak(text, lang=None, fast=False):
    if text is None:
        return
    text = str(text).strip()
    if not text or text in (".", ",", "!", "?", "..."):
        return
    if lang is None:
        lang = detect_response_lang(text) if LANGUAGE_MODE == "auto" else LANGUAGE_MODE
    print(f"[Malkin] {text}")
    with tts_lock:
        try:
            # Fast acknowledgements: local TTS avoids edge-tts network + PowerShell delay
            if fast:
                tts_engine.setProperty('rate', 210)
                tts_engine.say(text)
                tts_engine.runAndWait()
                tts_engine.setProperty('rate', 175)
                return
            if EDGE_TTS_AVAILABLE and lang in ("hi", "en"):
                if _speak_edge_tts(text, lang):
                    return
            tts_engine.setProperty('rate', 190)
            tts_engine.say(text)
            tts_engine.runAndWait()
            tts_engine.setProperty('rate', 175)
        except Exception as e:
            print(f"TTS error: {e}")

# Volume helpers
def set_volume(percent):
    if not PYCAW_AVAILABLE:
        return False
    try:
        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))
        volume.SetMasterVolumeLevelScalar(max(0, min(100, percent)) / 100.0, None)
        return True
    except Exception:
        return False

def get_volume():
    if not PYCAW_AVAILABLE:
        return None
    try:
        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))
        return int(volume.GetMasterVolumeLevelScalar() * 100)
    except Exception:
        return None

def change_volume(delta):
    cur = get_volume()
    if cur is None:
        return False
    return set_volume(cur + delta)

# System commands
def lock_pc():
    ctypes.windll.user32.LockWorkStation()

def shutdown_pc():
    subprocess.run(["shutdown", "/s", "/t", "5"], check=False)
    speak("Shutting down in 5 seconds")

def restart_pc():
    subprocess.run(["shutdown", "/r", "/t", "5"], check=False)
    speak("Restarting in 5 seconds")

def sleep_pc():
    ctypes.windll.powrprof.SetSuspendState(0, 1, 0)

# Media keys (works with most media players including Spotify)
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_VOLUME_MUTE = 0xAD

def press_media_key(vk):
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, 2, 0)

# App launching
APP_ALIASES = {
    "chrome": ["chrome", "google chrome"],
    "edge": ["edge", "microsoft edge"],
    "firefox": ["firefox"],
    "notepad": ["notepad"],
    "calculator": ["calculator", "calc"],
    "spotify": ["spotify"],
    "discord": ["discord"],
    "vscode": ["vscode", "vs code", "code", "visual studio code"],
    "explorer": ["explorer", "file explorer", "files", "this pc", "my computer"],
    "cmd": ["command prompt", "cmd", "terminal"],
    "powershell": ["powershell"],
    "task manager": ["task manager"],
    "settings": ["settings"],
    "control panel": ["control panel"],
    "paint": ["paint"],
    "word": ["word", "microsoft word"],
    "excel": ["excel", "microsoft excel"],
    "steam": ["steam"],
    "whatsapp": ["whatsapp"],
}

APP_COMMANDS = {
    "chrome": r"start chrome",
    "edge": r"start msedge",
    "firefox": r"start firefox",
    "notepad": "notepad",
    "calculator": "calc",
    "spotify": r"start spotify:",
    "discord": r"start discord:",
    "vscode": r"code",
    "explorer": "explorer",
    "cmd": "start cmd",
    "powershell": "start powershell",
    "task manager": "taskmgr",
    "settings": "start ms-settings:",
    "control panel": "control",
    "paint": "mspaint",
    "word": r"start winword",
    "excel": r"start excel",
    "steam": r"start steam:",
    "whatsapp": r"start whatsapp:",
}

_installed_apps_cache = None
_installed_apps_cache_time = 0

def _load_installed_apps():
    """Index installed apps from Start Apps and App Paths registry. Cached."""
    global _installed_apps_cache, _installed_apps_cache_time
    now = time.time()
    if _installed_apps_cache is not None and (now - _installed_apps_cache_time) < 300:
        return _installed_apps_cache
    apps = {}  # normalized name -> launch info
    # 1) Get-StartApps (UWP + shortcuts)
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-StartApps | ForEach-Object { $_.Name + '||' + $_.AppID }"],
            capture_output=True, text=True, timeout=15
        )
        for line in (out.stdout or "").splitlines():
            if "||" in line:
                name, appid = line.split("||", 1)
                name = name.strip(); appid = appid.strip()
                if name and appid:
                    apps[name.lower()] = {"type": "appid", "value": appid, "display": name}
    except Exception:
        pass
    # 2) App Paths registry
    try:
        import winreg
        for hive, path in [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
                           (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths")]:
            try:
                with winreg.OpenKey(hive, path) as k:
                    i = 0
                    while True:
                        try:
                            sub = winreg.EnumKey(k, i); i += 1
                            exe_name = sub.replace(".exe", "").lower()
                            try:
                                with winreg.OpenKey(k, sub) as sk:
                                    val, _ = winreg.QueryValueEx(sk, "")
                                    if val:
                                        apps.setdefault(exe_name, {"type": "path", "value": val.strip('"'), "display": exe_name})
                            except Exception:
                                pass
                        except OSError:
                            break
            except Exception:
                pass
    except Exception:
        pass
    _installed_apps_cache = apps
    _installed_apps_cache_time = now
    return apps

def _best_installed_app_match(target):
    apps = _load_installed_apps()
    t = target.lower().strip()
    if not t:
        return None
    # exact
    if t in apps:
        return apps[t]
    # contains / token overlap scoring
    best = None; best_score = 0
    t_tokens = set(re.findall(r'[a-z0-9]+', t))
    for name, info in apps.items():
        score = 0
        if t == name: score = 100
        elif name.startswith(t) or t.startswith(name): score = 80
        elif t in name: score = 60
        elif name in t: score = 55
        else:
            n_tokens = set(re.findall(r'[a-z0-9]+', name))
            overlap = len(t_tokens & n_tokens)
            if overlap: score = 40 + overlap * 5
        if score > best_score:
            best_score = score; best = info
    return best if best_score >= 50 else None

def launch_installed_app(target):
    info = _best_installed_app_match(target)
    if not info:
        return False, None
    try:
        if info["type"] == "appid":
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{info['value']}"])
        else:
            subprocess.Popen([info["value"]], shell=True)
        return True, info.get("display", target)
    except Exception:
        return False, info.get("display", target)

def resolve_drive_path(text):
    """Resolve phrases like 'i drive', 'open c drive', 'e drive folder' to a path like I:\\""" 
    t = text.lower()
    # patterns: "i drive", "drive i", "i:" , "i disk"
    m = re.search(r'\b([a-z])\s*(?:drive|disk)\b', t)
    if not m:
        m = re.search(r'\bdrive\s*([a-z])\b', t)
    if not m:
        m = re.search(r'\b([a-z])\s*:\b', t)
    if m:
        letter = m.group(1).upper()
        path = f"{letter}:\\"
        if os.path.exists(path):
            set_last_location(path)
            return path, letter
    return None, None

def find_app_key(text):
    text = text.lower()
    # Don't let generic "explorer/file explorer" win if a specific drive is mentioned
    if resolve_drive_path(text)[0]:
        return None
    for key, aliases in APP_ALIASES.items():
        for a in aliases:
            if a in text:
                return key
    return None

def open_app(app_key):
    cmd = APP_COMMANDS.get(app_key)
    if not cmd:
        return False
    try:
        subprocess.Popen(cmd, shell=True)
        return True
    except Exception:
        return False

def close_app(app_key):
    # try common process names
    proc_names = {
        "chrome": ["chrome.exe"],
        "edge": ["msedge.exe"],
        "firefox": ["firefox.exe"],
        "notepad": ["notepad.exe"],
        "calculator": ["CalculatorApp.exe", "Calculator.exe", "calculator.exe"],
        "spotify": ["Spotify.exe"],
        "discord": ["Discord.exe"],
        "vscode": ["Code.exe"],
        "explorer": ["explorer.exe"],
        "steam": ["steam.exe"],
        "whatsapp": ["WhatsApp.exe", "WhatsApp.Root.exe"],
        "cmd": ["cmd.exe", "WindowsTerminal.exe"],
        "powershell": ["powershell.exe", "pwsh.exe", "WindowsTerminal.exe"],
        "task manager": ["Taskmgr.exe"],
        "settings": ["SystemSettings.exe"],
        "paint": ["mspaint.exe"],
        "word": ["WINWORD.EXE"],
        "excel": ["EXCEL.EXE"],
    }
    names = proc_names.get(app_key, [f"{app_key}.exe"])
    killed = False
    for name in names:
        try:
            r = subprocess.run(["taskkill", "/F", "/IM", name], capture_output=True, text=True)
            out = (r.stdout or "") + (r.stderr or "")
            if r.returncode == 0 or "SUCCESS" in out.upper():
                killed = True
        except Exception:
            pass
    return killed

def _kill_processes_by_name_substring(target):
    """Try to close running processes whose name/path/title matches target."""
    t = target.lower().strip()
    if not t:
        return 0
    # build candidate process names from installed app match
    candidates = set()
    info = _best_installed_app_match(t)
    if info:
        disp = (info.get("display") or "").lower()
        val = (info.get("value") or "")
        # from display
        for token in re.findall(r'[a-z0-9]+', disp):
            if len(token) >= 4:
                candidates.add(token)
        # from path/appid
        base = os.path.basename(val).replace(".exe", "").lower()
        if base:
            candidates.add(base)
    # from target tokens
    for token in re.findall(r'[a-z0-9]+', t):
        if len(token) >= 4:
            candidates.add(token)
    # common fixes
    synonyms = {
        "vs code": ["code"], "vscode": ["code"], "visual studio code": ["code"],
        "google chrome": ["chrome"], "microsoft edge": ["msedge"], "file explorer": ["explorer"],
    }
    for k, vals in synonyms.items():
        if k in t:
            candidates.update(vals)
    if not candidates:
        candidates.add(t.replace(" ", ""))

    killed_pids = set()
    for proc in psutil.process_iter(['pid', 'name', 'exe', 'cmdline']):
        try:
            name = (proc.info.get('name') or '').lower()
            exe = (proc.info.get('exe') or '').lower()
            base = name.replace('.exe','')
            hay = f"{name} {exe}"
            match = False
            for c in candidates:
                c = c.lower()
                if not c or c in ("open", "close", "start", "drive", "folder"):
                    continue
                if c == base or c in base or base in c or c in hay:
                    match = True; break
            if match:
                # be careful not to kill critical/system shell processes indiscriminately unless explicitly asked
                if base in ("explorer",) and "explorer" not in t and "file explorer" not in t:
                    continue
                proc.kill()
                killed_pids.add(proc.info['pid'])
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
        except Exception:
            pass
    return len(killed_pids)

# File operations
_last_open_location = None  # tracks last opened folder/drive for "go to ..." commands

def _get_desktop_path():
    try:
        # Works for OneDrive-redirected desktops too
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
            capture_output=True, text=True, timeout=5
        )
        p = (ps.stdout or "").strip()
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    for cand in [os.path.join(Path.home(), "OneDrive", "Desktop"), os.path.join(Path.home(), "Desktop")]:
        if os.path.exists(cand):
            return cand
    return os.path.join(Path.home(), "Desktop")

def open_path(path_str):
    global _last_open_location
    try:
        p = Path(os.path.expandvars(os.path.expanduser(path_str)))
        if p.exists():
            os.startfile(str(p))
            try:
                _last_open_location = str(p if p.is_dir() else p.parent)
            except Exception:
                pass
            return True
    except Exception:
        pass
    return False

def set_last_location(path_str):
    global _last_open_location
    try:
        if path_str and os.path.exists(path_str):
            _last_open_location = str(path_str)
    except Exception:
        pass

def get_last_location():
    return _last_open_location

DESKTOP_PATH = _get_desktop_path()

# Common folder name aliases (used with "go to ...")
FOLDER_ALIASES = {
    "program files": [r"C:\Program Files"],
    "program files x86": [r"C:\Program Files (x86)", r"C:\Program Files x86"],
    "program files 86": [r"C:\Program Files (x86)"],
    "users": [r"C:\Users"],
    "user": [r"C:\Users"],
    "windows": [r"C:\Windows"],
    "system32": [r"C:\Windows\System32"],
    "system 32": [r"C:\Windows\System32"],
    "downloads": [str(Path.home() / "Downloads")],
    "download": [str(Path.home() / "Downloads")],
    "documents": [str(Path.home() / "Documents")],
    "document": [str(Path.home() / "Documents")],
    "desktop": [DESKTOP_PATH],
    "pictures": [str(Path.home() / "Pictures")],
    "picture": [str(Path.home() / "Pictures")],
    "photos": [str(Path.home() / "Pictures")],
    "videos": [str(Path.home() / "Videos")],
    "video": [str(Path.home() / "Videos")],
    "music": [str(Path.home() / "Music")],
    "appdata": [os.environ.get("APPDATA", ""), os.path.join(Path.home(), "AppData")],
    "temp": [os.environ.get("TEMP", ""), r"C:\Windows\Temp"],
}

def _desktop_items_index():
    items = []
    base = DESKTOP_PATH
    if not base or not os.path.exists(base):
        return items
    try:
        for name in os.listdir(base):
            full = os.path.join(base, name)
            disp = name
            lower = name.lower()
            stem = os.path.splitext(name)[0].lower()
            items.append({"name": name, "stem": stem, "lower": lower, "path": full, "is_dir": os.path.isdir(full)})
    except Exception:
        pass
    return items

def find_on_desktop(query):
    """Find a file/folder on Desktop by fuzzy name match."""
    q = (query or "").lower().strip()
    if not q:
        return None
    q = re.sub(r'\b(on|from|in|my|the|file|folder|shortcut)\b', ' ', q)
    q = re.sub(r'\s+', ' ', q).strip()
    if not q:
        return None
    items = _desktop_items_index()
    # exact stem/name
    for it in items:
        if q == it["stem"] or q == it["lower"]:
            return it
    # contains scoring
    best, best_score = None, 0
    q_tokens = set(re.findall(r'[a-z0-9]+', q))
    for it in items:
        score = 0
        s = it["stem"]
        if q == s: score = 100
        elif s.startswith(q) or q.startswith(s): score = 85
        elif q in s or s in q: score = 70
        else:
            tks = set(re.findall(r'[a-z0-9]+', s))
            ov = len(q_tokens & tks)
            if ov: score = 45 + ov * 8
        if it["is_dir"] and score > 0:
            score += 2  # slight bias when ambiguous and command says folder
        if score > best_score:
            best_score = score; best = it
    return best if best_score >= 55 else None

def list_desktop_items(limit=12):
    items = _desktop_items_index()
    names = []
    for it in items[:limit]:
        names.append(it["name"] + ("/" if it["is_dir"] else ""))
    return names, len(items)

def resolve_go_to_path(command_text):
    """Resolve 'go to ...' targets. Uses last opened location as base when available."""
    t = command_text.lower().strip()
    # strip trigger phrases
    t = re.sub(r'^(go to|navigate to|take me to|open folder|go inside|go in)\s+', '', t)
    t = re.sub(r'\b(mein|me|par|pe|folder|directory|kholo|khol do|jao|chalo)\b', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip(' .')
    if not t:
        return None, None

    base = get_last_location()

    # If user says "go to c drive program files" or includes a drive
    drive_path, drive_letter = resolve_drive_path(t)
    # If a drive is mentioned, prefer that as base (or direct drive open)
    if drive_path and not any(x in t for x in ["program", "users", "windows", "folder", "file", " in "]):
        # ambiguous like "go to c drive" -> open drive
        if len(t.replace("drive","").replace(drive_letter.lower(),"").strip()) <= 2:
            return drive_path, f"{drive_letter} drive"

    # Try absolute/explicit path in text
    m = re.search(r'([a-zA-Z]:[\\/][^\n\r]+)', command_text)
    if m:
        cand = m.group(1).strip().strip('"').strip("'")
        if os.path.exists(cand):
            return cand, os.path.basename(cand) or cand

    # Known aliases (check longer keys first)
    for name in sorted(FOLDER_ALIASES.keys(), key=len, reverse=True):
        if name in t:
            # if drive specified and alias is relative-ish under root (e.g. program files on C)
            if drive_path and name in ("program files", "program files x86", "program files 86", "users", "windows", "system32", "system 32"):
                cand = os.path.join(drive_path, FOLDER_ALIASES[name][0].split(":\\",1)[-1]) if ":\\" in FOLDER_ALIASES[name][0] else FOLDER_ALIASES[name][0]
                if os.path.exists(cand):
                    return cand, name
            for cand in FOLDER_ALIASES[name]:
                if cand and os.path.exists(cand):
                    # if a specific drive was mentioned in command and this is a system path, prefer that drive
                    if drive_path and cand.lower().startswith("c:\\"):
                        alt = os.path.join(drive_path, cand[3:])
                        if os.path.exists(alt):
                            return alt, name
                    return cand, name

    # If user says "go to X in Y drive" / "go to program files in c drive"
    # try build from drive base + remainder
    if drive_path:
        remainder = t
        remainder = re.sub(r'\b([a-z])\s*(?:drive|disk)\b', ' ', remainder)
        remainder = re.sub(r'\bdrive\s*([a-z])\b', ' ', remainder)
        remainder = re.sub(r'\b(in|on|inside|under|mein|me)\b', ' ', remainder)
        remainder = re.sub(r'\s+', ' ', remainder).strip()
        if remainder:
            # try aliases again against remainder
            for name in sorted(FOLDER_ALIASES.keys(), key=len, reverse=True):
                if name in remainder:
                    for cand in FOLDER_ALIASES[name]:
                        if cand and cand.lower().startswith("c:\\"):
                            alt = os.path.join(drive_path, cand[3:])
                            if os.path.exists(alt):
                                return alt, name
            # try direct folder under drive
            cand = os.path.join(drive_path, remainder)
            if os.path.exists(cand):
                return cand, remainder
            # try case-insensitive match under drive root
            try:
                for entry in os.listdir(drive_path):
                    if entry.lower() == remainder.lower() or remainder.lower() in entry.lower():
                        cand2 = os.path.join(drive_path, entry)
                        if os.path.isdir(cand2):
                            return cand2, entry
            except Exception:
                pass

    # Try relative to last opened location
    if base and os.path.isdir(base):
        cand = os.path.join(base, t)
        if os.path.exists(cand):
            return cand, os.path.basename(cand)
        try:
            for entry in os.listdir(base):
                el = entry.lower()
                if el == t or t in el or el in t:
                    cand2 = os.path.join(base, entry)
                    if os.path.exists(cand2):
                        return cand2, entry
        except Exception:
            pass

    # Try relative to home
    cand = os.path.join(str(Path.home()), t)
    if os.path.exists(cand):
        return cand, os.path.basename(cand)

    return None, t

def search_files(query, root=None):
    if root is None:
        root = str(Path.home())
    results = []
    query_lower = query.lower()
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            # skip some heavy dirs
            dirnames[:] = [d for d in dirnames if d.lower() not in ('appdata', 'node_modules', '.git', '__pycache__', 'windows')]
            for name in filenames:
                if query_lower in name.lower():
                    results.append(os.path.join(dirpath, name))
                    if len(results) >= 10:
                        return results
            if len(results) >= 10:
                break
    except Exception:
        pass
    return results

def detect_wake_word(text):
    """Detect wake word/variants in ASR text (handles Hindi/English accents and common mishearings).
    Tuned to avoid false positives from background noise/silence."""
    if not text:
        return False, ""
    raw = text.strip()
    if len(raw) < 3:
        return False, ""
    tl = raw.lower()
    norm = correct_asr_text(raw)

    # Fast path: explicit aliases/Devanagari tokens (isolated)
    for alias in WAKE_WORD_ALIASES:
        try:
            a = alias.lower().strip()
        except Exception:
            a = str(alias).strip()
        if not a or len(a) < 3:
            continue
        if a in tl:
            idx = tl.find(a)
            before_ok = (idx == 0) or (not tl[idx-1].isalnum())
            end_i = idx + len(a)
            after_ok = (end_i >= len(tl)) or (not tl[end_i].isalnum())
            if before_ok and after_ok:
                after = raw[end_i:].strip(" ,.!?:;-") if end_i <= len(raw) else ""
                return True, after

    # ASR-normalized explicit malkin token
    if re.search(r'(^|\s)malkin(\s|$|[,.!?:;])', f" {norm} "):
        after = re.sub(r'^.*?\bmalkin\b\s*', '', norm, count=1, flags=re.I).strip(" ,.!?:;-")
        if after == norm:
            after = ""
        if len(norm.split()) <= 8 or after or norm.strip() == "malkin":
            return True, after

    # Strict fuzzy phonetic check (Hindi accents), only when short/near start
    joined = norm.lower()
    m = re.search(r'\b(m+a+l+k+(ee+n|i+n)|m+u+l+k+(ee+n|i+n)|m+a+l+\s*k+(ee+n|i+n|een)|m+u+l+\s*k+(ee+n|i+n|een)|m+a+l+\s*q+u+ee+n|m+u+l+\s*q+u+ee+n|m+a+l+i+k+i+n)\b', joined)
    if m and (m.start() <= 12 or len(joined.split()) <= 6):
        after = joined[m.end():].strip(" ,.!?:;-")
        return True, after

    # Very short utterance near-exact only
    compact = re.sub(r'[^a-z]', '', tl)
    if 5 <= len(compact) <= 8:
        for cand in ("malkin", "malkeen", "maalkeen", "mulkin", "mulkeen", "maalkin"):
            if compact == cand:
                return True, ""
            if abs(len(compact)-len(cand)) <= 1:
                mism = sum(1 for a,b in zip(compact,cand) if a!=b) + abs(len(compact)-len(cand))
                if mism <= 2 and compact[0] == cand[0]:
                    return True, ""
    return False, ""

def llm_reason_and_reply(user_text, response_lang="en"):
    """Optional LLM brain for deeper reasoning / human-like conversation.
    Returns dict: {"action":"answer|command|clarify","reply":"...","intent":"...","target":"..."} or None.
    Safety: never executes system actions directly; caller decides.
    """
    global llm_history
    if not LLM_ENABLED or not LLM_API_KEY or not user_text:
        return None
    try:
        ctx = {
            "last_app": CONTEXT.get("last_app"),
            "last_path": CONTEXT.get("last_path"),
            "last_action": CONTEXT.get("last_action"),
            "desktop_path": DESKTOP_PATH,
            "language_mode": LANGUAGE_MODE,
            "os": "windows",
        }
        system = (
            "You are Malkin (मालकिन), a Windows PC voice assistant. Be concise, helpful, and human-like. "
            "Reply in the user's language (English/Hindi/mix) based on the user message. "
            "You can reason, but you do NOT directly control the PC. "
            "Return ONLY valid JSON with keys: "
            "action ('answer'|'command'|'clarify'), reply (string), intent (string), target (string). "
            "Use action='command' only for common PC intents you are confident about: "
            "open_app, open_website, open_path, go_to, close_app, search, volume_up, volume_down, mute, "
            "brightness_up, brightness_down, play_pause, next_track, prev_track, lock, screenshot, open_desktop. "
            "For dangerous actions (shutdown/restart/delete/system changes) use action='clarify' and ask confirmation. "
            "If you don't know, use action='clarify' with a helpful question. Keep reply short (1-2 sentences)."
        )
        messages = [{"role": "system", "content": system},
                    {"role": "system", "content": f"Context: {json.dumps(ctx, ensure_ascii=False)}"}]
        messages.extend(llm_history[-LLM_HISTORY_MAX:])
        messages.append({"role": "user", "content": user_text})
        payload = {
            "model": LLM_MODEL,
            "messages": messages,
            "temperature": 0.4,
            "max_tokens": 220,
        }
        headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
        # Use /chat/completions (OpenAI-compatible)
        base = LLM_BASE_URL.rstrip("/")
        url = base + "/chat/completions"
        r = requests.post(url, headers=headers, json=payload, timeout=LLM_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        # Try to parse JSON (allow fenced)
        m = re.search(r'\{.*\}', content, re.S)
        if not m:
            return {"action": "answer", "reply": content[:300], "intent": "chat", "target": ""}
        out = json.loads(m.group(0))
        if not isinstance(out, dict):
            return None
        out.setdefault("action", "answer")
        out.setdefault("reply", "")
        out.setdefault("intent", "")
        out.setdefault("target", "")
        # update short memory
        llm_history.append({"role": "user", "content": user_text})
        if out.get("reply"):
            llm_history.append({"role": "assistant", "content": str(out["reply"])[:500]})
        llm_history[:] = llm_history[-(LLM_HISTORY_MAX * 2):]
        return out
    except Exception as e:
        print(f"[LLM] {e}")
        return None


def _chrome_path():
    for p in [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]:
        if os.path.exists(p):
            return p
    return None


def open_in_chrome(url, response_lang="en"):
    """Open a URL in Google Chrome (uses Chrome's current/default profile)."""
    chrome = _chrome_path()
    # Try known Chrome path
    if chrome:
        try:
            subprocess.Popen([chrome, url])
            set_context(last_app="chrome", last_action="open_url", last_path=url)
            return True
        except Exception:
            pass
    # Try Start Menu / App Paths
    ok, _ = launch_installed_app("google chrome")
    if ok:
        time.sleep(0.5)
        try:
            subprocess.Popen(f'start "" chrome "{url}"', shell=True)
            set_context(last_app="chrome", last_action="open_url", last_path=url)
            return True
        except Exception:
            pass
    # Last resort: try chrome via shell
    try:
        subprocess.Popen(f'start "" chrome "{url}"', shell=True)
        set_context(last_app="chrome", last_action="open_url", last_path=url)
        return True
    except Exception:
        return False


def play_music_on_youtube(query=None, response_lang="en"):
    """Open YouTube Music (or YouTube search) in Chrome and try to autoplay."""
    try:
        if query:
            url = f"https://music.youtube.com/search?q={requests.utils.quote(query)}"
            spoken = query
        else:
            url = "https://music.youtube.com/"
            spoken = None
        launched = open_in_chrome(url, response_lang=response_lang)

        if not launched:
            speak(_pick({"en": "I couldn't open Chrome for YouTube Music.",
                         "hi": "Main YouTube Music ke liye Chrome nahi khol paya."}, response_lang), lang=response_lang)
            return False

        set_context(last_app="chrome", last_action="play_music", last_path=url)
        if spoken:
            speak(_pick({"en": f"Playing {spoken} on YouTube Music.",
                         "hi": f"YouTube Music par {spoken} chala raha hoon."}, response_lang), lang=response_lang)
        else:
            speak(_pick({"en": "Playing music on YouTube Music.",
                         "hi": "YouTube Music par gaana chala raha hoon."}, response_lang), lang=response_lang)

        # Best-effort autoplay: wait for page load then send 'k' (YouTube play/pause) or space
        def _try_autoplay():
            try:
                time.sleep(4.5)
                pyautogui.press('k')   # YouTube/YT Music play/pause toggle
                time.sleep(0.5)
                pyautogui.press('space')
            except Exception:
                pass
        threading.Thread(target=_try_autoplay, daemon=True).start()
        return True
    except Exception as e:
        print(f"[Music] {e}")
        speak(_pick({"en": "I couldn't start music right now.",
                     "hi": "Main abhi gaana start nahi kar paya."}, response_lang), lang=response_lang)
        return False


# Command parsing and execution
def execute_command(text):
    t = correct_asr_text(text)
    tl = t.lower().strip()
    # remove wake word aliases if still present
    for alias in WAKE_WORD_ALIASES:
        tl = tl.replace(alias, " ").strip()
    tl = re.sub(r'\s+', ' ', tl)
    t = tl
    if not t or len(t) < 2:
        return

    print(f"[Command] {t}")

    # Hindi command aliases -> normalize to English keywords where helpful
    hindi_map = [
        (r'\b(chrome|krom)\s*(kholo|khol do|chalu karo|chalu|khole|khol)\b', 'open chrome'),
        (r'\b(edge|edg)\s*(kholo|khol do|chalu karo)\b', 'open edge'),
        (r'\b(notepad|notpaid)\s*(kholo|khol do)\b', 'open notepad'),
        (r'\b(calculator|kalkuletar)\s*(kholo|khol do)\b', 'open calculator'),
        (r'\b(spotify|spotifai)\s*(kholo|khol do|chalu karo)\b', 'open spotify'),
        (r'\b(file explorer|explorer|files)\s*(kholo|khol do)\b', 'open explorer'),
        (r'\b(settings|setting)\s*(kholo|khol do)\b', 'open settings'),
        (r'\b(volume|aawaz)\s*(badhao|bada do|zyada karo|upar)\b', 'volume up'),
        (r'\b(volume|aawaz)\s*(kam karo|kam|ghatao|neeche)\b', 'volume down'),
        (r'\b(mute|chup|awaaz band)\b', 'mute'),
        (r'\b(brightness|roshni)\s*(badhao|zyada|upar)\b', 'brightness up'),
        (r'\b(brightness|roshni)\s*(kam|neeche)\b', 'brightness down'),
        (r'\b(gaana|gana|music)\s*(chalao|play karo|bajao)\b', 'play music'),
        (r'\b(gaana|gana|music)\s*(roko|band karo|pause)\b', 'pause'),
        (r'\b(agla|next)\s*(gaana|gana|song|track)\b', 'next track'),
        (r'\b(pichla|previous)\s*(gaana|gana|song|track)\b', 'previous track'),
        (r'\b(pc|computer|laptop)\s*(lock|band)\s*(karo|kar do)\b', 'lock pc'),
        (r'\b(band|shutdown|shut down)\s*(karo|kar do|ho jao)\b', 'shutdown'),
        (r'\b(restart|dobara chalu)\s*(karo|kar do)\b', 'restart'),
        (r'\b(screenshot|screen shot|tasveer)\s*(lo|le lo|kheench)\b', 'screenshot'),
        (r'\b(downloads|download)\s*(kholo|khol do)\b', 'open downloads'),
        (r'\b(documents|document)\s*(kholo|khol do)\b', 'open documents'),
        (r'\b(desktop)\s*(kholo|khol do)\b', 'open desktop'),
        (r'\b(time|samay|waqt)\s*(kya|batao|bata)\b', 'what time'),
        (r'\b(date|tareekh|tarikh|aaj)\s*(kya|batao|bata)\b', 'what date'),
        (r'\b(google|search)\s+(pe|par)?\s*(.+)\s*(dhundho|search karo|khojo)\b', r'search \3'),
    ]
    for pat, repl in hindi_map:
        new_t = re.sub(pat, repl, t)
        if new_t != t:
            t = new_t.strip()
            print(f"[Normalized] {t}")
            break

    # --- Human-like reasoning / quick intents ---
    response_lang = detect_response_lang(t)

    # Direct YouTube / YouTube Music / YouTube Studio commands (always launch in Chrome)
    if "youtube studio" in t or "yt studio" in t or "studio.youtube" in t:
        open_in_chrome("https://studio.youtube.com/", response_lang=response_lang)
        speak(_pick({"en": "Opening YouTube Studio in Chrome.",
                     "hi": "Chrome mein YouTube Studio khol raha hoon."}, response_lang), lang=response_lang)
        return

    # "play/search on youtube" (video/site) — use Chrome
    if "on youtube" in t and "youtube music" not in t and "yt music" not in t:
        q = t
        for kw in ["open", "play", "search", "on youtube", "youtube", "kholo", "chalao", "dhundho", "search karo", "please"]:
            q = q.replace(kw, " ")
        q = re.sub(r'\s+', ' ', q).strip(" ,.-")
        url = f"https://www.youtube.com/results?search_query={requests.utils.quote(q)}" if q else "https://www.youtube.com/"
        open_in_chrome(url, response_lang=response_lang)
        speak(_pick({"en": f"Opening YouTube in Chrome{(' for ' + q) if q else ''}.",
                     "hi": f"Chrome mein YouTube khol raha hoon{(' ' + q + ' ke liye') if q else ''}."}, response_lang), lang=response_lang)
        return

    # Music commands (YouTube / YouTube Music) - high priority, always Chrome
    _music_cmd = any(x in t for x in ["play music", "play song", "music chalao", "gaana chalao", "gana chalao", "bajao", "kuch gaana", "koi gaana"])
    _yt_music = ("youtube music" in t) or ("yt music" in t) or ("on youtube music" in t) or ("music.youtube" in t)
    if _yt_music or (_music_cmd and t.strip() in ("play music", "play some music", "gaana chalao", "gana chalao", "music chalao", "kuch gaana chalao", "koi gaana chalao")) or ("play" in t and "youtube music" in t):
        q = t
        for kw in ["malkin", "maalkeen", "malkeen", "मालकिन", "play", "on youtube music", "on youtube", "youtube music", "yt music", "music chalao", "gaana chalao", "gana chalao", "kuch", "koi", "gaana", "gana", "song", "music", "chalao", "bajao", "please", "mujhe", "mere liye"]:
            q = q.replace(kw, " ")
        q = re.sub(r'\s+', ' ', q).strip(" ,.-")
        play_music_on_youtube(q if q else None, response_lang=response_lang)
        return

    # Open YouTube / YouTube Music by name (always Chrome) - catch any phrasing
    if "youtube music" in t or "yt music" in t or "music.youtube" in t:
        q = t
        for kw in ["open", "launch", "start", "kholo", "khol do", "play", "on", "youtube music", "yt music", "please"]:
            q = q.replace(kw, " ")
        q = re.sub(r'\s+', ' ', q).strip(" ,.-")
        play_music_on_youtube(q if q else None, response_lang=response_lang)
        return
    if ("youtube" in t and "studio" not in t) and ("open" in t or "launch" in t or "start" in t or "kholo" in t or "khol" in t or t.strip() in ("youtube", "yt")):
        open_in_chrome("https://www.youtube.com/", response_lang=response_lang)
        speak(_pick({"en": "Opening YouTube in Chrome.",
                     "hi": "Chrome mein YouTube khol raha hoon."}, response_lang), lang=response_lang)
        return

    # Handle pending confirmations (e.g., shutdown/restart)
    if CONTEXT.get("pending_confirm"):
        if any(x in t for x in ["yes", "haan", "ha", "confirm", "do it", "kar do", "sure"]):
            action = CONTEXT["pending_confirm"].get("action")
            CONTEXT["pending_confirm"] = None
            speak("Okay, confirmed.", lang=response_lang)
            try:
                if callable(action):
                    action()
            except Exception:
                speak("I couldn't complete that action.", lang=response_lang)
            return
        if any(x in t for x in ["no", "nahi", "cancel", "mat karo", "stop", "rehne do"]):
            CONTEXT["pending_confirm"] = None
            speak("Cancelled.", lang=response_lang)
            return

    # Conversational/small talk
    if any(x in t for x in ["hello", "hi ", "hey ", "namaste", "namaskar", "what's up", "whats up", "kaise ho"]):
        speak(_pick({
            "en": ["Hey. What do you need?", "Hello. I'm listening.", "Hi. Tell me what to do."],
            "hi": ["Namaste. Batao kya karna hai?", "Haan ji, main sun raha hoon.", "Hello. Kya madad chahiye?"],
        }, response_lang), lang=response_lang)
        return
    if "how are you" in t or "kaisa hai" in t or "kaisi ho" in t:
        speak(_pick({
            "en": ["I'm running fine. Ready when you are.", "All good here. What should I do?"],
            "hi": ["Main theek hoon. Batao kya karna hai.", "Sab badhiya. Aap bolo."],
        }, response_lang), lang=response_lang)
        return
    if any(x in t for x in ["thank you", "thanks", "shukriya", "dhanyavaad"]):
        speak(_pick({
            "en": ["Anytime.", "You're welcome.", "No problem."],
            "hi": ["Koi baat nahi.", "Shukriya, aur batao.", "Hamesha."],
        }, response_lang), lang=response_lang)
        return
    if any(x in t for x in ["who are you", "tum kaun ho", "aap kaun ho"]):
        speak(f"I'm {ASSISTANT_NAME}, your PC assistant. I can open apps, control your system, manage files, and help with quick tasks." if response_lang=="en"
              else f"Main {ASSISTANT_NAME} hoon, aapki PC assistant. Main apps, system controls, files aur quick tasks mein madad kar sakti hoon.", lang=response_lang)
        return
    if any(x in t for x in ["what can you do", "kya kar sakte ho", "kya kar sakti ho", "help"]):
        speak("You can ask me to open apps or websites, open drives and folders, control volume/brightness/media, take screenshots, find files, and more. Try saying open I drive or go to Program Files." if response_lang=="en"
              else "Aap mujhse apps ya websites kholne, drives/folders open karne, volume/brightness/media control, screenshot, file search aur bohot kuch karwa sakte ho. Jaise bolo I drive kholo ya Program Files mein jao.", lang=response_lang)
        return
    if any(x in t for x in ["stop listening", "go to sleep", "sleep mode", "exit", "quit malkin", "shutdown malkin", "so jao malkin", "malkin so jao", "chup raho"]):
        speak("Going offline. Say my name when you need me." if response_lang=="en" else "Main offline ja raha hoon. Jab zarurat ho mera naam lena.", lang=response_lang)
        return "sleep"

    # Time / date
    if "time" in t and ("what" in t or "kya" in t or "bata" in t):
        from datetime import datetime
        now = datetime.now().strftime('%I:%M %p')
        speak(f"It's {now}" if response_lang=="en" else f"Abhi {now} baj rahe hain", lang=response_lang)
        return
    if "date" in t and ("what" in t or "today" in t or "kya" in t or "aaj" in t):
        from datetime import datetime
        today = datetime.now().strftime('%A, %B %d, %Y')
        speak(f"Today is {today}" if response_lang=="en" else f"Aaj {today} hai", lang=response_lang)
        return

    # Quick reasoning based on context
    if any(x in t for x in ["close it", "band karo ise", "ise band karo", "close that", "band kar do ise"]):
        if CONTEXT.get("last_app"):
            target = CONTEXT["last_app"]
            ack(response_lang)
            if close_app(target) or _kill_processes_by_name_substring(target) > 0:
                speak(f"Closed {target}", lang=response_lang)
            else:
                speak(f"I couldn't find {target} running", lang=response_lang)
            return
    if any(x in t for x in ["go back", "peeche jao", "back jao"]):
        # best-effort: send Alt+Left
        try:
            pyautogui.hotkey('alt', 'left')
            speak("Going back", lang=response_lang)
        except Exception:
            speak("I couldn't go back", lang=response_lang)
        return

    # System control (with confirmation for destructive actions)
    if "lock" in t and ("pc" in t or "computer" in t or "screen" in t or "laptop" in t):
        speak("Locking your computer", lang=response_lang)
        lock_pc()
        return
    if "shutdown" in t or "shut down" in t:
        CONTEXT["pending_confirm"] = {"action": shutdown_pc, "prompt": "shutdown"}
        speak("Do you want me to shut down the PC now? Say yes to confirm or no to cancel.", lang=response_lang)
        return
    if "restart" in t or "reboot" in t:
        CONTEXT["pending_confirm"] = {"action": restart_pc, "prompt": "restart"}
        speak("Do you want me to restart the PC now? Say yes to confirm or no to cancel.", lang=response_lang)
        return
    if "sleep" in t and ("pc" in t or "computer" in t or "laptop" in t):
        speak("Putting computer to sleep", lang=response_lang)
        sleep_pc()
        return

    # Volume
    m = re.search(r'(?:set\s+)?volume\s+(?:to\s+)?(\d+)', t)
    if m:
        level = int(m.group(1))
        if set_volume(level):
            speak(f"Volume set to {level} percent")
        else:
            speak("I couldn't control the volume")
        return
    if "volume up" in t or "increase volume" in t or "louder" in t:
        change_volume(10)
        speak(f"Volume up. Now at {get_volume() or 'unknown'} percent")
        return
    if "volume down" in t or "decrease volume" in t or "quieter" in t:
        change_volume(-10)
        speak(f"Volume down. Now at {get_volume() or 'unknown'} percent")
        return
    if "mute" in t or "unmute" in t:
        press_media_key(VK_VOLUME_MUTE)
        speak("Toggled mute")
        return

    # Brightness
    m = re.search(r'(?:set\s+)?brightness\s+(?:to\s+)?(\d+)', t)
    if m:
        level = int(m.group(1))
        try:
            sbc.set_brightness(level)
            speak(f"Brightness set to {level} percent")
        except Exception as e:
            speak("I couldn't change brightness")
        return
    if "brightness up" in t or "increase brightness" in t:
        try:
            cur = sbc.get_brightness()
            if cur:
                sbc.set_brightness(min(100, cur[0] + 10))
                speak("Brightness increased")
        except Exception:
            speak("I couldn't change brightness")
        return
    if "brightness down" in t or "decrease brightness" in t:
        try:
            cur = sbc.get_brightness()
            if cur:
                sbc.set_brightness(max(0, cur[0] - 10))
                speak("Brightness decreased")
        except Exception:
            speak("I couldn't change brightness")
        return

    # Media control
    if any(x in t for x in ["play music", "pause music", "play pause", "pause", "resume", "play song", "pause song"]):
        press_media_key(VK_MEDIA_PLAY_PAUSE)
        speak("Toggled playback")
        return
    if any(x in t for x in ["next song", "next track", "skip", "skip song"]):
        press_media_key(VK_MEDIA_NEXT_TRACK)
        speak("Next track")
        return
    if any(x in t for x in ["previous song", "previous track", "last song", "go back"]):
        press_media_key(VK_MEDIA_PREV_TRACK)
        speak("Previous track")
        return

    # Navigate / go to (e.g., "go to program files" after opening C drive)
    if t.startswith("go to ") or t.startswith("navigate to ") or t.startswith("take me to ") or t.startswith("open folder ") or " chalo" in t or " jao" in t or " mein jao" in t:
        ack_thinking(response_lang)
        dest, label = resolve_go_to_path(text if text else t)
        if dest and os.path.exists(dest):
            try:
                set_last_location(dest if os.path.isdir(dest) else os.path.dirname(dest))
                subprocess.Popen(["explorer.exe", dest])
                set_context(last_path=dest, last_action="navigate")
                speak(f"Going to {label or os.path.basename(dest)}", lang=response_lang)
            except Exception:
                speak(f"I couldn't go to {label or dest}", lang=response_lang)
        else:
            speak(f"I couldn't find {label or 'that folder'}. Try saying go to program files in C drive.", lang=response_lang)
        return

    # Open/close apps
    if t.startswith("open ") or t.startswith("launch ") or t.startswith("start ") or " khol" in t or " kholo" in t:
        target = re.sub(r'^(open|launch|start)\s+', '', t).strip()
        target = re.sub(r'\b(kholo|khol do|khol|chalu karo)\b', '', target).strip()

        # Desktop-aware opens (files/folders on desktop) and "show desktop files"
        if "desktop" in t and any(k in t for k in ["show", "list", "what", "kya", "dikhao", "batao", "files", "items", "sab"]):
            ack_thinking(response_lang)
            names, total = list_desktop_items(limit=15)
            if names:
                speak(f"You have {total} items on your desktop. I listed some in the console.", lang=response_lang)
                print("[Desktop items]")
                for n in names:
                    print(" -", n)
            else:
                speak("I couldn't read your desktop items.", lang=response_lang)
            return

        # If user refers to something on desktop, or asks to open a file/folder that exists on desktop
        desktop_target = target
        desktop_target = re.sub(r'\b(on|from|my)\s+desktop\b', '', desktop_target).strip()
        desktop_target = re.sub(r'\bdesktop\s+(par|pe|mein|me)\b', '', desktop_target).strip()
        if "desktop" in t and desktop_target and desktop_target not in ("", "desktop"):
            it = find_on_desktop(desktop_target)
            if it:
                if open_path(it["path"]):
                    set_last_location(it["path"] if it["is_dir"] else os.path.dirname(it["path"]))
                    set_context(last_path=it["path"], last_action="open_desktop_item")
                    speak(f"Opening {os.path.splitext(it['name'])[0]}", lang=response_lang)
                    return
        # Even without saying "desktop", prefer a strong desktop match for short file/folder names
        if target and len(target) <= 60 and not any(x in target for x in [".com", ".org", ".net", ".io", ".in", "http", "www"]):
            it = find_on_desktop(target)
            if it and ("open" in t or "kholo" in t or "launch" in t):
                if it["is_dir"] or os.path.splitext(it["name"])[1].lower() in (".lnk", ".url", ".txt", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".png", ".jpg", ".jpeg", ".mp3", ".mp4", ".mkv", ".zip", ".rar", ".py", ".json", ".exe"):
                    if open_path(it["path"]):
                        set_last_location(it["path"] if it["is_dir"] else os.path.dirname(it["path"]))
                        set_context(last_path=it["path"], last_action="open_desktop_item")
                        speak(f"Opening {os.path.splitext(it['name'])[0]}", lang=response_lang)
                        return

        # 1) Check for drive/path requests first (fixes "open i drive" opening explorer generically)
        drive_path, drive_letter = resolve_drive_path(target) if target else resolve_drive_path(t)
        if drive_path and ("drive" in t or "disk" in t or re.search(r'\b[a-z]\s*:', t) or re.search(r'\b(file explorer|explorer|folder)\b', t) or t.strip() in [f"open {drive_letter.lower()} drive", f"{drive_letter.lower()} drive"]):
            try:
                set_last_location(drive_path)
                subprocess.Popen(["explorer.exe", drive_path])
                set_context(last_path=drive_path, last_action="open_drive")
                speak(f"Opening {drive_letter} drive", lang=response_lang)
            except Exception:
                speak(f"I couldn't open {drive_letter} drive", lang=response_lang)
            return
        # Also handle simple "open c drive" / "i drive kholo" even without extra words
        if drive_path and len(target.split()) <= 3:
            try:
                set_last_location(drive_path)
                subprocess.Popen(["explorer.exe", drive_path])
                set_context(last_path=drive_path, last_action="open_drive")
                speak(f"Opening {drive_letter} drive", lang=response_lang)
            except Exception:
                speak(f"I couldn't open {drive_letter} drive", lang=response_lang)
            return

        # 2b) Folder navigation intents inside "open ..." (e.g., "open program files in c drive")
        nav_target = (target or t).strip()
        if nav_target:
            dest, label = resolve_go_to_path("go to " + nav_target)
            if dest and os.path.isdir(dest):
                if any(k in t for k in ["folder", "drive", "program files", "users", "windows", "documents", "downloads", "desktop", "pictures", "videos", "music", "directory", "mein", " in "]):
                    try:
                        set_last_location(dest)
                        subprocess.Popen(["explorer.exe", dest])
                        set_context(last_path=dest, last_action="navigate")
                        speak(f"Opening {label or os.path.basename(dest)}", lang=response_lang)
                    except Exception:
                        speak(f"I couldn't open {label or dest}", lang=response_lang)
                    return

        # 2) Website
        # Force YouTube properties to Chrome (uses Chrome's current/default profile)
        if "youtube studio" in target or "studio.youtube.com" in target:
            open_in_chrome("https://studio.youtube.com/", response_lang=response_lang)
            speak(_pick({"en": "Opening YouTube Studio in Chrome.", "hi": "Chrome mein YouTube Studio khol raha hoon."}, response_lang), lang=response_lang)
            return
        if "youtube music" in target or "music.youtube.com" in target:
            play_music_on_youtube(None, response_lang=response_lang)
            return
        if target.strip() in ("youtube", "youtube.com", "www.youtube.com") or "youtube.com" in target:
            url = target if target.startswith("http") else ("https://www.youtube.com/" if target.strip() in ("youtube", "youtube.com", "www.youtube.com") else "https://" + target.replace(" ", ""))
            open_in_chrome(url, response_lang=response_lang)
            speak(_pick({"en": "Opening YouTube in Chrome.", "hi": "Chrome mein YouTube khol raha hoon."}, response_lang), lang=response_lang)
            return
        if any(x in target for x in [".com", ".org", ".net", ".io", ".in", "http", "www"]):
            url = target if target.startswith("http") else "https://" + target.replace(" ", "")
            webbrowser.open(url)
            set_context(last_action="open_website")
            speak(f"Opening {target}", lang=response_lang)
            return

        # 3) Search
        if target.startswith("search for ") or target.startswith("google "):
            query = re.sub(r'^(search for|google)\s+', '', target)
            ack_thinking(response_lang)
            webbrowser.open(f"https://www.google.com/search?q={requests.utils.quote(query)}")
            set_context(last_action="search")
            speak(f"Searching for {query}", lang=response_lang)
            return

        # 4) Known aliases / direct commands
        app_key = find_app_key(target)
        if app_key:
            if open_app(app_key):
                set_context(last_app=app_key, last_action="open_app")
                speak(f"Opening {app_key}", lang=response_lang)
            else:
                speak(f"I couldn't open {app_key}", lang=response_lang)
            return

        # 5) Search installed apps (Start Menu + App Paths)
        ack_thinking(response_lang)
        ok, display = launch_installed_app(target)
        if ok:
            set_context(last_app=display or target, last_action="open_app")
            speak(f"Opening {display}", lang=response_lang)
            return

        # 6) Try opening as path
        if open_path(target):
            set_context(last_path=target, last_action="open_path")
            speak(f"Opening {target}", lang=response_lang)
            return

        # 7) Fallback
        try:
            subprocess.Popen(f'start "" "{target}"', shell=True)
            set_context(last_app=target, last_action="open_fallback")
            speak(f"Trying to open {target}", lang=response_lang)
        except Exception:
            speak(f"I don't know how to open {target}", lang=response_lang)
        return

    if t.startswith("close ") or t.startswith("quit ") or t.startswith("kill ") or " band karo" in t or " band kar do" in t or t.endswith(" band karo") or t.endswith(" band kar do"):
        target = re.sub(r'^(close|quit|kill)\s+', '', t).strip()
        target = re.sub(r'\b(band karo|band kar do|band)\b', '', target).strip()
        # protect system/sleep/shutdown intents handled earlier
        if not target or target in ("pc", "computer", "laptop"):
            speak("What should I close?")
            return
        app_key = find_app_key(target)
        closed = False
        if app_key:
            closed = close_app(app_key)
        if not closed:
            # try smarter process matching (installed apps/running processes)
            count = _kill_processes_by_name_substring(target)
            closed = count > 0
        if not closed:
            # last resort: taskkill by guessed exe name
            try:
                exe_guess = target.replace(" ", "") + ".exe"
                r = subprocess.run(["taskkill", "/F", "/IM", exe_guess], capture_output=True, text=True)
                out = (r.stdout or "") + (r.stderr or "")
                closed = r.returncode == 0 or "SUCCESS" in out.upper()
            except Exception:
                closed = False
        if closed:
            speak(f"Closed {target}")
        else:
            speak(f"I couldn't find {target} running")
        return

    # Search web
    if t.startswith("search ") or t.startswith("google "):
        query = re.sub(r'^(search|google)\s+(for\s+)?', '', t)
        webbrowser.open(f"https://www.google.com/search?q={requests.utils.quote(query)}")
        speak(f"Searching for {query}")
        return

    # File search
    if "find file" in t or "search file" in t or "find " in t and "file" in t:
        query = re.sub(r'(find|search)\s+(file|for file|files?)\s*(named|called)?\s*', '', t).strip()
        if query:
            speak(f"Searching for {query}. This may take a moment.")
            results = search_files(query)
            if results:
                speak(f"Found {len(results)} results. Opening the first one.")
                open_path(results[0])
                for r in results[:5]:
                    print(f"  - {r}")
            else:
                speak("I couldn't find any matching files")
        else:
            speak("What file should I search for?")
        return

    # Open common folders
    if "open downloads" in t or "downloads kholo" in t:
        open_path(str(Path.home() / "Downloads"))
        speak("Opening Downloads")
        return
    if "open documents" in t or "documents kholo" in t:
        open_path(str(Path.home() / "Documents"))
        speak("Opening Documents")
        return
    if "open desktop" in t or "desktop kholo" in t or "show desktop" in t:
        open_path(DESKTOP_PATH)
        speak("Opening Desktop")
        return
    if "open pictures" in t or "pictures kholo" in t:
        open_path(str(Path.home() / "Pictures"))
        speak("Opening Pictures")
        return

    # Type text
    if t.startswith("type "):
        text_to_type = text[5:].strip()  # use original case-ish
        pyautogui.write(text_to_type, interval=0.02)
        speak("Typed it")
        return

    # Screenshot
    if "screenshot" in t or "screen shot" in t:
        path = os.path.join(DESKTOP_PATH, f"screenshot_{int(time.time())}.png")
        pyautogui.screenshot(path)
        speak(f"Screenshot saved to desktop")
        return

    # Smarter fallback: try to infer intent instead of giving up
    # Try as a direct "go to/open folder" if it looks like a path/name
    if any(k in t for k in ["folder", "directory", "drive", "desktop file", " on desktop", " in c", " in d", " in e", " in i"]):
        dest, label = resolve_go_to_path("go to " + t)
        if dest and os.path.exists(dest):
            subprocess.Popen(["explorer.exe", dest])
            set_last_location(dest if os.path.isdir(dest) else os.path.dirname(dest))
            speak(f"Opening {label or os.path.basename(dest)}", lang=response_lang)
            return

    # Try desktop match as last chance
    it = find_on_desktop(t)
    if it:
        open_path(it["path"])
        set_last_location(it["path"] if it["is_dir"] else os.path.dirname(it["path"]))
        speak(f"Opening {os.path.splitext(it['name'])[0]}", lang=response_lang)
        return

    # Try installed app fuzzy match
    ok, display = launch_installed_app(t)
    if ok:
        set_context(last_app=display or t, last_action="open_app")
        speak(f"Opening {display}", lang=response_lang)
        return

    # Optional LLM brain: deeper reasoning / human-like conversation and intent extraction
    brain = llm_reason_and_reply(text, response_lang=response_lang)
    if brain and isinstance(brain, dict):
        action = (brain.get("action") or "answer").lower()
        reply = (brain.get("reply") or "").strip()
        intent = (brain.get("intent") or "").lower().strip()
        target = (brain.get("target") or "").strip()
        if action == "clarify":
            speak(reply or _pick({"en":"Can you clarify that?", "hi":"Thoda clear batao?"}, response_lang), lang=response_lang)
            return
        if action == "command":
            # Safe mapped intents only
            safe = {
                "open_app": lambda v: execute_command(f"open {v}"),
                "open_website": lambda v: webbrowser.open(v if v.startswith("http") else f"https://{v}"),
                "open_path": lambda v: open_path(v),
                "go_to": lambda v: execute_command(f"go to {v}"),
                "close_app": lambda v: execute_command(f"close {v}"),
                "search": lambda v: webbrowser.open(f"https://www.google.com/search?q={requests.utils.quote(v)}"),
                "volume_up": lambda v: change_volume(10),
                "volume_down": lambda v: change_volume(-10),
                "mute": lambda v: toggle_mute(),
                "brightness_up": lambda v: set_brightness(min(100, (sbc.get_brightness(display=0)[0] if (sbc and sbc.get_brightness(display=0)) else 50) + 10)),
                "brightness_down": lambda v: set_brightness(max(0, (sbc.get_brightness(display=0)[0] if (sbc and sbc.get_brightness(display=0)) else 50) - 10)),
                "play_pause": lambda v: media_key(0xB3),
                "next_track": lambda v: media_key(0xB0),
                "prev_track": lambda v: media_key(0xB1),
                "lock": lambda v: lock_pc(),
                "screenshot": lambda v: execute_command("screenshot"),
                "open_desktop": lambda v: open_path(DESKTOP_PATH),
            }
            if intent in ("shutdown", "restart"):
                # never auto-execute; force confirmation
                CONTEXT["pending_confirm"] = {"action": shutdown_pc if intent == "shutdown" else restart_pc, "prompt": intent}
                speak(reply or _pick({"en": f"Do you want me to {intent} your PC? Say yes to confirm.", "hi": f"Kya main PC {intent} kar doon? Confirm ke liye haan bolo."}, response_lang), lang=response_lang)
                return
            if intent in safe and (target or intent not in ("open_app","open_website","open_path","go_to","close_app","search")):
                if reply:
                    speak(reply, lang=response_lang)
                try:
                    safe[intent](target)
                except Exception:
                    speak(_pick({"en":"I tried, but that action failed.", "hi":"Maine try kiya, lekin action fail ho gaya."}, response_lang), lang=response_lang)
                return
            # If command intent but missing/unsafe target mapping, ask
            if reply:
                speak(reply, lang=response_lang)
                return
        if reply:
            speak(reply, lang=response_lang)
            return

    speak(_pick({
        "en": ["I didn't catch that clearly. Could you say it again?",
               "Not sure what you meant. Please repeat that."],
        "hi": ["Mujhe clearly samajh nahi aaya. Dobara bolo?",
               "Main samjha nahi. Kripya phir se bolo."],
    }, response_lang), lang=response_lang)

# Voice recognition loop
audio_queue = queue.Queue()

def audio_callback(indata, frames, time_info, status):
    if status:
        print(status, file=sys.stderr)
    audio_queue.put(bytes(indata))

def ensure_model(lang=None):
    """Ensure Vosk model for the given language is downloaded. Returns model path or None."""
    if lang is None:
        lang = "en" if LANGUAGE_MODE == "auto" else LANGUAGE_MODE
    info = VOSK_MODELS.get(lang) or VOSK_MODELS["en"]
    model_path = os.path.join(os.path.dirname(__file__), info["dir"])
    if os.path.isdir(model_path):
        return model_path
    model_zip = os.path.join(os.path.dirname(__file__), info["dir"] + ".zip")
    model_url = info["url"]
    print(f"[Malkin] Downloading {lang} speech model from {model_url} ...")
    speak(f"Downloading {lang} speech recognition model. This happens once.", lang="en")
    try:
        import zipfile
        with requests.get(model_url, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(model_zip, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        with zipfile.ZipFile(model_zip, 'r') as z:
            z.extractall(os.path.dirname(__file__))
        try:
            os.remove(model_zip)
        except Exception:
            pass
        print(f"[Malkin] {lang} model downloaded.")
        return model_path
    except Exception as e:
        print(f"[Malkin] Failed to download {lang} model: {e}")
        speak(f"I couldn't download the {lang} speech model. Check your internet.", lang="en")
        return None

def ensure_whisper_model():
    """Load faster-whisper model for bilingual (auto) mode. Returns True on success."""
    global whisper_model, WHISPER_AVAILABLE
    if whisper_model is not None:
        return True
    if not WHISPER_AVAILABLE:
        return False
    try:
        from faster_whisper import WhisperModel
        print(f"[Malkin] Loading Whisper model '{WHISPER_MODEL_SIZE}' on {WHISPER_DEVICE}...")
        speak("Loading speech model. This may take a moment on first run.", lang="en")
        if WHISPER_DEVICE.lower() == "cpu":
            whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
            print("[Malkin] Whisper loaded on CPU.")
            return True
        # Optional GPU path (may be unstable on some systems)
        try:
            whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cuda", compute_type=WHISPER_COMPUTE)
            print("[Malkin] Whisper loaded on GPU.")
        except Exception as gpu_err:
            print(f"[Malkin] GPU load failed ({gpu_err}); falling back to CPU.")
            whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
            print("[Malkin] Whisper loaded on CPU.")
        return True
    except Exception as e:
        print(f"[Malkin] Failed to load Whisper: {e}")
        WHISPER_AVAILABLE = False
        whisper_model = None
        return False

def _dedupe_repeated_text(text):
    """Collapse pathological ASR repetitions like 'open youtube music. open youtube music...'."""
    if not text:
        return ""
    t = re.sub(r'\s+', ' ', text).strip()
    # Collapse repeated sentences/clauses
    parts = [p.strip(" .,!?:;-") for p in re.split(r'[.!\n]+', t) if p.strip(" .,!?:;-")]
    if parts:
        dedup = []
        last = None
        rep = 0
        for p in parts:
            pl = p.lower()
            if pl == last:
                rep += 1
                continue
            if rep >= 1 and dedup:
                # keep first occurrence only
                pass
            dedup.append(p)
            last = pl
            rep = 0
        t = ". ".join(dedup).strip()
    # Collapse repeated phrase loops (A B A B A B or A A A)
    words = t.split()
    if len(words) > 8:
        # try chunk repetition detection
        for n in range(2, min(9, len(words)//2 + 1)):
            chunk = [w.lower().strip(".,!?:;") for w in words[:n]]
            if not any(chunk):
                continue
                continue
            reps = 1
            i = n
            while i + n <= len(words):
                nxt = [w.lower().strip(".,!?:;") for w in words[i:i+n]]
                if nxt == chunk:
                    reps += 1
                    i += n
                else:
                    break
            if reps >= 3:  # pathological loop
                t = " ".join(words[:n])
                break
    # final sanity
    if len(t) > 220:
        t = t[:220].rsplit(" ", 1)[0]
    return t.strip()


def transcribe_whisper(audio_bytes):
    """Transcribe int16 audio bytes (16kHz mono) using faster-whisper, auto-detecting en/hi."""
    global whisper_model
    if whisper_model is None:
        return "", None
    def _do_transcribe(vad):
        audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        # Skip very quiet/short audio
        if audio.size < SAMPLE_RATE * 0.25:
            return "", None
        rms = float(np.sqrt(np.mean(audio * audio))) if audio.size else 0.0
        if rms < 120:
            return "", None
        prompt = "Malkin, open YouTube. Open YouTube Music. Open YouTube Studio. Open Chrome. Play music. Open I drive."
        segments, info = whisper_model.transcribe(
            audio,
            beam_size=1,
            best_of=1,
            vad_filter=vad,
            vad_parameters=dict(min_silence_duration_ms=350, speech_pad_ms=120) if vad else None,
            language=None,  # auto-detect; handles Hinglish/mixed well
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            compression_ratio_threshold=2.2,
            log_prob_threshold=-1.0,
            initial_prompt=prompt,
            temperature=0.0,
            without_timestamps=True,
        )
        texts = []
        for seg in segments:
            st = (getattr(seg, "text", "") or "").strip()
            if not st:
                continue
            # skip low-confidence/noise segments when metadata exists
            try:
                if getattr(seg, "no_speech_prob", 0) and seg.no_speech_prob > 0.7:
                    continue
                if getattr(seg, "avg_logprob", 0) is not None and seg.avg_logprob < -1.2:
                    continue
            except Exception:
                pass
            texts.append(st)
            if len(" ".join(texts)) > 240:
                break
        text = " ".join(texts).strip()
        text = _dedupe_repeated_text(text)
        lang = getattr(info, "language", None)
        return text, lang
    try:
        try:
            return _do_transcribe(True)
        except Exception as vad_err:
            if "onnxruntime" in str(vad_err).lower() or "vad" in str(vad_err).lower():
                return _do_transcribe(False)
            raise
    except Exception as e:
        # If runtime error (often CUDA/cuBLAS), reload on CPU and retry once
        try:
            print("[Malkin] Whisper runtime issue; forcing CPU model...")
            from faster_whisper import WhisperModel
            whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
            print("[Malkin] Whisper reloaded on CPU.")
            try:
                return _do_transcribe(True)
            except Exception:
                return _do_transcribe(False)
        except Exception as e2:
            print(f"[Whisper] CPU fallback failed: {e2}")
            return "", None

def main():
    global LANGUAGE_MODE
    print("=" * 50)
    print("  MALKIN (मालकिन) - Voice Assistant for Windows")
    print("=" * 50)
    print(f"Wake word: '{WAKE_WORD}' | Mode: {LANGUAGE_MODE} (English + Hindi supported)")
    print("Say 'Malkin, stop listening' or 'Malkin, go to sleep' to pause.")
    print("In auto mode you can mix English and Hindi in the same command.")
    print("Say 'Malkin, switch to auto / hindi / english' to change mode.")
    print("Press Ctrl+C to exit.\n")

    use_whisper = False
    model = None
    recognizer = None

    if LANGUAGE_MODE == "auto" and WHISPER_AVAILABLE:
        if ensure_whisper_model():
            use_whisper = True
            speak(f"{ASSISTANT_NAME} online.", lang="en")
        else:
            speak("Whisper not available, falling back to English recognition.", lang="en")
            LANGUAGE_MODE = "en"

    if not use_whisper:
        # Vosk fallback for single-language modes
        lang = "en" if LANGUAGE_MODE == "auto" else LANGUAGE_MODE
        model_path = ensure_model(lang)
        if not model_path:
            return
        try:
            model = vosk.Model(model_path)
        except Exception as e:
            print(f"Failed to load model: {e}")
            speak("Failed to load the speech model.")
            return

    def make_recognizer():
        rec = vosk.KaldiRecognizer(model, SAMPLE_RATE)
        rec.SetWords(True)
        rec.SetPartialWords(True)
        try:
            # Help Vosk keep likely wake/command words in lattice (esp. EN wake variants)
            rec.SetGrammar(json.dumps([
                "malkin", "malkeen", "maalkeen", "maalkin", "mal kin", "mulkin", "mulkeen", "mal queen",
                "hey malkin", "hello malkin", "suno malkin",
                "open youtube", "open youtube studio", "open youtube music", "play music",
                "open chrome", "open i drive", "yes", "no", "[unk]"
            ]))
        except Exception:
            pass
        return rec

        recognizer = make_recognizer()
        if LANGUAGE_MODE != "auto" or not WHISPER_AVAILABLE:
            speak(f"{ASSISTANT_NAME} online.", lang="en")

    listening_for_command = False
    command_buffer = ""
    command_audio = bytearray()
    last_wake_time = 0
    last_speech_time = 0
    WAKE_TIMEOUT = 8
    SILENCE_AFTER_SPEECH = 0.7 if use_whisper else 0.6
    MIN_COMMAND_CHARS = 2
    MAX_CMD_SECONDS = 10

    def reload_for_mode():
        nonlocal use_whisper, model, recognizer, wake_vosk_model, wake_recognizer
        if LANGUAGE_MODE == "auto" and WHISPER_AVAILABLE and ensure_whisper_model():
            use_whisper = True
            model = None
            recognizer = None
            # keep wake recognizer
            return
        use_whisper = False
        lang = "en" if LANGUAGE_MODE == "auto" else LANGUAGE_MODE
        mp = ensure_model(lang)
        if mp:
            model = vosk.Model(mp)
            recognizer = vosk.KaldiRecognizer(model, SAMPLE_RATE)
            recognizer.SetWords(True)
            recognizer.SetPartialWords(True)

    # For auto/bilingual mode: use Vosk (EN) as lightweight wake-word detector,
    # and buffer raw audio for Whisper transcription once wake word is heard.
    wake_vosk_model = None
    wake_recognizer = None
    if use_whisper:
        try:
            wake_mp = ensure_model("en")
            if wake_mp:
                wake_vosk_model = vosk.Model(wake_mp)
                wake_recognizer = vosk.KaldiRecognizer(wake_vosk_model, SAMPLE_RATE)
                wake_recognizer.SetWords(False)
                try:
                    wake_recognizer.SetGrammar(json.dumps([
                        "malkin", "malkeen", "maalkeen", "maalkin", "mal kin", "mulkin", "mulkeen",
                        "mal queen", "markin", "hey malkin", "hello malkin", "suno malkin", "[unk]"
                    ]))
                except Exception:
                    pass
        except Exception as e:
            print(f"[Malkin] Wake detector load failed: {e}")

    def handle_mode_switch(check_text):
        nonlocal listening_for_command, command_buffer, command_audio, last_speech_time
        global LANGUAGE_MODE
        tl = check_text.lower()
        if "auto mode" in tl or "bilingual" in tl or "hinglish" in tl or "dono" in tl or ("both" in tl and ("hindi" in tl or "english" in tl)):
            LANGUAGE_MODE = "auto"
            speak("Auto mode on.", lang="en")
            listening_for_command = False; command_buffer = ""; command_audio = bytearray(); last_speech_time = 0
            reload_for_mode()
            return True
        if "hindi mode" in tl or ("switch" in tl and "hindi" in tl) or "bol hindi" in tl or "hindi mein bolo" in tl or "sirf hindi" in tl:
            LANGUAGE_MODE = "hi"
            speak("Hindi mode mein switch kar raha hoon.", lang="hi")
            listening_for_command = False; command_buffer = ""; command_audio = bytearray(); last_speech_time = 0
            reload_for_mode()
            return True
        if "english mode" in tl or ("switch" in tl and "english" in tl) or "speak english" in tl or "angrezi mein bolo" in tl or "only english" in tl:
            LANGUAGE_MODE = "en"
            speak("Switching to English mode.", lang="en")
            listening_for_command = False; command_buffer = ""; command_audio = bytearray(); last_speech_time = 0
            reload_for_mode()
            return True
        return False

    def finalize_whisper_command():
        nonlocal listening_for_command, command_buffer, command_audio, last_speech_time
        audio_data = bytes(command_audio)
        command_audio = bytearray()
        command_buffer = ""
        listening_for_command = False
        last_speech_time = 0
        # need enough audio to avoid hallucinated/noise transcripts
        if len(audio_data) < int(SAMPLE_RATE * 2 * 0.45):
            return
        # energy gate: skip very quiet captures
        try:
            a = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(a * a))) if a.size else 0.0
        except Exception:
            rms = 0.0
        if rms < 180:
            return
        print("[Malkin] Processing...")
        text, detected_lang = transcribe_whisper(audio_data)
        if text:
            tl = correct_asr_text(text)
            tl = _dedupe_repeated_text(tl)
            for alias in WAKE_WORD_ALIASES:
                tl = tl.replace(alias, " ")
            tl = re.sub(r'\s+', ' ', tl).strip(" .,!?:;-")
            if handle_mode_switch(tl):
                return
            print(f"[Heard/{detected_lang or 'auto'}] {tl}")
            if len(tl) >= MIN_COMMAND_CHARS:
                execute_command(tl)
            else:
                speak("Dobara bolo?" if LANGUAGE_MODE == "hi" else "Please say that again.", lang=("hi" if LANGUAGE_MODE=="hi" else "en"), fast=True)
        else:
            # stay quiet on no-speech/noise to avoid annoying loops
            return

    def rms_int16(data_bytes):
        try:
            a = np.frombuffer(data_bytes, dtype=np.int16).astype(np.float32)
            if a.size == 0:
                return 0.0
            return float(np.sqrt(np.mean(a * a)))
        except Exception:
            return 0.0

    # Speech energy gate for whisper mode (helps "listen like human" - waits for speech/silence)
    SPEECH_RMS_THRESH = 900   # higher = less sensitive to background noise (tune: 500-1500)
    WAKE_SPEECH_RMS_THRESH = 1100  # stricter gate before wake detection from Vosk/Whisper flow
    MIN_WAKE_AUDIO_SEC = 0.35
    last_above_thresh = 0.0

    try:
        with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=1600, device=DEVICE,
                               dtype='int16', channels=1, callback=audio_callback):
            while True:
                try:
                    data = audio_queue.get(timeout=0.15)
                except queue.Empty:
                    if listening_for_command and last_speech_time > 0:
                        if time.time() - last_speech_time > SILENCE_AFTER_SPEECH:
                            if use_whisper:
                                finalize_whisper_command()
                            elif command_buffer.strip():
                                cmd = command_buffer.strip(); command_buffer = ""
                                listening_for_command = False; last_speech_time = 0
                                execute_command(cmd)
                            continue
                    if listening_for_command and (time.time() - last_wake_time > WAKE_TIMEOUT):
                        if use_whisper and len(command_audio) > SAMPLE_RATE:
                            finalize_whisper_command()
                        elif (not use_whisper) and command_buffer.strip():
                            cmd = command_buffer.strip(); command_buffer = ""
                            listening_for_command = False; last_speech_time = 0
                            execute_command(cmd)
                        else:
                            listening_for_command = False; command_buffer = ""; command_audio = bytearray(); last_speech_time = 0
                    continue

                if use_whisper:
                    # Use small Vosk EN for wake detection; buffer raw audio for Whisper command
                    if wake_recognizer is not None and not listening_for_command:
                        level = rms_int16(data)
                        # Ignore quiet/background audio to prevent false wake activations
                        if level < WAKE_SPEECH_RMS_THRESH:
                            continue
                        if wake_recognizer.AcceptWaveform(data):
                            wr = json.loads(wake_recognizer.Result())
                            wt = wr.get("text", "").strip().lower()
                        else:
                            wp = json.loads(wake_recognizer.PartialResult())
                            wt = wp.get("partial", "").strip().lower()
                        if wt and len(wt) >= 4:
                            detected, after = detect_wake_word(wt)
                            if detected:
                                listening_for_command = True
                                last_wake_time = time.time()
                                last_speech_time = time.time()
                                command_audio = bytearray()
                                wake_recognizer.Reset()
                                speak("Haan?" if LANGUAGE_MODE == "hi" else "Yes?", lang=("hi" if LANGUAGE_MODE=="hi" else "en"), fast=True)
                                # don't buffer the wake chunk itself
                                continue
                    if listening_for_command:
                        command_audio.extend(data)
                        level = rms_int16(data)
                        if level > SPEECH_RMS_THRESH:
                            last_speech_time = time.time()
                        # safety: if audio keeps coming without speech/silence state, finalize on timeout handled above
                        # cap max command length
                        if len(command_audio) > SAMPLE_RATE * 2 * MAX_CMD_SECONDS:
                            finalize_whisper_command()
                    else:
                        pass
                    continue

                # Vosk-only path (single language modes)
                if recognizer.AcceptWaveform(data):
                    result = json.loads(recognizer.Result())
                    text = result.get("text", "").strip().lower()
                    if not text:
                        if listening_for_command and command_buffer.strip() and last_speech_time > 0:
                            if time.time() - last_speech_time > SILENCE_AFTER_SPEECH:
                                cmd = command_buffer.strip(); command_buffer = ""
                                listening_for_command = False; last_speech_time = 0
                                execute_command(cmd)
                        continue
                    print(f"[Heard] {text}")
                    last_speech_time = time.time()
                    if handle_mode_switch(text):
                        recognizer.Reset() if recognizer else None
                        continue
                    if not listening_for_command:
                        detected, after = detect_wake_word(text)
                        if detected:
                            if after and len(after) >= MIN_COMMAND_CHARS:
                                if not handle_mode_switch(after):
                                    execute_command(after)
                            else:
                                listening_for_command = True
                                last_wake_time = time.time()
                                command_buffer = ""
                                speak("Yes?" if LANGUAGE_MODE != "hi" else "Haan ji?")
                    else:
                        clean_text = text
                        for alias in WAKE_WORD_ALIASES:
                            clean_text = clean_text.replace(alias, "").strip()
                        if not clean_text:
                            continue
                        command_buffer = (command_buffer + " " + clean_text).strip() if command_buffer else clean_text
                        if time.time() - last_wake_time > WAKE_TIMEOUT - 1 and len(command_buffer) >= MIN_COMMAND_CHARS:
                            cmd = command_buffer.strip(); command_buffer = ""
                            listening_for_command = False; last_speech_time = 0
                            execute_command(cmd)
                else:
                    partial = json.loads(recognizer.PartialResult())
                    partial_text = partial.get("partial", "").strip().lower()
                    if partial_text:
                        last_speech_time = time.time()
                        if not listening_for_command:
                            detected, after = detect_wake_word(partial_text)
                            if detected:
                                listening_for_command = True
                                last_wake_time = time.time()
                                command_buffer = after
                                recognizer.Reset()
                                speak("Yes?" if LANGUAGE_MODE != "hi" else "Haan ji?")

                if listening_for_command and not use_whisper and (time.time() - last_wake_time > WAKE_TIMEOUT):
                    if command_buffer.strip():
                        cmd = command_buffer.strip(); command_buffer = ""
                        listening_for_command = False; last_speech_time = 0
                        execute_command(cmd)
                    else:
                        listening_for_command = False; command_buffer = ""; last_speech_time = 0

    except KeyboardInterrupt:
        print("\n[Malkin] Shutting down.")
        speak("Goodbye.", lang="en")
    except Exception as e:
        print(f"[Malkin] Error: {e}")
        import traceback
        traceback.print_exc()
        try:
            speak("I encountered an error and need to shut down.")
        except Exception:
            pass

if __name__ == "__main__":
    main()
