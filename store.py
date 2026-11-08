"""store.py - local storage (SQLite) + metrics sync to Tiger Data.

Spec: "store.py". This file has both, so app.py only calls
store.start(). Runs on the LAPTOP (the Arduino 101 replaced the Raspberry Pi).

Subscribes
  stt.final       -> finals(t0, t1, text, mean_conf)      the transcript never leaves the laptop
  metrics.minute  -> metrics(..., synced = 0)
  debug.reset     -> deletes today's finals (spec: "clears today's transcript"). ASSUMED topic
                     name, the same one fatigue.py listens to; (web.py).
Functions other modules may call (spec)
  store.transcript_since(ts) -> str          recap.py
  store.metrics_today()      -> list[dict]
Background thread
  Every SYNC_INTERVAL_S (60 s) pushes unsynced metrics to Tiger Data (speech_metrics), then
  marks them synced. No network or no TIGER_DSN: everything stays local and is retried later.
  Only metrics leave the laptop, never audio or transcript (privacy point for the judges).

Check the connection by hand:  python store.py --check
"""
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from statistics import mean

try:
    import config
except ImportError:
    config = None

try:
    import psycopg
except ImportError:  # local storage still works; sync is disabled
    psycopg = None


def _cfg(name, default):
    return getattr(config, name, default) if config is not None else default


DB_PATH = _cfg("DB_PATH", "stillme.db")
TIGER_DSN = _cfg("TIGER_DSN", None)
PATIENT_ID = _cfg("PATIENT_ID", "alex")
SESSION_ID = _cfg("SESSION_ID", None) or (
    datetime.now().strftime("%Y%m%d-%H%M") + "-" + uuid.uuid4().hex[:4])
SYNC_INTERVAL_S = _cfg("SYNC_INTERVAL_S", 60)
SYNC_BATCH = 500
TZ_NAME = _cfg("TZ_NAME", "America/Toronto")
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(TZ_NAME)
except Exception:  # Windows without tzdata -> laptop clock
    TZ = None

METRIC_COLS = ["ts", "wpm", "mean_conf", "pause_ratio", "n_words", "fatigue", "state",
               "mode", "accuracy", "ms_per_syll", "day_part"]

