# audio.py - STAND-IN so the software can be tested on a laptop.
# Publishes audio.frame: 16 kHz mono int16 bytes, 0.1 s chunks.
# AUDIO_FILE=some.wav in .env replays a recording in real time instead of the mic.
import threading, time, wave
import numpy as np
import bus, config

CHUNK = 1600  # 0.1 s at 16 kHz

def _to_16k_mono(a, sr, ch):
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if sr != 16000:
        n = int(len(a) * 16000 / sr)
        a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a)
    return a.astype(np.int16)

def _from_file(path):
    with wave.open(path, "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise SystemExit("AUDIO_FILE must be 16-bit WAV (convert: ffmpeg -i in.m4a -ar 16000 -ac 1 out.wav)")
    a = _to_16k_mono(np.frombuffer(raw, dtype=np.int16), sr, ch)
    print(f"[audio] replaying {path} ({len(a)/16000:.0f} s)")
    nxt = time.time()
    for i in range(0, len(a), CHUNK):
        bus.publish("audio.frame", a[i:i + CHUNK].tobytes())
        nxt += 0.1
        time.sleep(max(0, nxt - time.time()))
    print("[audio] file finished")

def _from_mic():
    import sounddevice as sd
    try:
        with sd.RawInputStream(samplerate=16000, channels=1, dtype="int16",
                               blocksize=CHUNK, device=config.MIC_DEVICE) as s:
            print("[audio] mic at 16 kHz")
            while True:
                data, _ = s.read(CHUNK)
                bus.publish("audio.frame", bytes(data))
    except sd.PortAudioError:
        with sd.RawInputStream(samplerate=48000, channels=1, dtype="int16",
                               blocksize=CHUNK * 3, device=config.MIC_DEVICE) as s:
            print("[audio] mic at 48 kHz, downsampling")
            while True:
                data, _ = s.read(CHUNK * 3)
                a = np.frombuffer(bytes(data), dtype=np.int16).reshape(-1, 3).mean(axis=1)
                bus.publish("audio.frame", a.astype(np.int16).tobytes())

def start():
    target = (lambda: _from_file(config.AUDIO_FILE)) if config.AUDIO_FILE else _from_mic
    threading.Thread(target=target, daemon=True).start()
