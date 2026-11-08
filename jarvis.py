import os
# Reduce noisy HF/Whisper warnings on Windows
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
import sys
# Windows console UTF-8 safety (prevents crashes when printing Hindi/Devanagari)
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
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

# Windows beep for wake feedback
try:
    import winsound
    WINSOUND_AVAILABLE = True
except Exception:
    WINSOUND_AVAILABLE = False

# Windows volume control
try:
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    PYCAW_AVAILABLE = True
except Exception:
    PYCAW_AVAILABLE = False

# Config
WAKE_WORD = "jarvis"
WAKE_WORD_ALIASES = [
    "jarvis", "jarvis.", "jarvis,", "jarvis!",
    "jarvis ji", "hey jarvis", "hello jarvis", "ok jarvis", "hi jarvis",
    "jarvas", "jarvez", "javis", "jarviss", "jaarvis", "jarves", "jarwis", "jervish", "jarvish",
    "jar viz", "jar vis", "jaar viz", "j.a.r.v.i.s", "jar weis", "jarvice", "jarvis hey",
    "chavis", "travis", "jervis", "gervais", "jarvus", "javas", "jarus",
    "charvis", "darvis", "jarvesh", "jharvis",
    "driver", "drivers", "dryver", "draver",  # common ASR mishearing for "Jarvis"
    "doris", "doris's", "dorris", "dollars", "dollar", "readers", "reader", "service", "davis",
    "à¤œà¤¾à¤°à¥à¤µà¤¿à¤¸", "à¤œà¤¾à¤°à¥à¤µà¤¿à¤¸ à¤œà¥€", "à¤¹à¥‡ à¤œà¤¾à¤°à¥à¤µà¤¿à¤¸", "à¤ à¤œà¤¾à¤°à¥à¤µà¤¿à¤¸", "à¤“ à¤œà¤¾à¤°à¥à¤µà¤¿à¤¸",
]  # common mishearings (Vosk EN model variants included)
ASSISTANT_NAME = "Jarvis"

# Common voice misrecognitions (ASR quirks) -> corrections applied before command parsing
ASR_CORRECTIONS = [
    (r'\b(jar\s*vis|jar\s*vas|jar\s*vez|jar\s*viss|jaa+r\s*vis|jar\s*wes|jar\s*wis|jar\s*vish|jer\s*vish|javas|dar\s*vis|jhar\s*vis)\b', 'jarvis'),
    (r'\b(jarvis\s*ji)\b', 'jarvis'),
    (r'\b(tab\s*let|tablate|tablit|teb\s*let|tablets?)\b', 'tablet'),
    (r'\b(your\s*tube|you\s*tube|your\s*cube|you\s*cube|u\s*tube|utube|yoo\s*tube|youtub)\b', 'youtube'),
    (r'\b(youtube\s*musi[ck]|yt\s*musi[ck])\b', 'youtube music'),
    (r'\b(youtube\s*stud[yi]o|yt\s*studio)\b', 'youtube studio'),
    (r'\b(chromee?|krom|crome|go+gle\s*chrome)\b', 'chrome'),
    (r'\b(note\s*pad|not\s*paid)\b', 'notepad'),
    (r'\b(file\s*explorer|explora)\b', 'explorer'),
]

# Wake-only ASR corrections (applied in detect_wake_word; avoids changing "driver" in commands)
WAKE_ASR_CORRECTIONS = [
    (r'\b(drivers?|dryver|draver|driver\'?s)\b', 'jarvis'),
    (r'\b(doris\'?s?|dorris|dollars?|readers?|davis|service)\b', 'jarvis'),
]

def correct_asr_text(text):
    t = " " + (text or "").strip().lower() + " "
    for pat, repl in ASR_CORRECTIONS:
        t = re.sub(pat, repl, t, flags=re.IGNORECASE)
    t = re.sub(r'\s+', ' ', t).strip()
    t = t.replace("टैबलेट", "tablet").replace("टेबलेट", "tablet").replace("टैब", "tablet")
    t = re.sub(r'\byoutube\s+musi[ck]\b', 'youtube music', t)
    t = re.sub(r'\byoutube\s+stud[yi]o\b', 'youtube studio', t)
    return t


