"""fatigue.py - per-minute speech metrics + fatigue score (Daniel).

Runs on the LAPTOP. The Arduino 101 only drives the touch pad / LCD / accelerometer,
and hub.py (Nihal) relays state.changed to it. This module never touches hardware and
talks to the rest of the app only through bus.py.

Subscribes
  stt.final      {text, words: [{word, start, end, conf}], t0, t1}   t0/t1 = wall-clock s
  recap.status   {phase, ...}     on phase "done" the late-day recap counter restarts
  debug.fatigue  {level: float}   ASSUMED topic behind POST /debug/fatigue (agree with Yasmin)
  debug.reset    None             ASSUMED topic behind POST /debug/reset   (agree with Yasmin)
Publishes
  metrics.minute {ts, wpm, mean_conf, pause_ratio, n_words, fatigue, state,
                  mode, accuracy, ms_per_syll, day_part}
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
  python fatigue.py --replay morning.jsonl --make-baseline     (fixed baseline for the demo)
"""
import json
import re
import threading
import time
from datetime import datetime
from datetime import time as dtime
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


# Hamilton time, explicitly: the laptop running app.py may be set to another zone.
TZ_NAME = _cfg("TZ_NAME", "America/Toronto")
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(TZ_NAME)
except Exception:  # Windows needs `pip install tzdata`; falls back to the laptop's clock
    TZ = None
    print(f"fatigue.py: no time-zone data for {TZ_NAME}; using the laptop's local time "
          "(on Windows: pip install tzdata)")

BASELINE_FILE = _cfg("BASELINE_FILE", "baseline.json")
BASELINE_MINUTES = _cfg("BASELINE_MINUTES", 5)          # spec: first 5 talking minutes
MIN_WORDS = _cfg("MIN_WORDS_PER_MINUTE", 15)            # spec: skip minutes under 15 words
WPM_FULL_DROP = _cfg("WPM_FULL_DROP", 0.28)             # case: 165 -> 118 wpm
CLARITY_FULL_DROP = _cfg("CLARITY_FULL_DROP", 0.07)     # case: 98 -> 91 % intelligibility
SMOOTH_PREV = _cfg("FATIGUE_SMOOTH_PREV", 0.6)          # spec: 0.6 * prev + 0.4 * raw
TIRING_AT = _cfg("TIRING_AT", 0.35)
RECAP_AT = _cfg("RECAP_AT", 0.70)
LATE_RECAP_TIME = _cfg("LATE_RECAP_TIME", "14:30")      # spec: after 2:30 PM ...
LATE_RECAP_MIN_MINUTES = _cfg("LATE_RECAP_MIN_MINUTES", 10)  # ... with 10+ minutes of transcript
AGREE_MINUTES = _cfg("STATE_AGREE_MINUTES", 2)          # spec: 2 minutes in a row agree
PASSAGE_TEXT = _cfg("PASSAGE_TEXT", None)
FINALS_LOG = _cfg("FINALS_LOG", None)   # e.g. "finals.jsonl": saves every stt.final for --replay
MODE = _cfg("FATIGUE_MODE", "passage" if PASSAGE_TEXT else "class")
CLOSE_GRACE_S = 5.0                                     # wait for late finals before closing
LEVEL = {"fresh": 0, "tiring": 1, "recap_ready": 2}
PASSAGE_MIN_TOKENS = 300                                # tile short passages (> 1 min of reading)


