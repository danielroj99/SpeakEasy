"""fatigue.py - per-minute speech metrics + fatigue score (Daniel).

Runs on the LAPTOP. The Arduino 101 only drives the touch pad / LCD / vibration motor,
and hub.py relays state.changed to it. This module never touches hardware and
talks to the rest of the app only through bus.py.

How it decides (set up for the demo):
  * Calibration: his FIRST MINUTE OF TALKING (the 60 s that start with his first word) is
    his baseline: words per minute + Vosk confidence. A minute with under 30 s of talking
    (a mic check, a few words) does not count: the calibration minute starts again at his
    next phrase. The baseline is kept in memory only, so every run of app.py calibrates
    again, and "Reset session" (POST /debug/reset) calibrates again from his next minute.
  * After that, one score per minute of talking: wpm drop + clarity drop against that
    baseline, smoothed -> fresh / tiring / recap_ready. Going up, the state moves one step
    per minute, so amber (and the armband's "tiring" buzz) always comes before purple.
  * The time of day is NOT used: no "after 2:30 PM" rule, no reset at midnight, no
    baseline reused from an earlier run or from baseline.json. Minutes are counted from his
    first word, not from the clock. (day_part "am"/"pm" is still written to each minute,
    only as a label for the database; it never changes the state.)

Subscribes
  stt.final      {text, words: [{word, start, end, conf}], t0, t1}   t0/t1 = wall-clock s
  debug.fatigue  {level: float}   demo buttons (POST /debug/fatigue): adopts the level as the score
  debug.reset    None             "Reset session" (POST /debug/reset): score 0, fresh, calibrate again
Publishes
  metrics.minute {ts, wpm, mean_conf, pause_ratio, n_words, fatigue, state, mode, accuracy,
                  ms_per_syll, day_part, baseline_wpm, baseline_conf}
                 one per minute of talking (15+ words); store.py saves it
  state.changed  {state}          "fresh" | "tiring" | "recap_ready"

Two modes (config.FATIGUE_MODE):
  class    the spec formula as written: wpm drop + Vosk-confidence drop.
  passage  someone reads config.PASSAGE_TEXT (demo, "slow, slurred reading" checklist test).
           The confidence drop is replaced by the drop in word accuracy against the
           passage (edit-distance alignment, the LCS family with a cost for skipped
           words), which is the quantity the spec's 0.07 was built from (98 -> 91 %).
           Use it only while the passage is being read.

Integration: app.py calls fatigue.start() once. Optional config.FINALS_LOG = "finals.jsonl"
records every stt.final so you can replay real Vosk output later.

Test without hardware:
  python fatigue.py --replay finals.jsonl                      (one stt.final per line)
"""
import json
import re
import threading
import time
from datetime import datetime
from statistics import mean, median

try:
    import config
except ImportError:  # lets the file run on its own
    config = None

try:
    import pronouncing  # optional: CMUdict syllable counts (pip install pronouncing)
except ImportError:
    pronouncing = None


# ---------- text helpers (self-contained: fatigue.py needs no other file of ours) ----------
_VOWEL_GROUPS = re.compile(r"[aeiouy]+")


def norm_tokens(text):
    """Lowercase word tokens; keeps apostrophes (don't, it's)."""
    return re.findall(r"[a-z']+", text.lower())


def syllables(word):
    w = word.lower().strip("'")
    if pronouncing is not None:
        phones = pronouncing.phones_for_word(w)
        if phones:
            return max(1, pronouncing.syllable_count(phones[0]))
    n = len(_VOWEL_GROUPS.findall(w))
    if w.endswith("e") and n > 1 and not w.endswith(("le", "ee")):
        n -= 1  # silent final e
    return max(1, n)


