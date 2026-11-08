# fatigue_simple.py - fallback fatigue score (used only if fatigue.py or align_metrics.py fails to load).
# In:  stt.final {text, words:[{word,start,end,conf}], t0, t1}, debug.fatigue {level}, debug.reset
# Out: metrics.minute {ts, wpm, mean_conf, pause_ratio, n_words, fatigue, state, baseline_wpm, baseline_conf}
#      state.changed {state: fresh | tiring | recap_ready}   (only when it changes)
#
# Every window (60 s by default): speaking rate + mean word confidence, compared with the
# speaker's own baseline from their first few talking windows. Scaled so Alex's documented
# 9 AM -> 4 PM drop (165 -> 118 wpm, 28%; 98% -> 91% intelligibility, 7%) lands near 1.0.
import datetime, json, os, statistics, threading, time
import bus, config

WINDOW_S = float(os.getenv("FATIGUE_WINDOW_S", "60"))        # demo tip: 20 for faster reactions
BASELINE_WINDOWS = int(os.getenv("FATIGUE_BASELINE_WINDOWS", getattr(config, "BASELINE_MINUTES", 5)))
MIN_WORDS = max(6, int(15 * WINDOW_S / 60))                   # fewer = not really talking, skip
WPM_DROP_FULL, CONF_DROP_FULL = 0.28, 0.07                    # from the case numbers
SMOOTH = 0.4                                                  # weight of the newest window
TIRING_AT, RECAP_AT = 0.35, 0.70
CONFIRM = getattr(config, "STATE_AGREE_MINUTES", 2)                                                   # windows in a row before the state changes
OVERRIDE_HOLD_S = 180                                         # demo buttons win for 3 minutes
BASELINE_PATH = os.path.join(config.HERE, "baseline.json")

_lock = threading.Lock()
_finals = []
_baseline_samples = []
_baseline = None              # {"wpm":..., "conf":..., "date": "YYYY-MM-DD"}
_score = 0.0
_state = "fresh"
_pending, _pending_n = None, 0
_override_until = 0.0
_first_speech = None

def _level_to_state(x):
    return "fresh" if x < TIRING_AT else ("tiring" if x < RECAP_AT else "recap_ready")

def _load_baseline():
    global _baseline
    try:
        b = json.load(open(BASELINE_PATH))
        if b.get("date") == str(datetime.date.today()):
            _baseline = b
            print(f"[fatigue] using today's baseline: {b['wpm']:.0f} wpm, conf {b['conf']:.2f}")
    except Exception:
        pass

def _save_baseline():
    try:
        json.dump(_baseline, open(BASELINE_PATH, "w"))
    except Exception as e:
        print("[fatigue] could not save baseline:", e)

def window_metrics(finals, window_s):
    """Pure function, easy to test: finals in one window -> metrics or None."""
    words = [w for f in finals for w in (f.get("words") or [])]
    n_words = len(words) if words else sum(len(f.get("text", "").split()) for f in finals)
    speaking = sum(max(0.0, f["t1"] - f["t0"]) for f in finals)
    if n_words < MIN_WORDS or speaking < 2:
        return None
    return {"wpm": n_words / (speaking / 60.0),
            "mean_conf": (sum(w["conf"] for w in words) / len(words)) if words else None,
            "pause_ratio": max(0.0, 1 - speaking / window_s),
            "n_words": n_words}

def raw_score(m, base):
    wpm_drop = max(0.0, 1 - m["wpm"] / base["wpm"]) / WPM_DROP_FULL
    if m["mean_conf"] is None or not base.get("conf"):
        return min(1.0, wpm_drop)
    conf_drop = max(0.0, (base["conf"] - m["mean_conf"]) / base["conf"]) / CONF_DROP_FULL
    return 0.5 * min(1.0, wpm_drop) + 0.5 * min(1.0, conf_drop)

ORDER = ["fresh", "tiring", "recap_ready"]

def _set_state(new):
    """Moves one step at a time (green -> amber -> purple), each confirmed over CONFIRM windows."""
    global _state, _pending, _pending_n
    i, j = ORDER.index(_state), ORDER.index(new)
    if abs(j - i) > 1:
        new = ORDER[i + (1 if j > i else -1)]
    if new == _state:
        _pending, _pending_n = None, 0
        return
    if new == _pending:
        _pending_n += 1
    else:
        _pending, _pending_n = new, 1
    if _pending_n >= CONFIRM:
        _state, _pending, _pending_n = new, None, 0
        bus.publish("state.changed", {"state": _state})

