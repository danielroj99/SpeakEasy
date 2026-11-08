# recap.py - Gemini summary + ElevenLabs voice + playback (Yasmin).
# In:  touch.hold, recap.play_text {text}, stt.final, state.changed, debug.reset
# Out: recap.status {phase: generating|playing|done|error, text?}
import os, platform, shutil, subprocess, tempfile, threading, time, wave
import requests
import bus, config

PROMPT = (
    "Write the end-of-class recap that Mr. Chen, a high school biology teacher, will play "
    "to his class in his own voice. Use only the lesson transcript below, which has "
    "speech-recognition errors, so skip garbled parts. 60 to 90 words, first person, his "
    "speaking style: the 3 key ideas, key terms, and any homework or reminders he gave. "
    "Plain spoken sentences, no lists.\n\nTranscript:\n"
)
NOT_ENOUGH = "There isn't enough from today's lesson to recap yet. Let's keep going."

_busy = threading.Lock()
_finals = []                 # fallback transcript if store.py isn't wired yet
_last_recap_ts = None
_state = "fresh"
_draft = {"made": 0.0, "key": None, "text": None, "mp3": None}   # key = _last_recap_ts it was built for
_tmp = tempfile.mkdtemp(prefix="recap_")

# ---------- building blocks (each testable on its own) ----------
def transcript_since(ts):
    local = " ".join(t for t0, t in _finals if t0 >= ts)
    try:
        import store
        saved = store.transcript_since(ts)
        return saved if len(saved.split()) >= len(local.split()) else local
    except Exception:
        return local

_model = None   # Gemini model that worked last (auto-picked if the configured one fails)
_SKIP_WORDS = ("image", "tts", "live", "audio", "embedding", "omni", "vision", "robotics", "learnlm")
_PREFERRED = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash-lite",
              "gemini-1.5-flash", "gemini-3-flash", "gemini-3.0-flash"]

def _candidates(client):
    """Models to try, best first: configured, known free-tier Flash models, then other stable Flash ones."""
    names = []
    try:
        for m in client.models.list():
            acts = getattr(m, "supported_actions", None) or []
            if acts and "generateContent" not in acts:
                continue
            n = m.name.split("/")[-1]
            if not any(k in n for k in _SKIP_WORDS):
                names.append(n)
    except Exception as e:
        print("[recap] could not list Gemini models:", str(e)[:120])
    order = [config.GEMINI_MODEL] + [p for p in _PREFERRED if p in names]
    rest = sorted((n for n in names if "flash" in n and n not in order),
                  key=lambda n: ("preview" in n or "exp" in n, "lite" in n, n))
    seen, out = set(), []
    for n in order + rest:
        if n not in seen:
            seen.add(n); out.append(n)
    return out[:8]

def _skippable(e):
    t = str(e)
    return any(k in t for k in ("NOT_FOUND", "404", "RESOURCE_EXHAUSTED", "429", "PERMISSION_DENIED", "403"))

def summarize(transcript):
    global _model
    from google import genai
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    tried = []
    for model in ([_model] if _model else []) + [m for m in _candidates(client) if m != _model]:
        try:
            resp = client.models.generate_content(model=model, contents=PROMPT + transcript)
        except Exception as e:
            if not _skippable(e):
                raise
            tried.append(f"{model} ({'quota' if '429' in str(e) or 'RESOURCE' in str(e) else 'unavailable'})")
            continue
        if model != _model:
            print(f"[recap] using Gemini model {model}")
        _model = model
        return (resp.text or "").strip()
    raise RuntimeError("no Gemini model worked for this key: " + ", ".join(tried))

TTS_RATE = 22050

def tts(text, path):
    """ElevenLabs -> raw 16-bit PCM -> .wav (plays anywhere without ffmpeg)."""
    url = (f"https://api.elevenlabs.io/v1/text-to-speech/{config.voice_id()}"
           f"?output_format=pcm_{TTS_RATE}")
    r = requests.post(url, timeout=30,
                      headers={"xi-api-key": config.ELEVENLABS_API_KEY, "Content-Type": "application/json"},
                      json={"text": text, "model_id": "eleven_multilingual_v2"})
    if r.status_code != 200:
        raise RuntimeError(f"ElevenLabs {r.status_code}: {r.text[:200]}")
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(TTS_RATE)
        w.writeframes(r.content)
    return path

def play(path):
    """Plays on the default speaker (set the JBL as default output)."""
    if path.lower().endswith(".wav"):
        import numpy as np, sounddevice as sd
        with wave.open(path, "rb") as w:
            sr, ch = w.getframerate(), w.getnchannels()
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        sd.play(a.reshape(-1, ch) if ch > 1 else a, sr)
        time.sleep(len(a) / ch / sr + 0.3)   # sd.wait() can hang on some Windows audio drivers
        sd.stop()
    elif platform.system() == "Darwin":
        subprocess.run(["afplay", path])
    elif shutil.which("ffplay"):
        subprocess.run(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path])
    else:
        os.startfile(path)  # Windows default player, last resort for an .mp3 backup

