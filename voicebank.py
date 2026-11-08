# voicebank.py - passive voice banking (Yasmin).
# While fresh (and before noon unless DEMO_MODE), keeps clean stt.final segments
# (mean conf >= 0.85, >= 3 s), cut from a rolling 60 s audio buffer, saved to bank/.
#   python voicebank.py --status    how much clean audio is banked
#   python voicebank.py --upload    send it to ElevenLabs, save ELEVEN_VOICE_ID to .env
import argparse, collections, datetime, glob, os, shutil, threading, time, wave
import bus, config

_buf = collections.deque()          # (arrival_time, bytes) per 0.1 s frame
_lock = threading.Lock()
_state = "fresh"
_enabled = True                     # UI can pause/resume recording
_speaker = os.getenv("ELEVEN_VOICE_NAME") or "Alex"
_phase = "idle"                     # idle | uploading | ready | error
_error = None

def _on_frame(b):
    now = time.time()
    with _lock:
        _buf.append((now, b))
        while _buf and _buf[0][0] < now - 60:
            _buf.popleft()

def _on_state(d):
    global _state
    _state = d["state"]

def _on_final(d):
    if not _enabled or _state != "fresh":
        return
    if not config.DEMO_MODE and datetime.datetime.now().hour >= 12:
        return
    words = d.get("words") or []
    if not words:
        return
    conf = sum(w["conf"] for w in words) / len(words)
    t0, t1 = d["t0"], d["t1"]
    if conf < config.BANK_MIN_CONF or (t1 - t0) < config.BANK_MIN_SECONDS:
        return
    time.sleep(0.4)  # let the tail of the segment arrive
    with _lock:
        # a frame arriving at time a covers roughly [a - 0.1, a]
        chunks = [b for a, b in _buf if a >= t0 - 0.15 and a - 0.1 <= t1 + 0.15]
    if not chunks:
        return
    os.makedirs(config.BANK_DIR, exist_ok=True)
    path = os.path.join(config.BANK_DIR, f"clip_{int(t0*1000)}.wav")
    _write_wav(path, b"".join(chunks))
    secs = banked_seconds()
    print(f"[voicebank] saved {path} ({t1-t0:.1f} s, conf {conf:.2f}) total {secs:.0f} s")
    publish_status()

def _write_wav(path, pcm):
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(config.SAMPLE_RATE)
        w.writeframes(pcm)

def _clips():
    return sorted(glob.glob(os.path.join(config.BANK_DIR, "clip_*.wav")))

def banked_seconds():
    total = 0.0
    for p in _clips():
        with wave.open(p, "rb") as w:
            total += w.getnframes() / w.getframerate()
    return total

def _set_env(key, value):
    lines = open(config.ENV_PATH).read().splitlines() if os.path.exists(config.ENV_PATH) else []
    out, done = [], False
    for ln in lines:
        if ln.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}"); done = True
        else:
            out.append(ln)
    if not done:
        out.append(f"{key}={value}")
    open(config.ENV_PATH, "w").write("\n".join(out) + "\n")

def upload(name="Alex Chen (SpeakEasy bank)"):
    import requests
    clips = _clips()
    if not clips:
        raise SystemExit("No clips in bank/ yet.")
    # one combined file keeps us under ElevenLabs' per-request file limit
    combined = os.path.join(config.BANK_DIR, "combined.wav")
    pcm = b""
    for p in clips:
        with wave.open(p, "rb") as w:
            pcm += w.readframes(w.getnframes())
    _write_wav(combined, pcm)
    with open(combined, "rb") as f:
        r = requests.post("https://api.elevenlabs.io/v1/voices/add", timeout=120,
                          headers={"xi-api-key": config.ELEVENLABS_API_KEY},
                          data={"name": name},
                          files=[("files", ("bank.wav", f, "audio/wav"))])
    if r.status_code != 200:
        raise SystemExit(f"ElevenLabs {r.status_code}: {r.text[:300]}")
    vid = r.json()["voice_id"]
    _set_env("ELEVEN_VOICE_ID", vid)
    print(f"Voice created: {vid} (saved to .env as ELEVEN_VOICE_ID)")
    return vid

def status():
    vid = os.getenv("ELEVEN_VOICE_ID") or ""
    return {"seconds": round(banked_seconds(), 1), "target": config.BANK_TARGET_SECONDS,
            "clips": len(_clips()), "enabled": _enabled, "speaker": _speaker,
            "paused_reason": None if _state == "fresh" else "paused while tiring",
            "voice_id": vid, "phase": _phase, "error": _error}

def publish_status():
    bus.publish("voicebank.progress", status())

def set_enabled(on):
    global _enabled
    _enabled = bool(on); publish_status()

def new_speaker(name):
    """Archive the current bank (never deleted) and start fresh for someone else."""
    global _speaker, _phase, _error
    name = (name or "Speaker").strip()[:40]
    if os.path.isdir(config.BANK_DIR) and os.listdir(config.BANK_DIR):
        arch = os.path.join(config.HERE, "bank_archive",
                            f"{datetime.datetime.now():%Y%m%d_%H%M%S}_{_speaker.replace(' ', '_')}")
        os.makedirs(os.path.dirname(arch), exist_ok=True)
        shutil.move(config.BANK_DIR, arch)
    os.makedirs(config.BANK_DIR, exist_ok=True)
    _speaker, _phase, _error = name, "idle", None
    _set_env("ELEVEN_VOICE_ID", ""); _set_env("ELEVEN_VOICE_NAME", name)
    os.environ["ELEVEN_VOICE_ID"] = ""; os.environ["ELEVEN_VOICE_NAME"] = name
    set_enabled(True)

def upload_async():
    def run():
        global _phase, _error
        _phase, _error = "uploading", None; publish_status()
        try:
            vid = upload(f"{_speaker} (SpeakEasy bank)")
            os.environ["ELEVEN_VOICE_ID"] = vid
            _phase = "ready"
        except BaseException as e:      # upload() raises SystemExit on API errors
            _phase, _error = "error", str(e)
        publish_status()
    threading.Thread(target=run, daemon=True).start()

def start():
    bus.subscribe("audio.frame", _on_frame, inline=True)
    bus.subscribe("state.changed", _on_state, inline=True)
    bus.subscribe("stt.final", _on_final)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    if a.upload:
        s = banked_seconds()
        if s < config.BANK_TARGET_SECONDS:
            print(f"Warning: only {s:.0f} s banked (target {config.BANK_TARGET_SECONDS} s). Uploading anyway.")
        upload()
    else:
        print(f"{banked_seconds():.0f} s of clean audio in {len(_clips())} clips")
