# web.py - Flask UI + live events.
#   GET /          projector captions (clean, big type)
#   GET /control   control panel: voice bank, recap, touch pad status, demo controls
#   GET /stream    Server-Sent Events     GET /status  JSON
import json, queue, threading, time
from flask import Flask, Response, jsonify, render_template, request, abort
import bus, config

app = Flask(__name__)
_clients, _lock = [], threading.Lock()
STATE = {"captions_on": True, "state": "fresh", "fatigue": 0.0, "last_metrics": None,
         "recap": None, "bank": None, "arduino": False, "transcript": []}

def _broadcast(event, data):
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    with _lock:
        for q in list(_clients):
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass

def _snapshot():
    try:
        import hub
        STATE["arduino"] = getattr(hub, "_ser", None) is not None
    except Exception:
        STATE["arduino"] = False
    try:
        import voicebank
        STATE["bank"] = voicebank.status()
    except Exception:
        pass
    return STATE

# ---- bus handlers ----
def _partial(d): _broadcast("partial", {"text": d["text"]})
def _final(d):
    STATE["transcript"] = (STATE["transcript"] + [{"t": d.get("t0"), "text": d["text"]}])[-30:]
    _broadcast("final", {"text": d["text"], "t": d.get("t0")})
def _metrics(d):
    STATE["last_metrics"] = d
    if d.get("fatigue") is not None:
        STATE["fatigue"] = d["fatigue"]
    _broadcast("metrics", d)
def _state(d):
    STATE["state"] = d["state"]; _broadcast("state", d)
def _tap(d):
    STATE["captions_on"] = not STATE["captions_on"]
    _broadcast("captions", {"on": STATE["captions_on"]})
    _broadcast("touch", {"kind": "tap", "source": (d or {}).get("source", "ui")})
def _hold(d):
    _broadcast("touch", {"kind": "hold", "source": (d or {}).get("source", "ui")})
def _recap(d):
    STATE["recap"] = d; _broadcast("recap", d)
def _bank(d):
    STATE["bank"] = d; _broadcast("bank", d)
def _hub(d):
    STATE["arduino"] = d.get("connected", False); _broadcast("hub", d)

# ---- pages + stream ----
@app.route("/")
def index():
    return render_template("captions.html")

@app.route("/control")
def control():
    return render_template("control.html", demo=config.DEMO_MODE)

@app.route("/status")
def status():
    return jsonify(_snapshot())

@app.route("/stream")
def stream():
    q = queue.Queue(maxsize=300)
    with _lock:
        _clients.append(q)
    def gen():
        try:
            yield f"event: hello\ndata: {json.dumps(_snapshot())}\n\n"
            while True:
                try:
                    yield q.get(timeout=15)
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            with _lock:
                if q in _clients:
                    _clients.remove(q)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ---- voice bank (always available, not just demo mode) ----
@app.post("/api/voicebank/new")
def vb_new():
    import voicebank
    name = (request.get_json(silent=True) or {}).get("name") or "Speaker"
    voicebank.new_speaker(name)
    return jsonify(ok=True, bank=voicebank.status())

@app.post("/api/voicebank/record")
def vb_record():
    import voicebank
    on = bool((request.get_json(silent=True) or {}).get("on", True))
    voicebank.set_enabled(on)
    return jsonify(ok=True, bank=voicebank.status())

@app.post("/api/voicebank/clone")
def vb_clone():
    import voicebank
    if voicebank.banked_seconds() < 10:
        return jsonify(ok=False, error="Record at least 10 s first (90 s is best)."), 400
    voicebank.upload_async()
    return jsonify(ok=True)

# ---- demo controls ----
def _demo():
    if not config.DEMO_MODE:
        abort(404)

def _level_to_state(x):
    return "fresh" if x < 0.35 else ("tiring" if x < 0.7 else "recap_ready")

@app.post("/debug/fatigue")
def dbg_fatigue():
    _demo()
    lvl = float(request.args.get("level", 0.5))
    bus.publish("debug.fatigue", {"level": lvl})   # fatigue.py should adopt this as its score
    st = _level_to_state(lvl)
    bus.publish("metrics.minute", {"ts": time.time(), "wpm": None, "mean_conf": None,
                                   "pause_ratio": None, "n_words": 0, "fatigue": lvl, "state": st})
    bus.publish("state.changed", {"state": st})
    return jsonify(ok=True, state=st)

@app.post("/api/new_class")
def new_class():
    bus.publish("recap.new_class")
    _broadcast("newclass", {})
    return jsonify(ok=True)

@app.post("/debug/tap")
def dbg_tap():
    _demo(); bus.publish("touch.tap", {"source": "ui"}); return jsonify(ok=True)

@app.post("/debug/hold")
def dbg_hold():
    _demo(); bus.publish("touch.hold", {"source": "ui"}); return jsonify(ok=True)

@app.post("/debug/recap_text")
def dbg_recap_text():
    _demo()
    text = (request.get_json(silent=True) or {}).get("text") or request.form.get("text") \
        or request.get_data(as_text=True)
    bus.publish("recap.play_text", {"text": (text or "").strip()})
    return jsonify(ok=True)

@app.post("/debug/reset")
def dbg_reset():
    _demo()
    bus.publish("debug.reset")
    bus.publish("state.changed", {"state": "fresh"})
    STATE.update(captions_on=True, last_metrics=None, recap=None, fatigue=0.0, transcript=[])
    _broadcast("reset", {})
    return jsonify(ok=True)

def start():
    for topic, fn in [("stt.partial", _partial), ("stt.final", _final),
                      ("metrics.minute", _metrics), ("state.changed", _state),
                      ("touch.tap", _tap), ("touch.hold", _hold), ("recap.status", _recap),
                      ("voicebank.progress", _bank), ("hub.status", _hub)]:
        bus.subscribe(topic, fn, inline=True)
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=config.WEB_PORT,
                     threaded=True, use_reloader=False), daemon=True).start()
    print(f"[web] captions at http://localhost:{config.WEB_PORT}   control panel at http://localhost:{config.WEB_PORT}/control")