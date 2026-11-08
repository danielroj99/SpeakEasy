# stt.py - Vosk speech to text (Yasmin).
# In:  audio.frame (bytes)
# Out: stt.partial {text, ts}            at most 4x per second
#      stt.final   {text, words:[{word,start,end,conf}], t0, t1}   wall-clock seconds
import json, queue, threading, time
import bus, config

_q = queue.Queue()
_stream_start = None
_samples = 0

def _on_frame(b):
    # runs inline in the audio thread: keep it tiny
    global _stream_start
    if _stream_start is None:
        _stream_start = time.time() - 0.1
    _q.put(b)

def _run():
    global _samples
    from vosk import Model, KaldiRecognizer, SetLogLevel
    SetLogLevel(-1)
    model = Model(config.VOSK_MODEL_PATH)
    rec = KaldiRecognizer(model, config.SAMPLE_RATE)
    rec.SetWords(True)
    print("[stt] Vosk ready")
    last_partial_t, last_partial = 0.0, ""
    while True:
        b = _q.get()
        _samples += len(b) // 2
        if rec.AcceptWaveform(b):
            _publish_final(json.loads(rec.Result()))
            last_partial = ""
        else:
            now = time.time()
            if now - last_partial_t >= 0.25:
                p = json.loads(rec.PartialResult()).get("partial", "")
                if p and p != last_partial:
                    bus.publish("stt.partial", {"text": p, "ts": now})
                    last_partial, last_partial_t = p, now

def _publish_final(r):
    words = r.get("result") or []
    if not words:
        return
    base = _stream_start  # Vosk times are seconds since the stream started
    ws = [{"word": w["word"], "start": base + w["start"],
           "end": base + w["end"], "conf": float(w.get("conf", 1.0))} for w in words]
    bus.publish("stt.final", {"text": r.get("text", ""), "words": ws,
                              "t0": ws[0]["start"], "t1": ws[-1]["end"]})

def start():
    bus.subscribe("audio.frame", _on_frame, inline=True)
    threading.Thread(target=_run, daemon=True).start()