# Same tables as the spec + the 4 columns fatigue.py sends (this replaces sqlite_schema.sql).
SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS finals (t0 REAL, t1 REAL, text TEXT, mean_conf REAL);
CREATE INDEX IF NOT EXISTS finals_t1 ON finals (t1);
CREATE TABLE IF NOT EXISTS metrics (
  ts REAL, wpm REAL, mean_conf REAL, pause_ratio REAL, n_words INTEGER,
  fatigue REAL, state TEXT, mode TEXT, accuracy REAL, ms_per_syll REAL, day_part TEXT,
  synced INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS metrics_unsynced ON metrics (synced, ts);
"""

# ON CONFLICT needs the unique index in tiger_schema.sql: a retry never duplicates a minute.
TIGER_INSERT = """
INSERT INTO speech_metrics (time, patient_id, session_id, wpm, mean_conf, pause_ratio, n_words,
                            fatigue, state, mode, accuracy, ms_per_syll, day_part)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (patient_id, time) DO NOTHING
"""


class Store:
    def __init__(self, db_path=DB_PATH, dsn=TIGER_DSN, patient_id=PATIENT_ID,
                 session_id=SESSION_ID, tz=TZ):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SQLITE_SCHEMA)
            self.db.commit()
        self.dsn, self.patient_id, self.session_id, self.tz = dsn, patient_id, session_id, tz
        self.last_sync_error = None
        self.last_sync_ok = None

    # ---------- bus handlers ----------
    def on_final(self, ev):
        ev = ev or {}
        words = [w for w in ev.get("words", []) if not str(w.get("word", "")).startswith("[")]
        text = (ev.get("text") or " ".join(w["word"] for w in words)).strip()
        if not text:
            return
        t0 = ev.get("t0", words[0]["start"] if words else time.time())
        t1 = ev.get("t1", words[-1]["end"] if words else t0)
        conf = mean(float(w.get("conf", 0.0)) for w in words) if words else None
        with self.lock:
            self.db.execute("INSERT INTO finals (t0, t1, text, mean_conf) VALUES (?, ?, ?, ?)",
                            (t0, t1, text, conf))
            self.db.commit()

    def on_metrics(self, m):
        m = m or {}
        with self.lock:
            self.db.execute(
                f"INSERT INTO metrics ({', '.join(METRIC_COLS)}) "
                f"VALUES ({', '.join('?' for _ in METRIC_COLS)})",
                [m.get(c) for c in METRIC_COLS])
            self.db.commit()

    def on_debug_reset(self, _data=None):
        with self.lock:
            self.db.execute("DELETE FROM finals WHERE t1 >= ?", (self._start_of_today(),))
            self.db.commit()

    # ---------- functions other modules call ----------
    def transcript_since(self, ts):
        with self.lock:
            rows = self.db.execute("SELECT text FROM finals WHERE t1 >= ? ORDER BY t0",
                                   (ts,)).fetchall()
        return " ".join(r["text"] for r in rows)

    def metrics_today(self):
        with self.lock:
            rows = self.db.execute(
                f"SELECT {', '.join(METRIC_COLS)} FROM metrics WHERE ts >= ? ORDER BY ts",
                (self._start_of_today(),)).fetchall()
        return [dict(r) for r in rows]

    # ---------- sync to Tiger Data ----------
    def sync_once(self):
        """Push one batch of unsynced minutes. Returns how many were confirmed."""
        if not self.dsn or psycopg is None:
            return 0
        with self.lock:
            rows = self.db.execute(
                "SELECT rowid AS rid, * FROM metrics WHERE synced = 0 ORDER BY ts LIMIT ?",
                (SYNC_BATCH,)).fetchall()
        if not rows:
            return 0
        payload = [(datetime.fromtimestamp(r["ts"], timezone.utc), self.patient_id,
                    self.session_id, r["wpm"], r["mean_conf"], r["pause_ratio"], r["n_words"],
                    r["fatigue"], r["state"], r["mode"], r["accuracy"], r["ms_per_syll"],
                    r["day_part"]) for r in rows]
        try:  # network I/O outside the lock, so bus handlers never wait on Tiger Data
            with psycopg.connect(self.dsn, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.executemany(TIGER_INSERT, payload)
        except Exception as e:  # offline, wrong DSN, schema missing...
            if repr(e) != self.last_sync_error:
                print(f"store.py: sync failed, will retry: {e}")
            self.last_sync_error = repr(e)
            return 0
        with self.lock:
            self.db.executemany("UPDATE metrics SET synced = 1 WHERE rowid = ?",
                                [(r["rid"],) for r in rows])
            self.db.commit()
        self.last_sync_error, self.last_sync_ok = None, time.time()
        return len(rows)

    def _sync_loop(self):
        while True:
            time.sleep(SYNC_INTERVAL_S)
            while self.sync_once() == SYNC_BATCH:   # catch up after being offline
                pass

    def counts(self):
        with self.lock:
            q = self.db.execute
            return {"finals": q("SELECT count(*) FROM finals").fetchone()[0],
                    "metrics": q("SELECT count(*) FROM metrics").fetchone()[0],
                    "unsynced": q("SELECT count(*) FROM metrics WHERE synced = 0").fetchone()[0]}

    def _start_of_today(self):
        now = datetime.now(self.tz) if self.tz else datetime.now()
        return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


# ---------- wiring for app.py and the functions the spec exposes ----------
_store = None


def start():
    """Called once by app.py."""
    import bus
    global _store
    _store = Store()
    bus.subscribe("stt.final", _store.on_final)
    bus.subscribe("metrics.minute", _store.on_metrics)
    bus.subscribe("debug.reset", _store.on_debug_reset)
    if not TIGER_DSN:
        print("store.py: no TIGER_DSN in config, metrics stay local only")
    elif psycopg is None:
        print('store.py: psycopg missing (pip install "psycopg[binary]"), metrics stay local')
    threading.Thread(target=_store._sync_loop, daemon=True).start()
    return _store


def transcript_since(ts):
    return _store.transcript_since(ts) if _store else ""


def metrics_today():
    return _store.metrics_today() if _store else []


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Check the local DB and the Tiger Data sync.")
    ap.add_argument("--check", action="store_true", help="show counts and try one sync now")
    args = ap.parse_args()
    s = Store()
    print("local:", s.counts(), "| patient:", s.patient_id, "| db:", DB_PATH)
    if args.check:
        if not TIGER_DSN:
            raise SystemExit("no TIGER_DSN in config.py")
        n = s.sync_once()
        print(f"synced now: {n}", "| error:", s.last_sync_error or "none")