def align_segment(ref, hyp):
    """Edit-distance alignment of hyp against the best-matching STRETCH of ref (free start
    and end inside ref; inside the stretch every skipped ref word costs 1). This is the
    LCS family with a cost for skipped words: plain LCS spreads its matches over a long or
    repeated passage. Returns {hits, subs, dels, ins, accuracy}."""
    n, m = len(ref), len(hyp)
    if n == 0 or m == 0:
        return {"hits": 0, "subs": 0, "dels": 0, "ins": m, "accuracy": None}
    D = [[0] * (m + 1) for _ in range(n + 1)]
    D[0] = list(range(m + 1))
    for i in range(1, n + 1):
        Di, Dp, r = D[i], D[i - 1], ref[i - 1]
        for j in range(1, m + 1):
            best = Dp[j - 1] + (0 if r == hyp[j - 1] else 1)
            if Dp[j] + 1 < best:
                best = Dp[j] + 1
            if Di[j - 1] + 1 < best:
                best = Di[j - 1] + 1
            Di[j] = best
    i = min(range(n + 1), key=lambda k: D[k][m])
    j = m
    hits = subs = dels = ins = 0
    while j > 0:
        if i > 0 and D[i][j] == D[i - 1][j - 1] + (0 if ref[i - 1] == hyp[j - 1] else 1):
            if ref[i - 1] == hyp[j - 1]:
                hits += 1
            else:
                subs += 1
            i -= 1
            j -= 1
        elif i > 0 and D[i][j] == D[i - 1][j] + 1:
            dels += 1
            i -= 1
        else:
            ins += 1
            j -= 1
    read = hits + subs + dels
    return {"hits": hits, "subs": subs, "dels": dels, "ins": ins,
            "accuracy": hits / read if read else None}


def _cfg(name, default):
    return getattr(config, name, default) if config is not None else default


# Hamilton time, ONLY for the "am"/"pm" label of each minute (never for the state).
TZ_NAME = _cfg("TZ_NAME", "America/Toronto")
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(TZ_NAME)
except Exception:  # Windows needs `pip install tzdata`; falls back to the laptop's clock
    TZ = None
    print(f"fatigue.py: no time-zone data for {TZ_NAME}; using the laptop's local time "
          "(on Windows: pip install tzdata)")

WINDOW_S = 60.0                                         # one score per minute of talking
# Demo: the baseline is his FIRST minute of talking. config.BASELINE_MINUTES,
# LATE_RECAP_TIME and BASELINE_FILE are not used by this file any more.
CALIBRATION_MINUTES = _cfg("FATIGUE_CALIBRATION_MINUTES", 1)
CALIBRATION_MIN_TALK_S = 30.0   # a calibration minute needs 30+ s of him talking; otherwise
                                # (a mic check, a few words) it restarts at his next phrase
MIN_WORDS = _cfg("MIN_WORDS_PER_MINUTE", 15)            # spec: skip minutes under 15 words
WPM_FULL_DROP = _cfg("WPM_FULL_DROP", 0.28)             # case: 165 -> 118 wpm
CLARITY_FULL_DROP = _cfg("CLARITY_FULL_DROP", 0.07)     # case: 98 -> 91 % intelligibility
SMOOTH_PREV = _cfg("FATIGUE_SMOOTH_PREV", 0.6)          # spec: 0.6 * prev + 0.4 * raw
TIRING_AT = _cfg("TIRING_AT", 0.35)
RECAP_AT = _cfg("RECAP_AT", 0.70)
AGREE_MINUTES = _cfg("STATE_AGREE_MINUTES", 2)          # spec: 2 minutes in a row agree
PASSAGE_TEXT = _cfg("PASSAGE_TEXT", None)
FINALS_LOG = _cfg("FINALS_LOG", None)   # e.g. "finals.jsonl": saves every stt.final for --replay
MODE = _cfg("FATIGUE_MODE", "passage" if PASSAGE_TEXT else "class")
CLOSE_GRACE_S = 5.0                                     # wait for late finals before closing
LEVEL = {"fresh": 0, "tiring": 1, "recap_ready": 2}
STATES = ["fresh", "tiring", "recap_ready"]
PASSAGE_MIN_TOKENS = 300                                # tile short passages (> 1 min of reading)