class FatigueTracker:
    def __init__(self, publish, mode=MODE, passage_text=PASSAGE_TEXT,
                 baseline_file=BASELINE_FILE, tz=TZ, collect_only=False):
        self.publish = publish
        self.mode = mode
        self.tz = tz
        self.baseline_file = baseline_file
        self.collect_only = collect_only
        self.lock = threading.RLock()
        self.ref = self._tile(passage_text) if passage_text else None
        if self.mode == "passage" and not self.ref:
            # never crash app.py (captions must keep running): fall back to class mode
            print("fatigue.py: passage mode without PASSAGE_TEXT -> using class mode")
            self.mode = "class"
        self.minute = None
        self.finals = []
        self.fatigue = 0.0
        self.state = "fresh"
        self.candidates = []
        self.minutes_since_recap = 0
        self.day = None
        self.collect = []
        self.baseline = self._load_baseline()

    # ---------- bus handlers ----------
    def on_final(self, ev):
        words = [w for w in (ev or {}).get("words", [])
                 if not str(w.get("word", "")).startswith("[")]
        if not words:
            return
        t0 = ev.get("t0", words[0]["start"])
        t1 = ev.get("t1", words[-1]["end"])
        m = int(t1 // 60)
        with self.lock:
            if self.minute is not None and m > self.minute:
                self._close_minute()
            if self.minute is None or m > self.minute:
                self.minute = m
            self.finals.append({"t0": t0, "t1": t1, "words": words})

    def tick(self, now=None):
        """Called every few seconds: closes the minute once he stops talking."""
        now = time.time() if now is None else now
        with self.lock:
            if self.minute is not None and now >= (self.minute + 1) * 60 + CLOSE_GRACE_S:
                self.flush()

    def flush(self):
        with self.lock:
            self._close_minute()
            self.minute = None

    def on_recap_status(self, data):
        if (data or {}).get("phase") != "done":
            return
        with self.lock:
            self.minutes_since_recap = 0
            self.candidates = []
            cand = self._candidate(late_ok=False)
            self._set_state(cand)

    def on_debug_fatigue(self, data):
        level = float((data or {}).get("level", 0.0))
        with self.lock:
            self.fatigue = level
            cand = self._candidate(late_ok=False)
            self.candidates = [cand] * AGREE_MINUTES
            self._set_state(cand)

    def on_debug_reset(self, _data=None):
        with self.lock:
            self.fatigue = 0.0
            self.candidates = []
            self.minutes_since_recap = 0
            self._set_state("fresh")

    # ---------- core ----------
    def _close_minute(self):
        finals, self.finals = self.finals, []
        if not finals or self.minute is None:
            return
        ts = (self.minute + 1) * 60
        local = self._local(ts)
        today = local.date().isoformat()
        if today != self.day:
            self._new_day(today)

        words = [w for f in finals for w in f["words"]]
        n = len(words)
        speaking = sum(max(0.0, f["t1"] - f["t0"]) for f in finals)
        if n < MIN_WORDS or speaking <= 0:
            return                                   # he isn't talking: skip the minute

        wpm = n / (speaking / 60.0)
        conf = mean(float(w.get("conf", 0.0)) for w in words)
        pause_ratio = max(0.0, 1.0 - speaking / 60.0)
        art = sum(max(0.0, w["end"] - w["start"]) for w in words)
        syl = sum(syllables(w["word"]) for w in words)
        ms_per_syll = 1000.0 * art / syl if syl else None   # logged, not scored yet
        acc = self._accuracy(words) if self.mode == "passage" else None
        minute = {"wpm": wpm, "conf": conf, "acc": acc}

        self.minutes_since_recap += 1
        base = None if self.collect_only else self._base(today)
        if base is None:
            self.collect.append(minute)
            if not self.collect_only and len(self.collect) >= BASELINE_MINUTES:
                self._save_baseline(today)
        else:
            raw = self._raw(minute, base)
            self.fatigue = SMOOTH_PREV * self.fatigue + (1.0 - SMOOTH_PREV) * raw

        cand = self._candidate(late_ok=True, local=local)
        self.candidates = (self.candidates + [cand])[-AGREE_MINUTES:]
        if len(self.candidates) == AGREE_MINUTES:
            # Spec: change only after 2 minutes in a row agree. Read as "agree on the
            # direction": if both minutes are above the current state, move to the lower of
            # the two (so a fast rise still shows amber before purple); same going down.
            cur = LEVEL[self.state]
            levels = [LEVEL[c] for c in self.candidates]
            if all(lv > cur for lv in levels):
                self._set_state(min(self.candidates, key=LEVEL.get))
            elif all(lv < cur for lv in levels):
                self._set_state(max(self.candidates, key=LEVEL.get))

        self.publish("metrics.minute", {
            "ts": ts, "wpm": round(wpm, 1), "mean_conf": round(conf, 3),
            "pause_ratio": round(pause_ratio, 3), "n_words": n,
            "fatigue": round(self.fatigue, 3), "state": self.state,
            "mode": self.mode,
            "accuracy": None if acc is None else round(acc, 3),
            "ms_per_syll": None if ms_per_syll is None else round(ms_per_syll, 1),
            "day_part": "am" if local.hour < 12 else "pm",
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

    def _candidate(self, late_ok, local=None):
        if self.fatigue >= RECAP_AT:
            return "recap_ready"
        if late_ok and local is not None:
            hh, mm = (int(x) for x in LATE_RECAP_TIME.split(":"))
            if local.time() >= dtime(hh, mm) and self.minutes_since_recap >= LATE_RECAP_MIN_MINUTES:
                return "recap_ready"
        return "tiring" if self.fatigue >= TIRING_AT else "fresh"

    def _set_state(self, cand):
        if cand != self.state:
            self.state = cand
            self.publish("state.changed", {"state": cand})

    # ---------- days and baseline ----------
    def _local(self, ts):
        return datetime.fromtimestamp(ts, self.tz) if self.tz else datetime.fromtimestamp(ts)

    def _new_day(self, today):
        self.day = today
        self.fatigue = 0.0
        self.candidates = []
        self.minutes_since_recap = 0
        self.collect = []
        self._set_state("fresh")

    def _base(self, today):
        b = self.baseline
        if b.get("fixed") or b.get("date") == today:
            return b.get(self.mode)
        return None

    def _load_baseline(self):
        try:
            with open(self.baseline_file) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _medians(self):
        b = {"wpm": median(m["wpm"] for m in self.collect),
             "conf": median(m["conf"] for m in self.collect),
             "n_minutes": len(self.collect)}
        accs = [m["acc"] for m in self.collect if m["acc"] is not None]
        if accs:
            b["acc"] = median(accs)
        return b

    def _save_baseline(self, today, fixed=False):
        keep = self.baseline.get("fixed") or self.baseline.get("date") == today
        data = dict(self.baseline) if keep else {}
        data.update({"date": today, "fixed": fixed or data.get("fixed", False),
                     self.mode: self._medians()})
        self.baseline = data
        try:
            with open(self.baseline_file, "w") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass
        self.collect = []

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
    bus.subscribe("recap.status", _tracker.on_recap_status)
    bus.subscribe("debug.fatigue", _tracker.on_debug_fatigue)
    bus.subscribe("debug.reset", _tracker.on_debug_reset)

    if FINALS_LOG:  # record real Vosk output to replay it later (--replay / --make-baseline)
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
    return _tracker


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Replay recorded stt.final events (JSON lines).")
    ap.add_argument("--replay", required=True, help="file with one stt.final payload per line")
    ap.add_argument("--mode", choices=["class", "passage"], default=MODE)
    ap.add_argument("--passage", help="text file with the passage (passage mode)")
    ap.add_argument("--make-baseline", action="store_true",
                    help="save this recording's medians as a fixed baseline.json for the demo")
    args = ap.parse_args()

    passage = PASSAGE_TEXT
    if args.passage:
        with open(args.passage, encoding="utf-8") as f:
            passage = f.read()
    tracker = FatigueTracker(lambda topic, data: print(topic, json.dumps(data)),
                             mode=args.mode, passage_text=passage,
                             collect_only=args.make_baseline)
    with open(args.replay, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                tracker.on_final(json.loads(line))
    tracker.flush()
    if args.make_baseline:
        if not tracker.collect:
            raise SystemExit("no talking minutes (15+ words) in the recording")
        tracker._save_baseline(tracker.day, fixed=True)
        print("fixed baseline saved:", json.dumps(tracker.baseline[args.mode]))