def _late_day_recap():
    """After 2:30 PM with 10+ min of speech, offer the recap anyway (off in demo mode)."""
    if config.DEMO_MODE or _first_speech is None:
        return False
    return datetime.datetime.now().time() >= datetime.time(14, 30) and time.time() - _first_speech >= 600

def _tick():
    global _score, _baseline
    now = time.time()
    with _lock:
        win = [f for f in _finals if f["t0"] >= now - WINDOW_S]
        _finals[:] = [f for f in _finals if f["t0"] >= now - 2 * WINDOW_S]
    m = window_metrics(win, WINDOW_S)
    if m is None:
        return
    if _baseline is None:
        _baseline_samples.append(m)
        n = len(_baseline_samples)
        print(f"[fatigue] baseline {n}/{BASELINE_WINDOWS}: {m['wpm']:.0f} wpm, conf {m['mean_conf'] or 0:.2f}")
        if n >= BASELINE_WINDOWS:
            confs = [s["mean_conf"] for s in _baseline_samples if s["mean_conf"] is not None]
            _baseline = {"wpm": statistics.median(s["wpm"] for s in _baseline_samples),
                         "conf": statistics.median(confs) if confs else None,
                         "date": str(datetime.date.today())}
            _save_baseline()
            print(f"[fatigue] baseline set: {_baseline['wpm']:.0f} wpm, conf {_baseline['conf'] or 0:.2f}")
        bus.publish("metrics.minute", {"ts": now, **m, "fatigue": _score, "state": _state,
                                       "baseline_wpm": None, "baseline_conf": None})
        return
    if now < _override_until:
        print(f"[fatigue] {m['wpm']:.0f} wpm (demo override active, not changing state)")
        return
    _score = (1 - SMOOTH) * _score + SMOOTH * raw_score(m, _baseline)
    target = "recap_ready" if _late_day_recap() else _level_to_state(_score)
    _set_state(target)
    print(f"[fatigue] {m['wpm']:.0f} wpm, conf {m['mean_conf'] or 0:.2f} -> score {_score:.2f} ({_state})")
    bus.publish("metrics.minute", {"ts": now, **m, "fatigue": round(_score, 3), "state": _state,
                                   "baseline_wpm": _baseline["wpm"], "baseline_conf": _baseline["conf"]})

def _loop():
    while True:
        time.sleep(WINDOW_S)
        try:
            _tick()
        except Exception as e:
            print("[fatigue] error:", e)

# ---------- bus handlers ----------
def _on_final(d):
    global _first_speech
    if not d.get("t0"):
        return
    if _first_speech is None:
        _first_speech = d["t0"]
    with _lock:
        _finals.append(d)

def _on_debug(d):
    """Demo button: adopt the level as our score and hold it for a few minutes."""
    global _score, _state, _override_until, _pending, _pending_n
    _score = float(d.get("level", _score))
    _state = _level_to_state(_score)        # web.py already published state.changed
    _pending, _pending_n = None, 0
    _override_until = time.time() + OVERRIDE_HOLD_S

def _on_reset(_):
    global _score, _state, _baseline, _override_until, _first_speech
    with _lock:
        _finals.clear()
    _baseline_samples.clear()
    _score, _state, _baseline, _override_until, _first_speech = 0.0, "fresh", None, 0.0, None
    try:
        os.remove(BASELINE_PATH)
    except OSError:
        pass
    print("[fatigue] reset: new baseline will be learned")

def status():
    return {"score": _score, "state": _state, "baseline": _baseline,
            "baseline_progress": f"{len(_baseline_samples)}/{BASELINE_WINDOWS}"}

def start():
    _load_baseline()
    bus.subscribe("stt.final", _on_final, inline=True)
    bus.subscribe("debug.fatigue", _on_debug, inline=True)
    bus.subscribe("debug.reset", _on_reset, inline=True)
    threading.Thread(target=_loop, daemon=True).start()
    print(f"[fatigue] window {WINDOW_S:.0f} s, baseline from first {BASELINE_WINDOWS} talking windows")