class FatigueTracker:
    def __init__(self, publish, mode=MODE, passage_text=PASSAGE_TEXT, tz=TZ):
        self.publish = publish
        self.mode = mode
        self.tz = tz
        self.lock = threading.RLock()
        self.ref = self._tile(passage_text) if passage_text else None
        if self.mode == "passage" and not self.ref:
            # never crash app.py (captions must keep running): fall back to class mode
            print("fatigue.py: passage mode without PASSAGE_TEXT -> using class mode")
            self.mode = "class"
        self.state = "fresh"
        self._new_session()

    def _new_session(self):
        """Forget the baseline and the score: calibrate from his next minute of talking."""
        self.t_start = None      # his first word; minutes are counted from here
        self.minute = None       # index of the open minute (0 = his first minute)
        self.finals = []
        self.fatigue = 0.0
        self.candidates = []
        self.collect = []        # minutes collected for the calibration
        self.base = None         # {"wpm", "conf", "acc"?} from his first minute of talking

    # ---------- bus handlers ----------
    def on_final(self, ev):
        words = [w for w in (ev or {}).get("words", [])
                 if not str(w.get("word", "")).startswith("[")]
        if not words:
            return
        t0 = ev.get("t0", words[0]["start"])
        t1 = ev.get("t1", words[-1]["end"])
        with self.lock:
            if self.minute is not None and self._index(t1) > self.minute:
                self._close_minute()                  # (may restart the calibration minute)
            if self.t_start is None:
                self.t_start = t0                     # his first minute starts here
            if self.minute is None:
                self.minute = self._index(t1)
            self.finals.append({"t0": t0, "t1": t1, "words": words})

    def _index(self, t):
        """Which minute of his talking t falls in (0 = his first minute)."""
        return int(max(0.0, t - self.t_start) // WINDOW_S)

    def tick(self, now=None):
        """Called every few seconds: closes the minute once it is over (+ a short grace)."""
        now = time.time() if now is None else now
        with self.lock:
            if (self.minute is not None
                    and now >= self.t_start + (self.minute + 1) * WINDOW_S + CLOSE_GRACE_S):
                self.flush()

    def flush(self):
        with self.lock:
            self._close_minute()

    def on_debug_fatigue(self, data):
        level = float((data or {}).get("level", 0.0))
        with self.lock:
            self.fatigue = level
            cand = self._candidate()
            self.candidates = [cand] * AGREE_MINUTES
            self._set_state(cand)

    def on_debug_reset(self, _data=None):
        with self.lock:
            self._new_session()
            self._set_state("fresh")
        print("[fatigue] reset: calibrating again from his next minute of talking")

    # ---------- core ----------
    def _close_minute(self):
        finals, self.finals = self.finals, []
        index, self.minute = self.minute, None
        if not finals or index is None:
            return
        ts = self.t_start + (index + 1) * WINDOW_S            # end of this minute

        words = [w for f in finals for w in f["words"]]
        n = len(words)
        speaking = sum(max(0.0, f["t1"] - f["t0"]) for f in finals)
        if self.base is None and (n < MIN_WORDS or speaking < CALIBRATION_MIN_TALK_S):
            # not a real minute of talking yet: his first minute starts at his next phrase
            print(f"[fatigue] calibration: only {speaking:.0f} s of talking in that minute, "
                  f"starting the calibration minute again at his next phrase")
            self.t_start = None
            return
        if n < MIN_WORDS or speaking <= 0:
            return                                   # he isn't talking: skip the minute

        wpm = n / (speaking / 60.0)
        conf = mean(float(w.get("conf", 0.0)) for w in words)
        pause_ratio = max(0.0, 1.0 - speaking / WINDOW_S)
        art = sum(max(0.0, w["end"] - w["start"]) for w in words)
        syl = sum(syllables(w["word"]) for w in words)
        ms_per_syll = 1000.0 * art / syl if syl else None   # logged, not scored yet
        acc = self._accuracy(words) if self.mode == "passage" else None
        minute = {"wpm": wpm, "conf": conf, "acc": acc}

        if self.base is None:                        # calibrating on his first minute
            self.collect.append(minute)
            if len(self.collect) >= CALIBRATION_MINUTES:
                self.base = self._medians()
                self.collect = []
                extra = "" if "acc" not in self.base else f", accuracy {self.base['acc']:.2f}"
                print(f"[fatigue] calibrated on his first minute of talking: "
                      f"{self.base['wpm']:.0f} wpm, conf {self.base['conf']:.2f}{extra}")
        else:
            raw = self._raw(minute, self.base)
            self.fatigue = SMOOTH_PREV * self.fatigue + (1.0 - SMOOTH_PREV) * raw

        cand = self._candidate()
        self.candidates = (self.candidates + [cand])[-AGREE_MINUTES:]
        if len(self.candidates) == AGREE_MINUTES:
            # Spec: change only after 2 minutes in a row agree. Read as "agree on the
            # direction". Going up it moves ONE step per minute, so amber (and the armband's
            # "tiring" buzz) always comes before purple, even with config.py's 1-minute demo
            # value; going down it moves to the higher of the minutes' states.
            cur = LEVEL[self.state]
            levels = [LEVEL[c] for c in self.candidates]
            if all(lv > cur for lv in levels):
                self._set_state(STATES[cur + 1])
            elif all(lv < cur for lv in levels):
                self._set_state(max(self.candidates, key=LEVEL.get))

        local = self._local(ts)
        self.publish("metrics.minute", {
            "ts": ts, "wpm": round(wpm, 1), "mean_conf": round(conf, 3),
            "pause_ratio": round(pause_ratio, 3), "n_words": n,
            "fatigue": round(self.fatigue, 3), "state": self.state,
            "mode": self.mode,
            "accuracy": None if acc is None else round(acc, 3),
            "ms_per_syll": None if ms_per_syll is None else round(ms_per_syll, 1),
            "day_part": "am" if local.hour < 12 else "pm",   # database label only
            # for the control panel ("X wpm (baseline Y)"); store.py ignores these two
            "baseline_wpm": None if self.base is None else round(self.base["wpm"], 1),
            "baseline_conf": None if self.base is None else round(self.base["conf"], 3),
        })

    def _raw(self, m, base):
        wpm_drop = 0.0
        if base.get("wpm"):
            wpm_drop = max(0.0, 1.0 - m["wpm"] / base["wpm"]) / WPM_FULL_DROP
        if self.mode == "passage" and m["acc"] is not None and base.get("acc"):
            clarity_drop = max(0.0, (base["acc"] - m["acc"]) / base["acc"]) / CLARITY_FULL_DROP
        elif base.get("conf"):
            clarity_drop = max(0.0, (base["conf"] - m["conf"]) / base["conf"]) / CLARITY_FULL_DROP
        else:
            clarity_drop = 0.0
        return 0.5 * min(1.0, wpm_drop) + 0.5 * min(1.0, clarity_drop)

    def _accuracy(self, words):
        """Share of the passage words read this minute that Vosk got right (SIT-style),
        aligned with free start/end in the passage (see align_segment above)."""
        hyp = [t[0] for t in (norm_tokens(w["word"]) for w in words) if t]
        acc = align_segment(self.ref, hyp)["accuracy"]
        return 0.0 if acc is None else acc

    def _candidate(self):
        """The voice only: no time-of-day rule."""
        if self.fatigue >= RECAP_AT:
            return "recap_ready"
        return "tiring" if self.fatigue >= TIRING_AT else "fresh"

    def _set_state(self, cand):
        if cand != self.state:
            self.state = cand
            self.publish("state.changed", {"state": cand})

    def _medians(self):
        b = {"wpm": median(m["wpm"] for m in self.collect),
             "conf": median(m["conf"] for m in self.collect),
             "n_minutes": len(self.collect)}
        accs = [m["acc"] for m in self.collect if m["acc"] is not None]
        if accs:
            b["acc"] = median(accs)
        return b

    def _local(self, ts):
        return datetime.fromtimestamp(ts, self.tz) if self.tz else datetime.fromtimestamp(ts)

    @staticmethod
    def _tile(text):
        toks = norm_tokens(text or "")
        if not toks:
            return None
        return toks * max(1, -(-PASSAGE_MIN_TOKENS // len(toks)))


# ---------- wiring for app.py ----------
_tracker = None


def start():
    """Called once by app.py."""
    import bus
    global _tracker
    _tracker = FatigueTracker(bus.publish)
    bus.subscribe("stt.final", _tracker.on_final)
    bus.subscribe("debug.fatigue", _tracker.on_debug_fatigue)
    bus.subscribe("debug.reset", _tracker.on_debug_reset)

    if FINALS_LOG:  # record real Vosk output to replay it later (--replay)
        log_lock = threading.Lock()

        def _log_final(ev):
            with log_lock, open(FINALS_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev) + "\n")

        bus.subscribe("stt.final", _log_final)

    def _ticker():
        while True:
            time.sleep(2)
            _tracker.tick()

    threading.Thread(target=_ticker, daemon=True).start()
    print(f"[fatigue] {_tracker.mode} mode: calibrates on his first {CALIBRATION_MINUTES} min "
          f"of talking, then one score per minute; state changes after {AGREE_MINUTES} min, "
          f"smoothing {SMOOTH_PREV}; time of day not used")
    return _tracker


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Replay recorded stt.final events (JSON lines).")
    ap.add_argument("--replay", required=True, help="file with one stt.final payload per line")
    ap.add_argument("--mode", choices=["class", "passage"], default=MODE)
    ap.add_argument("--passage", help="text file with the passage (passage mode)")
    args = ap.parse_args()

    passage = PASSAGE_TEXT
    if args.passage:
        with open(args.passage, encoding="utf-8") as f:
            passage = f.read()
    tracker = FatigueTracker(lambda topic, data: print(topic, json.dumps(data)),
                             mode=args.mode, passage_text=passage)
    with open(args.replay, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                tracker.on_final(json.loads(line))
    tracker.flush()