def _status(phase, text=None):
    bus.publish("recap.status", {"phase": phase, "text": text})

# ---------- the recap ----------
def _window_start():
    return _last_recap_ts or (time.time() - config.RECAP_LOOKBACK_MIN * 60)

def _make(text_override=None):
    """Returns (text, mp3_path)."""
    out = os.path.join(_tmp, f"recap_{int(time.time()*1000)}.wav")
    if text_override:
        return text_override, tts(text_override, out)
    since = _window_start()
    d = _draft
    if d["mp3"] and d["key"] == _last_recap_ts and time.time() - d["made"] < config.RECAP_DRAFT_EVERY_S + 60:
        return d["text"], d["mp3"]                       # fast path: background draft
    transcript = transcript_since(since)
    n = len(transcript.split())
    if n < config.RECAP_MIN_WORDS:
        print(f"[recap] only {n} words since the last recap (need {config.RECAP_MIN_WORDS})")
        return NOT_ENOUGH, tts(NOT_ENOUGH, out)
    print(f"[recap] summarizing {n} words with Gemini...")
    t = time.time()
    try:
        text = _with_timeout(summarize, transcript, seconds=GEMINI_TIMEOUT_S)
        print(f"[recap] Gemini done in {time.time() - t:.1f} s")
    except Exception as e:
        if not getattr(config, "RECAP_FALLBACK_TEXT", None):
            raise
        print(f"[recap] Gemini failed ({str(e)[:120]}), using RECAP_FALLBACK_TEXT")
        text = config.RECAP_FALLBACK_TEXT
    print("[recap] making the voice with ElevenLabs...")
    t = time.time()
    path = tts(text, out)
    print(f"[recap] voice ready in {time.time() - t:.1f} s")
    return text, path

GEMINI_TIMEOUT_S = 25

def _with_timeout(fn, *args, seconds=25):
    """Runs fn in a helper thread so a hung network call can't freeze the recap."""
    box = {}
    def run():
        try:
            box["v"] = fn(*args)
        except BaseException as e:
            box["e"] = e
    th = threading.Thread(target=run, daemon=True); th.start(); th.join(seconds)
    if th.is_alive():
        raise TimeoutError(f"Gemini took longer than {seconds} s")
    if "e" in box:
        raise box["e"]
    return box["v"]

def run_recap(text_override=None):
    global _last_recap_ts
    if not _busy.acquire(blocking=False):
        print("[recap] already running, ignoring")
        return
    try:
        _status("generating")
        text, mp3 = _make(text_override)
        _status("playing", text)
        print("[recap] playing:", text[:100] + ("..." if len(text) > 100 else ""))
        play(mp3)
        print("[recap] done")
        if not text_override and text != NOT_ENOUGH:
            _last_recap_ts = time.time()
            _draft.update(mp3=None, text=None, key=None)
        _status("done", text)
    except Exception as e:
        print("[recap] ERROR:", str(e)[:300])
        _status("error", str(e))
        if os.path.exists(config.RECAP_BACKUP):
            _status("playing", "(backup recap)")
            play(config.RECAP_BACKUP)
            _status("done", "(backup recap)")
    finally:
        _busy.release()

# ---------- background drafts while tiring ----------
def _draft_loop():
    while True:
        time.sleep(30)
        if _state not in ("tiring", "recap_ready") or _busy.locked():
            continue
        if time.time() - _draft["made"] < config.RECAP_DRAFT_EVERY_S:
            continue
        since = _window_start()
        transcript = transcript_since(since)
        if len(transcript.split()) < config.RECAP_MIN_WORDS:
            continue
        try:
            text = summarize(transcript)
            mp3 = tts(text, os.path.join(_tmp, f"draft_{int(time.time())}.wav"))
            _draft.update(made=time.time(), key=_last_recap_ts, text=text, mp3=mp3)
            print("[recap] draft ready")
        except Exception as e:
            _draft["made"] = time.time()          # wait a full interval before trying again
            print("[recap] draft failed:", str(e)[:200])

# ---------- wiring ----------
def _on_final(d):  _finals.append((d["t0"], d["text"]))
def _on_state(d):
    global _state
    _state = d["state"]
def _on_reset(_):
    global _last_recap_ts
    _finals.clear(); _last_recap_ts = None
    _draft.update(made=0.0, key=None, text=None, mp3=None)

def start():
    bus.subscribe("touch.hold", lambda _: run_recap())
    bus.subscribe("recap.play_text", lambda d: run_recap((d or {}).get("text") or None))
    bus.subscribe("stt.final", _on_final, inline=True)
    bus.subscribe("state.changed", _on_state, inline=True)
    bus.subscribe("debug.reset", _on_reset, inline=True)
    threading.Thread(target=_draft_loop, daemon=True).start()

if __name__ == "__main__":
    # quick test:  python recap.py "Today we covered photosynthesis ..."
    import sys
    t = " ".join(sys.argv[1:]) or "Hello class, this is a test of the recap voice."
    if len(t.split()) >= 20:
        t = summarize(t)
        print("Gemini:", t)
    play(tts(t, os.path.join(_tmp, "test.wav")))