# config.py - settings. Keys and machine-specific values go in .env.
import os
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")
load_dotenv(ENV_PATH)

def _get(k, default=None):
    v = os.getenv(k)
    return v if v not in (None, "") else default

GEMINI_API_KEY = _get("GEMINI_API_KEY", "")
GEMINI_MODEL = _get("GEMINI_MODEL", "gemini-2.5-flash")
ELEVENLABS_API_KEY = _get("ELEVENLABS_API_KEY", "")
TIGER_DSN = _get("TIGER_DSN", "")
AUDIO_FILE = _get("AUDIO_FILE")
SERIAL_PORT = _get("SERIAL_PORT")       # e.g. COM5 on Windows; blank = auto-detect
_mic = _get("MIC_DEVICE")
MIC_DEVICE = int(_mic) if _mic and _mic.isdigit() else _mic
DEMO_MODE = _get("DEMO_MODE", "1") == "1"
WEB_PORT = int(_get("WEB_PORT", "5000"))   # use 5050 on a Mac (AirPlay owns 5000)
VOSK_MODEL_PATH = _get("VOSK_MODEL_PATH", os.path.join(HERE, "vosk-model-small-en-us-0.15"))

SAMPLE_RATE = 16000
BANK_DIR = os.path.join(HERE, "bank")
BANK_MIN_CONF = 0.85
BANK_MIN_SECONDS = 3.0
BANK_TARGET_SECONDS = 90
RECAP_MIN_WORDS = 50
RECAP_LOOKBACK_MIN = 45
RECAP_DRAFT_EVERY_S = 300
RECAP_BACKUP = next((os.path.join(HERE, f) for f in ("recap_backup.wav", "recap_backup.mp3") if os.path.exists(os.path.join(HERE, f))), os.path.join(HERE, "recap_backup.wav"))

def voice_id():
    """Re-read .env so a freshly banked voice is picked up without a restart."""
    load_dotenv(ENV_PATH, override=True)
    return _get("ELEVEN_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")  # default voice until banked

# ---- fatigue settings (read by Daniel's fatigue.py) ----
# Demo values: baseline in 2 min, colour changes after 1 minute, faster reaction.
# For a real school day use BASELINE_MINUTES = 5, STATE_AGREE_MINUTES = 2, FATIGUE_SMOOTH_PREV = 0.6.
BASELINE_MINUTES = 2
STATE_AGREE_MINUTES = 1
FATIGUE_SMOOTH_PREV = 0.4
LATE_RECAP_TIME = "23:59"     # the after-2:30 PM auto-recap rule is off for judging (normal: "14:30")
BASELINE_FILE = os.path.join(HERE, "baseline.json")
PASSAGE_TEXT = None           # set to the passage the actor reads to use passage mode (ask Daniel)
FINALS_LOG = None             # e.g. os.path.join(HERE, "finals.jsonl") to record Vosk output for replay

# Recap safety net: if Gemini fails or hangs, this text is read in the cloned voice instead.
# Set it to the recap you expect from your demo lesson, or leave None to show the error.
RECAP_FALLBACK_TEXT = None