def correct_wake_text(text):
    t = correct_asr_text(text)
    if not t:
        return t
    t2 = " " + t + " "
    for pat, repl in WAKE_ASR_CORRECTIONS:
        t2 = re.sub(pat, repl, t2, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', t2).strip()

LANGUAGE_MODE = "auto"  # "auto" (English+Hindi mixed), "en", or "hi"
VOSK_MODELS = {
    "en": {"dir": "vosk-model-small-en-us-0.15", "url": "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"},
    "hi": {"dir": "vosk-model-small-hi-0.22", "url": "https://alphacephei.com/vosk/models/vosk-model-small-hi-0.22.zip"},
}
SAMPLE_RATE = 16000  # recognition target (Vosk/Whisper)
CAPTURE_SAMPLE_RATE = SAMPLE_RATE  # runtime: device-supported input rate
_RESAMPLE_POS = 0.0

def resolve_capture_sample_rate(device, target=SAMPLE_RATE):
    for sr in [target, 16000, 48000, 44100, 32000, 22050, 8000]:
        try:
            sd.check_input_settings(device=device, samplerate=sr, channels=1, dtype='int16')
            return int(sr)
        except Exception:
            continue
    try:
        d = sd.query_devices(device, 'input') if device is not None else sd.query_devices(kind='input')
        sr = int(d.get('default_samplerate') or target)
        return sr
    except Exception:
        return int(target)

def open_input_stream(*, samplerate, blocksize, device, callback):
    global CAPTURE_SAMPLE_RATE, _RESAMPLE_POS
    attempts = []
    seen = set()
    candidates = [samplerate, SAMPLE_RATE, 48000, 44100, 32000, 22050, 16000, 8000]
    try:
        d = sd.query_devices(device, 'input') if device is not None else sd.query_devices(kind='input')
        candidates.insert(1, int(d.get('default_samplerate') or samplerate))
    except Exception:
        pass
    last = None
    for sr in candidates:
        try:
            sr = int(sr)
        except Exception:
            continue
        if sr in seen:
            continue
        seen.add(sr)
        bs = max(256, int(blocksize * sr / SAMPLE_RATE)) if SAMPLE_RATE else blocksize
        try:
            stream = sd.RawInputStream(samplerate=sr, blocksize=bs, device=device,
                                       dtype='int16', channels=1, callback=callback)
            CAPTURE_SAMPLE_RATE = sr
            _RESAMPLE_POS = 0.0
            return stream
        except Exception as e:
            last = e
            attempts.append(f"{sr}Hz: {e}")
    raise RuntimeError("No supported input sample rate for selected microphone. Tried: " + " | ".join(attempts[:6]))

def resample_int16(data_bytes, src_rate, dst_rate=SAMPLE_RATE):
    global _RESAMPLE_POS
    if not data_bytes or src_rate == dst_rate:
        return data_bytes
    try:
        if np is None:
            return data_bytes
        a = np.frombuffer(data_bytes, dtype=np.int16).astype(np.float32)
        if a.size == 0:
            return data_bytes
        ratio = float(src_rate) / float(dst_rate)
        n_out = int((a.size - _RESAMPLE_POS) / ratio)
        if n_out <= 0:
            return b""
        idx = _RESAMPLE_POS + np.arange(n_out, dtype=np.float32) * ratio
        i0 = np.floor(idx).astype(np.int64)
        frac = idx - i0
        i1 = np.minimum(i0 + 1, a.size - 1)
        out = a[i0] * (1.0 - frac) + a[i1] * frac
        _RESAMPLE_POS = float(idx[-1] + ratio - (a.size - 1))
        if _RESAMPLE_POS < 0:
            _RESAMPLE_POS = 0.0
        if _RESAMPLE_POS > 2.0:
            _RESAMPLE_POS = _RESAMPLE_POS % 1.0
        return np.clip(out, -32768, 32767).astype(np.int16).tobytes()
    except Exception:
        return data_bytes

# Microphone selection
# Default: always use "USB PnP Audio Device" when present.
# Override with env JARVIS_DEVICE=<index|substring>; set JARVIS_DEVICE=default for system default.
PREFERRED_MIC_SUBSTRINGS = [
    s.strip().lower() for s in
    os.environ.get("JARVIS_MIC_PREFER", "usb pnp audio,usb pnp").split(",")
    if s.strip()
]
DEVICE = os.environ.get("JARVIS_DEVICE")  # resolved to an input device index/name in main()

# Tablet control (ADB). Galaxy Tab A 2017 (SM-T385) detected on this PC.
# Override: set JARVIS_TABLET_ADB=C:\path\to\adb.exe, JARVIS_TABLET_SERIAL=192.168.1.2:5555
TABLET_ADB_PATH = os.environ.get("JARVIS_TABLET_ADB") or r"C:\Users\music\platform-tools\adb.exe"
TABLET_ADB_SERIAL = os.environ.get("JARVIS_TABLET_SERIAL", "192.168.1.2:5555")

def resolve_input_device():
    requested = os.environ.get("JARVIS_DEVICE")
    if requested is not None and str(requested).strip().lower() in ("", "default", "system"):
        return None
    try:
        if requested is not None and str(requested).strip().isdigit():
            idx = int(str(requested).strip())
            d = sd.query_devices(idx)
            if d.get("max_input_channels", 0) > 0:
                return idx
    except Exception:
        pass
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        prefs = {"Windows WDM-KS": 0, "Windows WASAPI": 1, "MME": 2, "Windows DirectSound": 3}
        candidates = []
        for i, d in enumerate(devices):
            if d.get("max_input_channels", 0) <= 0:
                continue
            name = (d.get("name") or "")
            low = name.lower()
            if requested and requested.strip() and requested.strip().lower() in low:
                candidates.append((0, i, name, "requested"))
                continue
            if any(s in low for s in PREFERRED_MIC_SUBSTRINGS):
                api = hostapis[d.get("hostapi", 0)]["name"] if hostapis else ""
                # Prefer WASAPI (we resample 48k->16k); allow override via JARVIS_DEVICE
                candidates.append((prefs.get(api, 9), i, name, api))
        candidates.sort()
        if candidates:
            return candidates[0][1]
    except Exception as e:
        print(f"[Jarvis] Mic device resolution failed: {e}")
    if requested:
        try:
            return requested  # let sounddevice validate
        except Exception:
            return None
    return None

# Local speech recognition (uses GPU + CPU together for speed)
WHISPER_MODEL_SIZE = os.environ.get("JARVIS_WHISPER_MODEL", "small")   # "tiny" fastest; "small" better EN/HI; "base"/"medium" slower
WHISPER_DEVICE = os.environ.get("JARVIS_WHISPER_DEVICE", "cuda")       # "cuda" or "cpu" (bat defaults to cpu)
WHISPER_COMPUTE = os.environ.get("JARVIS_WHISPER_COMPUTE", "float16")   # CUDA; auto-fallback handled below
WHISPER_CPU_THREADS = max(2, (os.cpu_count() or 4) // 2)
WHISPER_NUM_WORKERS = 1

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
    if any(k in nm for k in ['david', 'mark', 'male', 'guy', 'hemant', 'ravi']):
        _tts_voice_id = v.id
        break
if not _tts_voice_id:
    # fallback heuristic
    for v in voices:
        nm = ((getattr(v, 'name', '') or '') + ' ' + (getattr(v, 'id', '') or '')).lower()
        if 'zira' not in nm and 'female' not in nm:
            _tts_voice_id = v.id
            break
if _tts_voice_id:
    try:
        tts_engine.setProperty('voice', _tts_voice_id)
    except Exception:
        pass

# Local TTS only (fast, no network)

# faster-whisper for bilingual (auto) recognition
WHISPER_AVAILABLE = False
whisper_model = None
try:
    from faster_whisper import WhisperModel
    WHISPER_AVAILABLE = True
except Exception as e:
    print(f"[Jarvis] faster-whisper not available ({e}); bilingual auto mode will use Vosk fallback.")

try:
    import numpy as np
except Exception:
    np = None

tts_lock = threading.Lock()
TTS_SPEAKING_UNTIL = 0.0


def play_wake_beep():
    """Immediate audible feedback when wake word is detected."""
    if not WINSOUND_AVAILABLE:
        return
    try:
        winsound.Beep(880, 120)
    except Exception:
        pass


def wake_ack(lang=None):
    """Confirm wake word with beep + spoken response."""
    if lang is None:
        lang = "hi" if LANGUAGE_MODE == "hi" else "en"
    try:
        threading.Thread(target=play_wake_beep, daemon=True).start()
    except Exception:
        pass
    msg = "Haan, main sun rahi hoon." if lang == "hi" else "Yes, I'm listening."
    speak(msg, lang=lang, fast=True, block=False)

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


def speak(text, lang=None, fast=False, block=False):
    """Speak text. block=False by default so command loop never stalls on TTS."""
    global TTS_SPEAKING_UNTIL, tts_engine, _tts_voice_id
    if text is None:
        return
    text = str(text).strip()
    if not text or text in (".", ",", "!", "?", "..."):
        return
    if lang is None:
        lang = detect_response_lang(text) if LANGUAGE_MODE == "auto" else LANGUAGE_MODE
    print(f"[Jarvis] {text}")
    if not block:
        threading.Thread(target=lambda: speak(text, lang=lang, fast=fast, block=True), daemon=True).start()
        return
    # prevent wake/self-trigger while speaking
    TTS_SPEAKING_UNTIL = max(TTS_SPEAKING_UNTIL, time.time() + max(0.5, min(5.0, len(text) * 0.055 + 0.3)))
    acquired = tts_lock.acquire(timeout=4.0)
    if not acquired:
        print("[Jarvis] TTS busy; skipping spoken response.")
        TTS_SPEAKING_UNTIL = time.time() + 0.25
        return
    try:
        try:
            if fast:
                tts_engine.setProperty('rate', 210)
                tts_engine.say(text)
                tts_engine.runAndWait()
                tts_engine.setProperty('rate', 175)
            else:
                tts_engine.setProperty('rate', 190)
                tts_engine.say(text)
                tts_engine.runAndWait()
                tts_engine.setProperty('rate', 175)
        except Exception as e:
            print(f"[Jarvis] TTS error (continuing): {e}")
            try:
                tts_engine.stop()
            except Exception:
                pass
            try:
                tts_engine = pyttsx3.init()
                try:
                    tts_engine.setProperty('rate', 175)
                    if _tts_voice_id:
                        tts_engine.setProperty('voice', _tts_voice_id)
                except Exception:
                    pass
            except Exception:
                pass
    finally:
        TTS_SPEAKING_UNTIL = time.time() + 0.45
        try:
            tts_lock.release()
        except Exception:
            pass

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

# Tablet control via ADB (Samsung/Android over Wi-Fi or USB)

def _adb_cmd(*args, timeout=12):
    adb = TABLET_ADB_PATH
    if not adb or not os.path.exists(adb):
        return False, "ADB not found. Set JARVIS_TABLET_ADB or install Platform Tools."
    cmd = [adb]
    if TABLET_ADB_SERIAL:
        cmd += ["-s", TABLET_ADB_SERIAL]
    cmd += list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
        return r.returncode == 0, out
    except Exception as e:
        return False, str(e)


def _adb_shell(shell_cmd, timeout=12):
    return _adb_cmd("shell", shell_cmd, timeout=timeout)


def tablet_reconnect():
    adb = TABLET_ADB_PATH
    if not adb or not os.path.exists(adb):
        return False, "ADB not found"
    if not TABLET_ADB_SERIAL or ":" not in TABLET_ADB_SERIAL:
        return False, "No Wi-Fi tablet serial set (JARVIS_TABLET_SERIAL)"
    try:
        r = subprocess.run([adb, "connect", TABLET_ADB_SERIAL], capture_output=True, text=True, timeout=12)
        out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip().lower()
        ok = ("connected" in out) or ("already connected" in out)
        return ok, out
    except Exception as e:
        return False, str(e)


TABLET_APP_PACKAGES = {
    "youtube": "com.google.android.youtube",
    "yt": "com.google.android.youtube",
    "chrome": "com.android.chrome",
    "browser": "com.android.chrome",
    "settings": "com.android.settings",
    "spotify": "com.spotify.music",
    "gmail": "com.google.android.gm",
    "maps": "com.google.android.apps.maps",
    "camera": "com.sec.android.app.camera",
    "gallery": "com.sec.android.gallery3d",
    "photos": "com.google.android.apps.photos",
    "play store": "com.android.vending",
    "store": "com.android.vending",
    "whatsapp": "com.whatsapp",
    "facebook": "com.facebook.katana",
    "instagram": "com.instagram.android",
    "netflix": "com.netflix.mediaclient",
}

TABLET_APP_ACTIVITIES = {
    "com.google.android.youtube": "com.google.android.youtube/.app.honeycomb.Shell$HomeActivity",
    "com.android.chrome": "com.android.chrome/com.google.android.apps.chrome.Main",
}


def _tablet_launch_package(pkg, timeout=12):
    # 1) Known explicit activity (fast/reliable on tested devices)
    act = TABLET_APP_ACTIVITIES.get(pkg)
    if act:
        ok, out = _adb_shell(f"am start -n {act}", timeout=timeout)
        merged = (out or "").lower()
        if ok or "starting: intent" in merged or "warning: activity not started" in merged:
            return True, out
    # 2) Android 7+ explicit launcher resolve
    ok, out = _adb_shell(
        f"cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER {pkg}",
        timeout=timeout,
    )
    if ok and out:
        for line in out.splitlines():
            line = line.strip()
            if "/" in line and not line.startswith(("priority=", "Action:", "Category:", "No activity")) and " " not in line:
                ok2, out2 = _adb_shell(f"am start -n {line}", timeout=timeout)
                merged = ((out2 or "") + " " + line).lower()
                if ok2 or "starting: intent" in merged or "warning: activity not started" in merged:
                    return True, line
    # 3) monkey (works on many Samsung/older devices)
    ok, out = _adb_shell(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1", timeout=timeout)
    if ok and "No activities found" not in (out or ""):
        return True, out
    # 4) generic launcher intent
    ok, out = _adb_shell(
        f"am start -a android.intent.action.MAIN -c android.intent.category.LAUNCHER -p {pkg}",
        timeout=timeout,
    )
    merged = (out or "").lower()
    return ok or "starting: intent" in merged, out


def _tablet_find_package(query):
    q = (query or "").lower().strip()
    if not q:
        return None, None
    for name, pkg in TABLET_APP_PACKAGES.items():
        if name in q or q in name:
            return name, pkg
    try:
        ok, out = _adb_shell("pm list packages", timeout=12)
        if not ok or not out:
            return None, None
        tokens = [t for t in re.findall(r'[a-z0-9]+', q) if len(t) >= 3]
        if not tokens:
            return None, None
        best = None
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("package:"):
                continue
            pkg = line.split(":", 1)[1].strip().lower()
            if not pkg:
                continue
            score = sum(1 for t in tokens if t in pkg)
            if score <= 0:
                continue
            if best is None or score > best[0] or (score == best[0] and len(pkg) < len(best[1])):
                best = (score, pkg)
        if best and best[0] >= max(1, len(tokens) - 1):
            return q, best[1]
    except Exception:
        pass
    return None, None


def _tablet_close_package(pkg, timeout=12):
    if not pkg:
        return False, "No package"
    ok, out = _adb_shell(f"am force-stop {pkg}", timeout=timeout)
    return ok, out


def handle_tablet_command(t, response_lang="en"):
    raw = t or ""
    tl = raw.lower().replace("टैबलेट", "tablet").replace("टेबलेट", "tablet").replace("टैब", "tablet")
    # lightweight Hindi intent normalization for tablet commands
    repls = [
        (r"\b(par|pe|mein|me|ka|ki|ko)\b", " "),
        (r"\b(kholo|khol do|khol dena|khol|chalu karo|start karo|open karo)\b", " open "),
        (r"\b(band karo|band kar do|band kar|band|close karo)\b", " close "),
        (r"\b(chalao|chala do|play karo|bajao|suna do|sunao|lagao)\b", " play "),
        (r"\b(dhundho|dhoondho|khojo|search karo)\b", " search "),
        (r"\b(aawaz|awaz)\b", " volume "),
        (r"\b(badhao|badao|zyada|upar)\b", " up "),
        (r"\b(kam karo|kam|ghatao|neeche)\b", " down "),
        (r"\b(ghar)\b", " home "),
        (r"\b(wapas|peeche)\b", " back "),
        (r"\b(roko|ruk jao|pause)\b", " pause "),
        (r"\b(jagao)\b", " wake "),
        (r"\b(yutube|yutub)\b", " youtube "),
    ]
    for pat, rep in repls:
        tl = re.sub(pat, rep, tl)
    tl = re.sub(r"\s+", " ", tl).strip()

    tablet_words = ("tablet", "tab a", "galaxy tab", "samsung tab", "pad", "sm-t385", "smt385", "tablate", "tablit")
    if not any(w in tl for w in tablet_words):
        return False

    # Wake/unlock helpers
    if any(x in tl for x in ["wake", "jagao", "screen on"]):
        ok, out = _adb_shell("input keyevent 224")
        speak("Waking tablet" if ok else "Tablet command failed", lang=response_lang)
        return True
    if any(x in tl for x in ["unlock screen", "swipe up", "unlock"]):
        ok, out = _adb_shell("input keyevent 82; input swipe 360 1000 360 300 250")
        speak("Trying to unlock tablet" if ok else "Tablet command failed", lang=response_lang)
        return True

    # Navigation / power-like keys (safe subset)
    if "home" in tl:
        ok, _ = _adb_shell("input keyevent 3")
        speak("Tablet home" if ok else "Tablet command failed", lang=response_lang); return True
    if "back" in tl:
        ok, _ = _adb_shell("input keyevent 4")
        speak("Going back on tablet" if ok else "Tablet command failed", lang=response_lang); return True
    if "recent" in tl or "recents" in tl or "overview" in tl:
        ok, _ = _adb_shell("input keyevent 187")
        speak("Tablet recents" if ok else "Tablet command failed", lang=response_lang); return True
    if "lock tablet" in tl or "screen off" in tl:
        ok, _ = _adb_shell("input keyevent 223")
        speak("Turning tablet screen off" if ok else "Tablet command failed", lang=response_lang); return True

    # Media
    if any(x in tl for x in ["play pause", "play/pause", "pause", "resume", "play music", "pause music"]):
        ok, _ = _adb_shell("input keyevent 85")
        speak("Toggled playback on tablet" if ok else "Tablet command failed", lang=response_lang); return True
    if any(x in tl for x in ["next", "skip"]):
        ok, _ = _adb_shell("input keyevent 87")
        speak("Next track on tablet" if ok else "Tablet command failed", lang=response_lang); return True
    if any(x in tl for x in ["previous", "prev", "back song"]):
        ok, _ = _adb_shell("input keyevent 88")
        speak("Previous track on tablet" if ok else "Tablet command failed", lang=response_lang); return True

    # Volume
    if "mute" in tl or "unmute" in tl:
        ok, _ = _adb_shell("input keyevent 164")
        speak("Toggled mute on tablet" if ok else "Tablet command failed", lang=response_lang); return True
    if "volume up" in tl or "louder" in tl or "aawaz badhao" in tl:
        steps = 2
        m = re.search(r'(\d+)', tl)
        if m:
            n = int(m.group(1))
            steps = max(1, min(15, n if n <= 15 else n // 10))
        ok = True
        for _ in range(steps):
            ok, _ = _adb_shell("input keyevent 24")
            if not ok:
                break
        speak("Tablet volume up" if ok else "Tablet command failed", lang=response_lang); return True
    if "volume down" in tl or "quieter" in tl or "aawaz kam" in tl:
        steps = 2
        m = re.search(r'(\d+)', tl)
        if m:
            n = int(m.group(1))
            steps = max(1, min(15, n if n <= 15 else n // 10))
        ok = True
        for _ in range(steps):
            ok, _ = _adb_shell("input keyevent 25")
            if not ok:
                break
        speak("Tablet volume down" if ok else "Tablet command failed", lang=response_lang); return True

    # Connection/status
    if "connect" in tl or "reconnect" in tl:
        ok, out = tablet_reconnect()
        speak("Tablet connected" if ok else "Tablet reconnect failed. Check Wi-Fi and ADB.", lang=response_lang)
        if out:
            print(f"[Tablet ADB] {out}")
        return True
    if "status" in tl or "connected" in tl:
        ok, out = _adb_cmd("get-state", timeout=8)
        model_ok, model = _adb_shell("getprop ro.product.model", timeout=8)
        speak(f"Tablet {out or 'unknown'} {('('+model+')') if model_ok and model else ''}", lang=response_lang)
        return True

    # App close (voice): "close <app> on tablet", "tablet close youtube", "force stop <app> on tablet"
    close_words = ("close", "stop app", "force stop", "quit app", "exit app", "kill", "band karo", "band kar")
    explicit_close = any(k in tl for k in close_words) or (re.search(r'\bquit\b', tl) and "youtube" not in tl)
    if explicit_close:
        q = tl
        for kw in tablet_words + ("close", "stop", "force", "quit", "exit", "kill", "app", "application", "band", "karo", "kar", "on", "in", "the", "please", "do", "ho"):
            q = re.sub(rf'\b{re.escape(kw)}\b', ' ', q)
        q = re.sub(r'\s+', ' ', q).strip()
        if q:
            name, pkg = _tablet_find_package(q)
            if pkg:
                ok, out = _tablet_close_package(pkg)
                speak(f"Closed {name or q} on tablet" if ok else f"Tablet couldn't close {name or q}", lang=response_lang)
                if not ok and out:
                    print(f"[Tablet ADB] {out}")
                return True
            speak(f"I couldn't find {q} on the tablet to close.", lang=response_lang)
            return True
        # no app name -> send Home (minimize current)
        ok, out = _adb_shell("input keyevent 3")
        speak("Sent tablet home" if ok else "Tablet command failed", lang=response_lang)
        return True

    # Generic open URL
    m = re.search(r'(https?://\S+|www\.\S+)', tl)
    if m and "open" in tl:
        url = m.group(1)
        if url.startswith("www."):
            url = "https://" + url
        ok, _ = _adb_shell(f'am start -a android.intent.action.VIEW -d "{url}"')
        speak("Opening link on tablet" if ok else "Tablet command failed", lang=response_lang); return True

    # YouTube (app search/open + optional autoplay)
    if "youtube" in tl or "yt" in tl:
        wants_play = any(k in tl for k in ("play", "chalao", "bajao", "suna", "sunao", "lagao"))
        wants_search = any(k in tl for k in ("search", "find", "dhundho", "khojo", "dikhao"))
        q = tl
        for kw in tablet_words + (
            "open", "launch", "start", "on", "in", "app", "play", "search", "find", "youtube", "yt",
            "tablet", "please", "kholo", "khol", "do", "chalao", "bajao", "suna", "sunao", "lagao",
            "video", "song", "gaana", "gana", "music",
        ):
            q = re.sub(rf"\b{re.escape(kw)}\b", " ", q)
        q = re.sub(r"\s+", " ", q).strip(" ,.-")

        if wants_play and q and q not in ("on", "the", "app"):
            return bool(play_video_on_youtube(q, response_lang=response_lang, tablet=True))

        if wants_search and q and q not in ("on", "the", "app"):
            url = f"https://www.youtube.com/results?search_query={requests.utils.quote(q)}"
            ok, _ = _adb_shell(f'am start -a android.intent.action.VIEW -d "{url}" com.google.android.youtube')
            if not ok:
                ok, _ = _adb_shell(f'am start -a android.intent.action.VIEW -d "{url}"')
            speak(f"Searching {q} on tablet YouTube" if ok else "Tablet command failed", lang=response_lang)
            return True

        ok, out = _tablet_launch_package(TABLET_APP_PACKAGES["youtube"])
        if not ok:
            ok, out = _adb_shell('am start -a android.intent.action.VIEW -d "https://www.youtube.com/"')
        speak("Opening YouTube on tablet" if ok else "Tablet command failed (YouTube app not found)", lang=response_lang)
        if not ok and out:
            print(f"[Tablet ADB] {out}")
        return True

    # Default for tablet: "play/chalao ... on tablet" (or any play query) -> YouTube on tablet
    wants_play_any = any(k in tl for k in (" play ", "chalao", "bajao", "suna", "sunao", "lagao"))
    tl_padded = f" {tl} "
    wants_play_any = wants_play_any or " play " in tl_padded
    if wants_play_any and not any(k in tl for k in ("open", "launch", "start", "close", "stop app", "force stop", "search", "find")):
        q = tl
        for kw in tablet_words + ("play", "chalao", "chalao", "bajao", "suna", "sunao", "lagao", "on", "in", "video", "song", "gaana", "gana", "music", "please", "youtube", "yt"):
            q = re.sub(rf"\b{re.escape(kw)}\b", " ", q)
        q = re.sub(r"\s+", " ", q).strip(" ,.-")
        if q and q not in ("pause", "resume"):
            return bool(play_video_on_youtube(q, response_lang=response_lang, tablet=True))

    # App launch (voice): "open <app> on tablet", "launch <app> on tablet"
    if any(k in tl for k in ("open", "launch", "start", "kholo", "khol", "chalu")):
        q = tl
        for kw in tablet_words + ("open", "launch", "start", "on", "in", "app", "application", "the", "please", "kholo", "khol", "do", "chalu", "karo"):
            q = re.sub(rf'\b{re.escape(kw)}\b', ' ', q)
        q = re.sub(r'\s+', ' ', q).strip()
        if q:
            name, pkg = _tablet_find_package(q)
            if pkg:
                ok, out = _tablet_launch_package(pkg)
                speak(f"Opening {name or q} on tablet" if ok else f"Tablet couldn't open {name or q}", lang=response_lang)
                if not ok and out:
                    print(f"[Tablet ADB] {out}")
                return True
            # Not an installed app name -> if user intent looks like media, try YouTube on tablet
            if len(q) >= 2:
                return bool(play_video_on_youtube(q, response_lang=response_lang, tablet=True))
            speak(f"I couldn't find {q} on the tablet. Try the app name as installed, or say 'tablet open YouTube'.", lang=response_lang)
            return True

    # Generic play-on-tablet fallback (when "tablet" present but no explicit YouTube/app match)
    q = tl
    for kw in tablet_words + ("on", "in", "please", "the"):
        q = re.sub(rf"\b{re.escape(kw)}\b", " ", q)
    q = re.sub(r"\s+", " ", q).strip(" ,.-")
    if q and q not in ("status", "connect", "reconnect", "home", "back"):
        return bool(play_video_on_youtube(q, response_lang=response_lang, tablet=True))

    speak("Tablet command not recognized. Try: tablet open/close YouTube, tablet open Chrome, tablet home, tablet play pause, tablet volume up.", lang=response_lang)
    return True

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
    """Detect wake word/variants in ASR text."""
    if not text:
        return False, ""
    raw = text.strip()
    if len(raw) < 3:
        return False, ""
    tl = raw.lower()
    norm = correct_wake_text(raw)

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

    if re.search(r'(^|\s)jarvis(\s|$|[,.!?:;])', f" {norm} "):
        after = re.sub(r'^.*?\bjarvis\b\s*', '', norm, count=1, flags=re.I).strip(" ,.!?:;-")
        if after == norm:
            after = ""
        if len(norm.split()) <= 8 or after or norm.strip() == "jarvis":
            return True, after

    joined = norm.lower()
    m = re.search(r'\b(j+a+r+v+i+s+|j+a+r+v+a+s+|j+a+r+v+e+z+|d+r+i+v+e+r+s?)\b', joined)
    if m and (m.start() <= 12 or len(joined.split()) <= 6):
        after = joined[m.end():].strip(" ,.!?:;-")
        return True, after

    # fuzzy: clear 'jarv*' / common mishearings (avoid matching java/javascript/etc.)
    wake_like = {'driver', 'drivers', 'dryver', 'draver', 'doris', 'dorris', 'dollars', 'dollar', 'readers', 'reader', 'davis', 'service'}
    for tok in re.findall(r'[a-z]+', joined):
        if (tok.startswith('jarv') and 4 <= len(tok) <= 8) or tok in wake_like:
            after = joined.replace(tok, ' ', 1).strip(" ,.!?:;-")
            return True, after

    compact = re.sub(r'[^a-z]', '', tl)
    if 4 <= len(compact) <= 8:
        for cand in ("jarvis", "jarvas", "jarvez", "jervis", "driver", "drivers", "doris", "dorris", "dollars", "dollar", "readers", "reader", "davis", "service"):
            if compact == cand:
                return True, ""
            if abs(len(compact)-len(cand)) <= 1:
                mism = sum(1 for a,b in zip(compact,cand) if a!=b) + abs(len(compact)-len(cand))
                if mism <= 1 and compact[0] in ('j', 'd', 'r', 's'):
                    return True, ""
    return False, ""

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


def _youtube_search_top_video_id(query, timeout=8):
    """Best-effort: resolve a YouTube query to a top video ID for direct autoplay."""
    q = (query or "").strip()
    if not q:
        return None
    try:
        headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}
        r = requests.get("https://www.youtube.com/results", params={"search_query": q}, headers=headers, timeout=timeout)
        if r.status_code != 200 or not r.text:
            return None
        m = re.search(r'var ytInitialData\s*=\s*({.*?});</script>', r.text, flags=re.S)
        if not m:
            m = re.search(r'"ytInitialData"\s*:\s*({.*?})\s*;</script>', r.text, flags=re.S)
        if not m:
            return None
        data = json.loads(m.group(1))

        def find_video_id(d):
            if isinstance(d, dict):
                vr = d.get("videoRenderer")
                if isinstance(vr, dict):
                    vid = vr.get("videoId")
                    if isinstance(vid, str) and len(vid) >= 8:
                        return vid
                for v in d.values():
                    vid = find_video_id(v)
                    if vid:
                        return vid
            elif isinstance(d, list):
                for v in d:
                    vid = find_video_id(v)
                    if vid:
                        return vid
            return None

        return find_video_id(data)
    except Exception:
        return None


def play_video_on_youtube(query=None, response_lang="en", tablet=False):
    """Play a YouTube video: resolve top result and open watch URL for autoplay."""
    q = (query or "").strip()
    if not q:
        url = "https://www.youtube.com/"
        if tablet:
            ok, _ = _adb_shell(f'am start -a android.intent.action.VIEW -d "{url}" com.google.android.youtube')
            if not ok:
                ok, _ = _adb_shell(f'am start -a android.intent.action.VIEW -d "{url}"')
            speak("Opening YouTube on tablet" if ok else "Tablet command failed", lang=response_lang)
            return bool(ok)
        open_in_chrome(url, response_lang=response_lang)
        speak(_pick({"en": "Opening YouTube.", "hi": "YouTube khol raha hoon."}, response_lang), lang=response_lang, block=False)
        return True

    vid = _youtube_search_top_video_id(q)
    if vid:
        url = f"https://www.youtube.com/watch?v={vid}&autoplay=1"
        spoke = f"Playing {q} on YouTube"
    else:
        url = f"https://www.youtube.com/results?search_query={requests.utils.quote(q)}"
        spoke = f"Searching {q} on YouTube"

    if tablet:
        ok, _ = _adb_shell(f'am start -a android.intent.action.VIEW -d "{url}" com.google.android.youtube')
        if not ok:
            ok, _ = _adb_shell(f'am start -a android.intent.action.VIEW -d "{url}"')
        speak((spoke + " on tablet") if ok else "Tablet command failed", lang=response_lang)
        return bool(ok)

    launched = open_in_chrome(url, response_lang=response_lang)
    if not launched:
        speak(_pick({"en": "I couldn't open Chrome for YouTube.", "hi": "Main YouTube ke liye Chrome nahi khol paya."}, response_lang), lang=response_lang)
        return False
    speak(_pick({"en": f"{spoke}.", "hi": f"YouTube par {q} chala raha hoon." if vid else f"YouTube par {q} dhoondh raha hoon."}, response_lang), lang=response_lang, block=False)
    if not vid:
        threading.Thread(target=lambda: _desktop_try_autoplay_youtube(5.2), daemon=True).start()
    return True


def _desktop_try_autoplay_youtube(delay=4.5):
    """Disabled: keystroke autoplay interferes with Chrome/YouTube UI (can type in search)."""
    return None


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

        threading.Thread(target=lambda: _desktop_try_autoplay_youtube(4.8), daemon=True).start()
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
        (r'\b(tablet|tab\s*let|tablate|tablit|टैबलेट|टेबलेट)\s*(par|pe|mein|me|ka|ki)?\s*(youtube|yt)\s*(kholo|khol do|khol|chalu karo|open karo)\b', 'tablet open youtube'),
        (r'\b(youtube|yt)\s*(tablet|tab\s*let|tablate|tablit|टैबलेट|टेबलेट)\s*(par|pe|mein|me)?\s*(kholo|khol do|khol|chalu karo)\b', 'tablet open youtube'),
        (r'\b(tablet|tab\s*let|tablate|tablit|टैबलेट|टेबलेट)\s*(par|pe|mein|me|ka|ki)?\s*(.+?)\s*(chalao|play karo|bajao|suna do|lagao)\s*(youtube|yt)?\b', r'tablet play \3 on youtube'),
        (r'\b(tablet|tab\s*let|tablate|tablit|टैबलेट|टेबलेट)\s*(par|pe|mein|me)?\s*(.+?)\s*(youtube|yt)\s*(par|pe)?\s*(chalao|play karo|bajao)\b', r'tablet play \3 on youtube'),
        (r'\b(tablet|tab\s*let|tablate|tablit|टैबलेट|टेबलेट)\s*(par|pe|mein|me)?\s*(.+?)\s*(kholo|khol do|khol|chalu karo|start karo)\b', r'tablet open \3'),
        (r'\b(.+?)\s*(tablet|tab\s*let|tablate|tablit|टैबलेट|टेबलेट)\s*(par|pe|mein|me)?\s*(kholo|khol do|khol|chalu karo)\b', r'tablet open \1'),
        (r'\b(tablet|tab\s*let|tablate|tablit|टैबलेट|टेबलेट)\s*(par|pe|mein|me)?\s*(.+?)\s*(band karo|band kar do|band kar|close karo|hatao)\b', r'tablet close \3'),
        (r'\b(.+?)\s*(tablet|tab\s*let|tablate|tablit|टैबलेट|टेबलेट)\s*(par|pe|mein|me)?\s*(band karo|band kar do|band kar|close karo)\b', r'tablet close \1'),
        (r'\b(tablet|tab\s*let|tablate|tablit|टैबलेट|टेबलेट)\s*(ghar|home)\b', 'tablet home'),
        (r'\b(tablet|tab\s*let|tablate|tablit|टैबलेट|टेबलेट)\s*(wapas|peeche|back)\b', 'tablet back'),
        (r'\b(tablet|tab\s*let|tablate|tablit|टैबलेट|टेबलेट)\s*(aawaz|volume)\s*(badhao|badao|zyada|upar)\b', 'tablet volume up'),
        (r'\b(tablet|tab\s*let|tablate|tablit|टैबलेट|टेबलेट)\s*(aawaz|volume)\s*(kam|ghatao|neeche)\b', 'tablet volume down'),
        (r'\b(tablet|tab\s*let|tablate|tablit|टैबलेट|टेबलेट)\s*(roko|pause|ruk jao)\b', 'tablet play pause'),
        (r'\b(tablet|tab\s*let|tablate|tablit|टैबलेट|टेबलेट)\s*(resume)\b', 'tablet play pause'),
        (r'\b(tablet|tab\s*let|tablate|tablit|टैबलेट|टेबलेट)\s*(screen|display)?\s*(band|off)\b', 'tablet screen off'),
        (r'\b(tablet|tab\s*let|tablate|tablit|टैबलेट|टेबलेट)\s*(jagao|wake|screen on)\b', 'tablet wake'),
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

    # Tablet control (ADB) - Galaxy Tab A 2017 on same Wi-Fi (must run before PC YouTube/app handlers)
    if any(x in t for x in ["tablet", "tab a", "galaxy tab", "samsung tab", "sm-t385", "tablate", "tablit", "टैबलेट", "टेबलेट"]):
        if handle_tablet_command(t, response_lang=response_lang):
            return

    # Direct YouTube / YouTube Music / YouTube Studio commands (always launch in Chrome)
    if "youtube studio" in t or "yt studio" in t or "studio.youtube" in t:
        open_in_chrome("https://studio.youtube.com/", response_lang=response_lang)
        speak(_pick({"en": "Opening YouTube Studio in Chrome.",
                     "hi": "Chrome mein YouTube Studio khol raha hoon."}, response_lang), lang=response_lang, block=False)
        return

    # "play/search on youtube" (video/site) — on tablet when requested, else Chrome on PC
    if ("on youtube" in t and "youtube music" not in t and "yt music" not in t) or (any(x in t for x in ["play", "chalao", "bajao"]) and "youtube" in t and "youtube music" not in t and "yt music" not in t):
        q = t
        for kw in ["tablet", "tab", "open", "play", "search", "on youtube", "youtube", "kholo", "chalao", "bajao", "dhundho", "search karo", "please", "video", "song", "gaana", "gana"]:
            q = re.sub(rf'\b{re.escape(kw)}\b', ' ', q)
        q = re.sub(r'\s+', ' ', q).strip(" ,.-")
        wants_play = any(x in t for x in ["play", "chalao", "bajao"])
        if any(x in t for x in ["tablet", "tab a", "galaxy tab", "samsung tab", "sm-t385"]):
            if wants_play and q:
                play_video_on_youtube(q, response_lang=response_lang, tablet=True)
                return
            if q:
                url = f"https://www.youtube.com/results?search_query={requests.utils.quote(q)}"
                ok, _ = _adb_shell(f'am start -a android.intent.action.VIEW -d "{url}" com.google.android.youtube')
                if not ok:
                    ok, _ = _adb_shell(f'am start -a android.intent.action.VIEW -d "{url}"')
                speak("Searching YouTube on tablet" if ok else "Tablet command failed", lang=response_lang)
                return
            play_video_on_youtube(None, response_lang=response_lang, tablet=True)
            return
        if not q:
            open_in_chrome("https://www.youtube.com/", response_lang=response_lang)
            speak(_pick({"en": "Opening YouTube in Chrome.", "hi": "Chrome mein YouTube khol raha hoon."}, response_lang), lang=response_lang, block=False)
            return
        if wants_play:
            play_video_on_youtube(q, response_lang=response_lang, tablet=False)
            return
        url = f"https://www.youtube.com/results?search_query={requests.utils.quote(q)}"
        open_in_chrome(url, response_lang=response_lang)
        speak(_pick({"en": f"Searching {q} on YouTube.", "hi": f"YouTube par {q} dhoondh raha hoon."}, response_lang), lang=response_lang, block=False)
        return

    # Music commands (YouTube / YouTube Music) - high priority, always Chrome
    _music_cmd = any(x in t for x in ["play music", "play song", "music chalao", "gaana chalao", "gana chalao", "bajao", "kuch gaana", "koi gaana"])
    _yt_music = ("youtube music" in t) or ("yt music" in t) or ("on youtube music" in t) or ("music.youtube" in t)
    if _yt_music or (_music_cmd and t.strip() in ("play music", "play some music", "gaana chalao", "gana chalao", "music chalao", "kuch gaana chalao", "koi gaana chalao")) or ("play" in t and "youtube music" in t):
        q = t
        for kw in ["jarvis", "maalkeen", "malkeen", "à¤œà¤¾à¤°à¥à¤µà¤¿à¤¸", "play", "on youtube music", "on youtube", "youtube music", "yt music", "music chalao", "gaana chalao", "gana chalao", "kuch", "koi", "gaana", "gana", "song", "music", "chalao", "bajao", "please", "mujhe", "mere liye"]:
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
        speak(_pick({"en": "Opening YouTube in Chrome.", "hi": "Chrome mein YouTube khol raha hoon."}, response_lang), lang=response_lang, block=False)
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
    if any(x in t for x in ["stop listening", "go to sleep", "sleep mode", "exit", "quit jarvis", "shutdown jarvis", "so jao jarvis", "jarvis so jao", "chup raho"]):
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
            speak(_pick({"en": "Opening YouTube Studio in Chrome.", "hi": "Chrome mein YouTube Studio khol raha hoon."}, response_lang), lang=response_lang, block=False)
            return
            play_music_on_youtube(None, response_lang=response_lang)
            return
        if target.strip() in ("youtube", "youtube.com", "www.youtube.com") or "youtube.com" in target:
            url = target if target.startswith("http") else ("https://www.youtube.com/" if target.strip() in ("youtube", "youtube.com", "www.youtube.com") else "https://" + target.replace(" ", ""))
            open_in_chrome(url, response_lang=response_lang)
            speak(_pick({"en": "Opening YouTube in Chrome.", "hi": "Chrome mein YouTube khol raha hoon."}, response_lang), lang=response_lang, block=False)
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

    # LLM removed for speed/local-only mode
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
    print(f"[Jarvis] Downloading {lang} speech model from {model_url} ...")
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
        print(f"[Jarvis] {lang} model downloaded.")
        return model_path
    except Exception as e:
        print(f"[Jarvis] Failed to download {lang} model: {e}")
        speak(f"I couldn't download the {lang} speech model. Check your internet.", lang="en")
        return None

def ensure_whisper_model():
    """Load faster-whisper model for bilingual (auto) mode. Uses GPU+CPU when available; probes CUDA."""
    global whisper_model, WHISPER_AVAILABLE
    # Local Whisper only
    if whisper_model is not None:
        return True
    if not WHISPER_AVAILABLE:
        return False
    try:
        from faster_whisper import WhisperModel
        preferred = (os.environ.get("JARVIS_WHISPER_DEVICE") or WHISPER_DEVICE or "cuda").lower()
        cpu_threads = globals().get("WHISPER_CPU_THREADS") or max(2, (os.cpu_count() or 4) // 2)
        num_workers = globals().get("WHISPER_NUM_WORKERS") or 1

        attempts = []
        if preferred in ("cuda", "gpu"):
            attempts = [("cuda", WHISPER_COMPUTE), ("cuda", "int8_float16"), ("cuda", "int8")]
        attempts.append(("cpu", "int8"))
        last_err = None
        for dev, ctype in attempts:
            try:
                print(f"[Jarvis] Loading Whisper '{WHISPER_MODEL_SIZE}' on {dev} (cpu_threads={cpu_threads})...")
                speak("Loading speech model. Please wait a moment.", lang="en", fast=True)
                kwargs = {"device": dev, "compute_type": ctype, "cpu_threads": cpu_threads, "num_workers": num_workers}
                whisper_model = WhisperModel(WHISPER_MODEL_SIZE, **kwargs)
                # Probe CUDA/cuBLAS early: model can load but fail on transcribe (e.g. missing cublas64_12.dll).
                try:
                    import numpy as _np
                    _probe = _np.zeros(max(1600, SAMPLE_RATE // 2), dtype=_np.float32)
                    _segs, _ = whisper_model.transcribe(_probe, beam_size=1, vad_filter=False, language="en", without_timestamps=True)
                    for _s in _segs:
                        break
                except Exception as probe_err:
                    pe = str(probe_err).lower()
                    if dev == "cuda" or "cublas" in pe or "cudnn" in pe or "cuda" in pe or "library" in pe or "dll" in pe:
                        print(f"[Jarvis] Whisper {dev} transcribe probe failed ({probe_err}); skipping CUDA variants...")
                        whisper_model = None
                        last_err = probe_err
                        # don't retry other CUDA compute types when runtime libs are missing/broken
                        attempts = [a for a in attempts if a[0] != "cuda"]
                        continue
                    raise
                mode = "GPU+CPU" if dev == "cuda" else "CPU"
                print(f"[Jarvis] Whisper loaded on {mode} ({ctype}, cpu_threads={cpu_threads}).")
                if dev == "cpu" and preferred in ("cuda", "gpu"):
                    print("[Jarvis] CUDA Whisper unavailable on this system; using fast local CPU mode.")
                return True
            except Exception as e:
                last_err = e
                print(f"[Jarvis] Whisper load failed on {dev}/{ctype}: {e}")
                whisper_model = None
        if last_err:
            print(f"[Jarvis] Failed to load Whisper: {last_err}")
        WHISPER_AVAILABLE = False
        whisper_model = None
        return False
    except Exception as e:
        print(f"[Jarvis] Failed to load Whisper: {e}")
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
    def _trim_speech(audio_np, sr=SAMPLE_RATE):
        """Trim long leading/trailing low-energy regions around likely speech."""
        try:
            if audio_np is None or getattr(audio_np, "size", 0) < sr // 4:
                return audio_np
            a = audio_np.astype(np.float32)
            frame = max(160, sr // 50)  # 20ms
            n = (a.size // frame) * frame
            if n < frame * 4:
                return a
            frames = a[:n].reshape(-1, frame)
            rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-9)
            amb = float(np.median(rms))
            thr = max(0.012, amb * 1.8, float(np.percentile(rms, 70)) * 0.8)
            idx = np.where(rms > thr)[0]
            if idx.size == 0:
                return a
            pad = 4  # ~80ms
            start = max(0, (int(idx[0]) - pad) * frame)
            end = min(a.size, (int(idx[-1]) + pad + 1) * frame)
            if end - start < sr // 4:
                return a
            return a[start:end]
        except Exception:
            return audio_np

    def _normalize_for_recognition(audio_np):
        """Light normalization for human-like recognition; avoids clipping/noise boost on silence."""
        try:
            if audio_np is None or getattr(audio_np, "size", 0) == 0:
                return audio_np
            a = _trim_speech(audio_np.astype(np.float32))
            peak = float(np.max(np.abs(a))) if a.size else 0.0
            if peak < 0.010:
                return a  # too quiet/noise floor
            # normalize speech peaks to a consistent level for Whisper
            target = 0.80
            gain = target / peak
            gain = max(0.8, min(14.0, gain))
            return np.clip(a * gain, -1.0, 1.0)
        except Exception:
            return audio_np

    def _do_transcribe(vad):
        audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        # Skip very quiet/short audio
        if audio.size < SAMPLE_RATE * 0.20:
            return "", None
        rms = float(np.sqrt(np.mean(audio * audio))) if audio.size else 0.0
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if rms < 0.008 and peak < 0.03:
            return "", None
        audio = _normalize_for_recognition(audio)
        prompt = "Jarvis assistant commands. Open YouTube. Open Chrome. Open Notepad. Play music. Open I drive. Go to downloads. Volume up. Search for cats on YouTube. Jarvis, kholo. Gaana chalao."
        # CPU: VAD often unavailable (onnxruntime) — try no-VAD first for reliability
        if vad:
            attempts = [(False, "en"), (False, "hi"), (True, None), (False, None)]
        else:
            attempts = [(False, "en"), (False, "hi"), (False, None)]
        best_text, best_lang = "", None
        for use_vad, lang in attempts:
            try:
                segments, info = whisper_model.transcribe(
                    audio,
                    beam_size=5,
                    best_of=1,
                    patience=1.0,
                    vad_filter=bool(use_vad),
                    vad_parameters=dict(min_silence_duration_ms=300, speech_pad_ms=150, threshold=0.35) if use_vad else None,
                    language=lang,
                    task="transcribe",
                    condition_on_previous_text=False,
                    no_speech_threshold=0.72,
                    compression_ratio_threshold=2.6,
                    log_prob_threshold=-1.4,
                    initial_prompt=prompt,
                    temperature=[0.0, 0.2, 0.4],
                    without_timestamps=True,
                )
                texts = []
                for seg in segments:
                    st = (getattr(seg, "text", "") or "").strip()
                    if not st:
                        continue
                    try:
                        nsp = getattr(seg, "no_speech_prob", 0) or 0
                        alp = getattr(seg, "avg_logprob", None)
                        if nsp > 0.88:
                            continue
                        if alp is not None and alp < -1.6 and nsp > 0.5:
                            continue
                    except Exception:
                        pass
                    texts.append(st)
                    if len(" ".join(texts)) > 280:
                        break
                text = " ".join(texts).strip()
                text = _dedupe_repeated_text(text)
                got_lang = getattr(info, "language", None) or lang
                # strip common hallucinations
                if text:
                    lowt = text.lower()
                    if any(h in lowt for h in ["thank you for watching", "subscribe", "like and subscribe", "thanks for watching", "字幕", "amara.org"]):
                        text = ""
                if text and not best_text:
                    best_text, best_lang = text, got_lang
                # accept plausible transcript
                if len(text) >= 2 and re.search(r'[a-zA-Z\u0900-\u097F]', text):
                    return text, got_lang
            except Exception as e:
                if "onnxruntime" in str(e).lower() or "vad" in str(e).lower():
                    continue
                raise
        if DEBUG_AUDIO and best_text:
            print(f"[Whisper debug] best='{best_text}' lang={best_lang}")
        return best_text, best_lang
    try:
        try:
            return _do_transcribe(True)
        except Exception as vad_err:
            if "onnxruntime" in str(vad_err).lower() or "vad" in str(vad_err).lower():
                return _do_transcribe(False)
            raise
    except Exception as e:
        # If runtime error, retry with CUDA fallbacks then CPU (keeps CPU threads)
        err = str(e)
        print(f"[Whisper] Transcribe error: {err}")
        try:
            from faster_whisper import WhisperModel
            cpu_threads = globals().get("WHISPER_CPU_THREADS") or max(2, (os.cpu_count() or 4) // 2)
            num_workers = globals().get("WHISPER_NUM_WORKERS") or 1
            for dev, ctype in [("cuda","float16"),("cuda","int8_float16"),("cuda","int8"),("cpu","int8")]:
                try:
                    whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device=dev, compute_type=ctype, cpu_threads=cpu_threads, num_workers=num_workers)
                    mode = "GPU+CPU" if dev == "cuda" else "CPU"
                    print(f"[Jarvis] Whisper reloaded on {mode} ({ctype}).")
                    try:
                        return _do_transcribe(True)
                    except Exception:
                        return _do_transcribe(False)
                except Exception:
                    continue
        except Exception as e2:
            print(f"[Whisper] Recovery failed: {e2}")
        return "", None

def main():
    global LANGUAGE_MODE
    print("=" * 50)
    print("  JARVIS - Voice Assistant for Windows")
    print("=" * 50)
    print(f"Wake word: '{WAKE_WORD}' | Mode: {LANGUAGE_MODE} (English + Hindi supported)")
    print("Say 'Jarvis, stop listening' or 'Jarvis, go to sleep' to pause.")
    print("In auto mode you can mix English and Hindi in the same command.")
    print("Say 'Jarvis, switch to auto / hindi / english' to change mode.")
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
        recognizer = vosk.KaldiRecognizer(model, SAMPLE_RATE)
        recognizer.SetWords(True)
        recognizer.SetPartialWords(True)

    global DEVICE
    device_info = "default system microphone"
    try:
        resolved = resolve_input_device()
        if resolved is not None:
            DEVICE = resolved
        if DEVICE is not None:
            d = sd.query_devices(DEVICE, 'input')
            api = sd.query_hostapis(d.get('hostapi', 0)).get('name', '')
            device_info = f"{d.get('name', DEVICE)}" + (f" [{api}]" if api else "") + f" (id {DEVICE})"
        else:
            d = sd.query_devices(kind='input')
            device_info = f"default: {d.get('name', 'system microphone')}"
    except Exception as e:
        print(f"[Jarvis] Mic info unavailable: {e}")
        print(f"[Jarvis] Using microphone: {device_info}")
        # periodic silence hint handled via calibration/wake debug; suggest stable endpoints if ambiguous
        if DEVICE in (13,) or 'DirectSound' in device_info:
            print("[Jarvis] Tip: try set JARVIS_DEVICE=48 (WDM-KS) or set JARVIS_DEVICE=31 (WASAPI, resampled) if input is silent.")
        elif DEVICE in (1,) or 'MME' in device_info:
            print("[Jarvis] Tip: if input is silent, try set JARVIS_DEVICE=48 (WDM-KS) or set JARVIS_DEVICE=31 (WASAPI, resampled).")
        elif DEVICE in (31,) or 'WASAPI' in device_info:
            print("[Jarvis] Tip: if input is silent, try set JARVIS_DEVICE=48 (WDM-KS, USB PnP) or set JARVIS_DEVICE=2 (webcam).")

    if use_whisper:
        speak(f"{ASSISTANT_NAME} online. Using {device_info.split(' [')[0]}.", lang="en")
    else:
        speak(f"{ASSISTANT_NAME} online. Say {ASSISTANT_NAME}.", lang=("en" if LANGUAGE_MODE != "hi" else "hi"))

    listening_for_command = False
    command_buffer = ""
    command_audio = bytearray()
    last_wake_time = 0
    last_speech_time = 0
    cmd_started_at = 0.0
    WAKE_TIMEOUT = 9
    SILENCE_AFTER_SPEECH = 1.45 if use_whisper else 0.7
    MIN_COMMAND_CHARS = 2
    MAX_CMD_SECONDS = 10
    MIN_LISTEN_GRACE = 2.4 if use_whisper else 0.9  # give user time after "Yes, I'm listening"
    CMD_ACTIVE_RMS = 80  # recalibrated; count as speech activity while capturing command
    ACK_SUPPRESS_SEC = 0.9  # ignore TTS echo right after wake ack

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
                # Keep grammar disabled for wake detection: constrained grammar can miss accent variants.
        except Exception as e:
            print(f"[Jarvis] Wake detector load failed: {e}")

    def handle_mode_switch(check_text):
        nonlocal listening_for_command, command_buffer, command_audio, last_speech_time, cmd_started_at, prebuffer
        global LANGUAGE_MODE
        tl = check_text.lower()
        if "auto mode" in tl or "bilingual" in tl or "hinglish" in tl or "dono" in tl or ("both" in tl and ("hindi" in tl or "english" in tl)):
            LANGUAGE_MODE = "auto"
            speak("Auto mode on.", lang="en")
            listening_for_command = False; command_buffer = ""; command_audio = bytearray(); prebuffer = bytearray(); last_speech_time = 0; cmd_started_at = 0.0
            reload_for_mode()
            return True
        if "hindi mode" in tl or ("switch" in tl and "hindi" in tl) or "bol hindi" in tl or "hindi mein bolo" in tl or "sirf hindi" in tl:
            LANGUAGE_MODE = "hi"
            speak("Hindi mode mein switch kar raha hoon.", lang="hi")
            listening_for_command = False; command_buffer = ""; command_audio = bytearray(); prebuffer = bytearray(); last_speech_time = 0; cmd_started_at = 0.0
            reload_for_mode()
            return True
        if "english mode" in tl or ("switch" in tl and "english" in tl) or "speak english" in tl or "angrezi mein bolo" in tl or "only english" in tl:
            LANGUAGE_MODE = "en"
            speak("Switching to English mode.", lang="en")
            listening_for_command = False; command_buffer = ""; command_audio = bytearray(); prebuffer = bytearray(); last_speech_time = 0; cmd_started_at = 0.0
            reload_for_mode()
            return True
        return False

    def finalize_whisper_command():
        nonlocal listening_for_command, command_buffer, command_audio, last_speech_time, cmd_started_at, prebuffer
        audio_data = bytes(command_audio)
        command_audio = bytearray()
        command_buffer = ""
        prebuffer = bytearray()  # avoid feeding stale audio into next command
        listening_for_command = False
        last_speech_time = 0
        cmd_started_at = 0.0
        # need enough audio to avoid hallucinated/noise transcripts
        if len(audio_data) < int(SAMPLE_RATE * 2 * 0.45):
            return
        # energy gate: skip very quiet captures (human-like: don't over-filter borderline speech)
        try:
            a = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(a * a))) if a.size else 0.0
        except Exception:
            rms = 0.0
        floor = max(35, int(CMD_ACTIVE_RMS * 0.35))
        if rms < floor:
            if DEBUG_AUDIO:
                print(f"[Jarvis] Skipped quiet capture (rms {int(rms)}, floor {floor}).")
            print("[Jarvis] I woke up but heard no clear command. Speak right after 'Yes, I'm listening'. If your mic is quiet, try set JARVIS_DEVICE=2 or default.")
            return
        print("[Jarvis] Processing...")
        result = {"text": "", "lang": None, "done": False, "err": None}
        def _run_tx():
            try:
                t, dl = transcribe_whisper(audio_data)
                result["text"], result["lang"], result["done"] = t, dl, True
            except Exception as ex:
                result["err"] = ex; result["done"] = True
        tx = threading.Thread(target=_run_tx, daemon=True)
        tx.start()
        tx.join(timeout=12.0)
        if not result["done"]:
            print("[Jarvis] Transcription timed out.")
            return
        if result["err"]:
            print(f"[Jarvis] Transcription failed: {result['err']}")
            return
        text, detected_lang = result["text"], result["lang"]
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
            elif DEBUG_AUDIO:
                print("[Jarvis] Heard text too short; ignored.")
        else:
            # stay quiet on no-speech/noise to avoid annoying loops
            if DEBUG_AUDIO:
                print("[Jarvis] No speech recognized.")
            return

    def rms_int16(data_bytes):
        try:
            a = np.frombuffer(data_bytes, dtype=np.int16).astype(np.float32)
            if a.size == 0:
                return 0.0
            return float(np.sqrt(np.mean(a * a)))
        except Exception:
            return 0.0

    # Speech energy gates - permissive for wake (any volume), calibration only nudges up if very noisy
    DEBUG_AUDIO = os.getenv("JARVIS_DEBUG_AUDIO", "").strip() in ("1", "true", "yes")
    SPEECH_RMS_THRESH = 120       # buffer command audio
    SPEECH_ACTIVE_THRESH = 180    # reset silence timer on speech (legacy / Vosk path)
    WAKE_SPEECH_RMS_THRESH = 80   # legacy display
    WAKE_MIN_RMS = int(os.getenv("JARVIS_WAKE_MIN_RMS", "120"))  # energy gate for wake (use recent peak)
    WAKE_RECENT_SEC = 0.9
    _recent_levels = []  # (t, rms)
    MIN_WAKE_AUDIO_SEC = 0.2
    MIN_SPEECH_FRAMES = 1
    PREBUFFER_SEC = 1.8
    prebuffer = bytearray()  # rolling 16kHz mono int16 audio before wake
    _wake_loud_frames = 0
    _last_debug_t = 0.0
    last_above_thresh = 0.0
    ignore_wake_until = 0.0  # prevent self-trigger from TTS playback

    try:
        # Calibration at device-supported rate; resample to 16 kHz for consistent gates
        try:
            calib = []
            cal_sr = resolve_capture_sample_rate(DEVICE, SAMPLE_RATE)
            cal_bs = max(256, int(1600 * cal_sr / SAMPLE_RATE))
            raw_probe = []
            def _calib_cb(indata, frames, time_info, status):
                raw = bytes(indata)
                raw_probe.append(rms_int16(raw))
                chunk = raw
                if cal_sr != SAMPLE_RATE:
                    chunk = resample_int16(chunk, cal_sr, SAMPLE_RATE)
                if chunk:
                    calib.append(chunk)
            with sd.InputStream(samplerate=cal_sr, blocksize=cal_bs, channels=1, dtype='int16', callback=_calib_cb, device=DEVICE):
                time.sleep(0.8)
            raw_avg = (sum(raw_probe) / len(raw_probe)) if raw_probe else 0.0
            if calib:
                ambient = sum(rms_int16(c) for c in calib) / max(1, len(calib))
                amb = int(ambient)
                # Human-like: stay sensitive. Cap auto-gates so noisy USB mics don't block normal speech.
                # Wake needs fresh energy above ambient; command activity should catch normal speech (not only loud spikes).
                WAKE_MIN_RMS = max(60, min(320, int(os.getenv("JARVIS_WAKE_MIN_RMS", "0")) or int(amb * 1.2 + 55)))
                CMD_ACTIVE_RMS = max(40, min(180, int(os.getenv("JARVIS_CMD_RMS", "0")) or int(amb * 1.08 + 35)))
                SPEECH_RMS_THRESH = max(35, min(160, int(amb * 1.05 + 25)))
                SPEECH_ACTIVE_THRESH = max(45, min(180, int(amb * 1.08 + 30)))
                print(f"[Jarvis] Mic calibrated @ {cal_sr}Hz (ambient~{amb}, raw~{int(raw_avg)}). wake_rms>{WAKE_MIN_RMS} cmd_rms>{CMD_ACTIVE_RMS}")
                if ambient < 1 and raw_avg < 1:
                    print("[Jarvis] Warning: microphone appears silent. Check Windows mic privacy/mute/default device, or run: set JARVIS_DEVICE=default")
            if DEBUG_AUDIO:
                print("[Jarvis] Debug audio enabled (JARVIS_DEBUG_AUDIO=1). Speak to see [Debug] rms/wake text.")
        except Exception as e:
            print(f"[Jarvis] Mic calibration skipped: {e}")

        capture_sr = resolve_capture_sample_rate(DEVICE, SAMPLE_RATE)
        capture_block = max(256, int(800 * capture_sr / SAMPLE_RATE))
        print(f"[Jarvis] Audio capture: {capture_sr}Hz -> {SAMPLE_RATE}Hz for recognition")
        with open_input_stream(samplerate=capture_sr, blocksize=capture_block, device=DEVICE, callback=audio_callback):
            while True:
                try:
                    data = audio_queue.get(timeout=0.08)
                except queue.Empty:
                    if listening_for_command and last_speech_time > 0 and cmd_started_at > 0:
                        # do not cut off user immediately after wake ack/TTS
                        if (time.time() - cmd_started_at) >= MIN_LISTEN_GRACE and (time.time() - last_speech_time) > SILENCE_AFTER_SPEECH:
                            if use_whisper:
                                finalize_whisper_command()
                            elif command_buffer.strip():
                                cmd = command_buffer.strip(); command_buffer = ""
                                listening_for_command = False; last_speech_time = 0; cmd_started_at = 0.0
                                execute_command(cmd)
                            continue
                    if listening_for_command and (time.time() - last_wake_time > WAKE_TIMEOUT):
                        if use_whisper and len(command_audio) > int(SAMPLE_RATE * 2 * 0.45):
                            finalize_whisper_command()
                        elif (not use_whisper) and command_buffer.strip():
                            cmd = command_buffer.strip(); command_buffer = ""
                            listening_for_command = False; last_speech_time = 0; cmd_started_at = 0.0
                            execute_command(cmd)
                        else:
                            listening_for_command = False; command_buffer = ""; command_audio = bytearray(); last_speech_time = 0; cmd_started_at = 0.0
                    continue
                if CAPTURE_SAMPLE_RATE != SAMPLE_RATE:
                    data = resample_int16(data, CAPTURE_SAMPLE_RATE, SAMPLE_RATE)
                    if not data:
                        continue

                if use_whisper:
                    # Use Vosk for wake detection; feed all audio, trigger on any volume when "Jarvis" detected
                    if wake_recognizer is not None and not listening_for_command:
                        level = rms_int16(data)
                        now_t = time.time()
                        _recent_levels.append((now_t, level))
                        cutoff = now_t - WAKE_RECENT_SEC
                        recent = [(t, v) for t, v in _recent_levels if t >= cutoff]
                        if len(_recent_levels) > 80:
                            _recent_levels = recent
                        recent_peak = max((v for t, v in recent), default=level)
                        # require energy close to detection time, not stale spikes
                        fresh_cutoff = now_t - 0.45
                        fresh_peak = max((v for t, v in recent if t >= fresh_cutoff), default=level)
                        if DEBUG_AUDIO and (time.time() - _last_debug_t) > 1.5:
                            _last_debug_t = time.time()
                            print(f"[Debug] rms={int(level)} peak~{int(recent_peak)} fresh~{int(fresh_peak)} gate={WAKE_MIN_RMS}")
                        try:
                            accepted = wake_recognizer.AcceptWaveform(data)
                        except Exception:
                            accepted = False
                        if accepted:
                            wr = json.loads(wake_recognizer.Result())
                            wt = wr.get("text", "").strip().lower()
                        else:
                            wp = json.loads(wake_recognizer.PartialResult())
                            wt = wp.get("partial", "").strip().lower()
                        if wt and len(wt) >= 2:
                            if DEBUG_AUDIO:
                                print(f"[Debug wake] '{wt}' rms={int(level)} peak~{int(recent_peak)} fresh~{int(fresh_peak)}")
                            detected, after = detect_wake_word(wt)
                            # Wake when word is detected and very recent audio had energy (avoids stale partial+old peak)
                            wake_energy = max(level, fresh_peak)
                            if detected and wake_energy >= WAKE_MIN_RMS and time.time() >= max(ignore_wake_until, TTS_SPEAKING_UNTIL):
                                listening_for_command = True
                                last_wake_time = time.time()
                                last_speech_time = last_wake_time + 0.2  # don't immediately time out during ack
                                cmd_started_at = last_wake_time
                                command_audio = bytearray(prebuffer)  # include speech just before wake
                                prebuffer.clear()
                                _recent_levels.clear()
                                try:
                                    wake_recognizer.Reset()
                                except Exception:
                                    pass
                                ignore_wake_until = time.time() + 1.5
                                if after and len(after) >= MIN_COMMAND_CHARS:
                                    # wake phrase included command text (best-effort); still capture audio
                                    pass
                                wake_ack(lang=("hi" if LANGUAGE_MODE == "hi" else "en"))
                                continue
                            # clear stale recognizer state if wake-like partial lingers with no fresh energy
                            if detected and wake_energy < max(60, WAKE_MIN_RMS * 0.5):
                                try:
                                    wake_recognizer.Reset()
                                except Exception:
                                    pass
                        continue
                    # maintain rolling prebuffer for wake->command handoff (16kHz int16)
                    if not listening_for_command:
                        prebuffer.extend(data)
                        max_pb = int(SAMPLE_RATE * 2 * PREBUFFER_SEC)
                        if len(prebuffer) > max_pb:
                            del prebuffer[:len(prebuffer) - max_pb]
                    if listening_for_command:
                        command_audio.extend(data)
                        level = rms_int16(data)
                        now_cmd = time.time()
                        since_start = now_cmd - cmd_started_at
                        # human-like: keep window open on normal speech; be forgiving of natural pauses
                        active_gate = max(35, CMD_ACTIVE_RMS * 0.72)
                        if since_start > ACK_SUPPRESS_SEC and level > active_gate:
                            last_speech_time = now_cmd
                        elif since_start > ACK_SUPPRESS_SEC and level > max(30, CMD_ACTIVE_RMS * 0.5):
                            last_speech_time = max(last_speech_time, now_cmd - 0.15)
                        # always keep alive during grace period after wake
                        if since_start <= MIN_LISTEN_GRACE and last_speech_time < now_cmd:
                            last_speech_time = now_cmd
                        if DEBUG_AUDIO and (time.time() - _last_debug_t) > 1.5:
                            _last_debug_t = time.time()
                            print(f"[Debug] cmd rms={int(level)} buffered={len(command_audio)//2} samples gate~{int(active_gate)}")
                        # safety: finalize on max length
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
                                wake_ack(lang=("hi" if LANGUAGE_MODE == "hi" else "en"))
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
                                wake_ack(lang=("hi" if LANGUAGE_MODE == "hi" else "en"))

                if listening_for_command and not use_whisper and (time.time() - last_wake_time > WAKE_TIMEOUT):
                    if command_buffer.strip():
                        cmd = command_buffer.strip(); command_buffer = ""
                        listening_for_command = False; last_speech_time = 0
                        execute_command(cmd)
                    else:
                        listening_for_command = False; command_buffer = ""; last_speech_time = 0

    except KeyboardInterrupt:
        print("\n[Jarvis] Shutting down.")
        speak("Goodbye.", lang="en")
    except Exception as e:
        print(f"[Jarvis] Error: {e}")
        import traceback
        traceback.print_exc()
        try:
            speak("I encountered an error and need to shut down.")
        except Exception:
            pass

if __name__ == "__main__":
    main()










