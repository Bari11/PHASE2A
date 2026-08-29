"""
core/history_db.py — Canary Phase 4 persistent storage
═══════════════════════════════════════════════════════════════════════════
SQLite-backed replacement for the old flat-JSON `~/.canary_history.json`.
Deliberately just a data layer — no Qt, no dashboard code — so it can be
unit-tested and reused from tray_app.py without dragging in UI imports.

Two tables:

  sessions    — one row per COMPLETED monitoring session (Start Session →
                Stop Session). This is the "session history" the Dashboard's
                Session Results tab and hourly/weekly/monthly/trend views
                are built from. Includes the Phase-3 orchestration counters
                (meaningful alerts, Horizon/Calm/Unplug usage during that
                session) alongside the existing detector-derived metrics.

  mode_usage  — one row per Horizon/Calm/Unplug RUN, whether it happened
                during a session or as a standalone manual trigger. This is
                what the Mode Statistics tab reads — it deliberately does
                NOT require a session to exist, since standalone usage is a
                first-class, explicitly-supported way to use those three
                modes (see Phase 3) and must be visible in their stats too.

Design notes:
  - Events (the per-alert timeline already embedded in SessionData) are
    kept as a single JSON blob column rather than a child table — nothing
    else needs to query into individual events beyond display, and a
    second table would just be normalized duplication for no benefit here.
  - All public functions catch their own exceptions and degrade to a safe
    empty/no-op result rather than raising, so a first-launch/no-database,
    interrupted-write, or corrupted-file situation never crashes the app.
    A corrupted database file is backed up (renamed) and a fresh one is
    created in its place, rather than the app failing to start.
"""

import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import List, Optional

DB_PATH = Path.home() / '.canary_history.db'

# The old Phase-1/2/3 flat-file history, migrated in once, on first init,
# if present and the new DB has no sessions yet. Not deleted afterwards
# (renamed .migrated) — just no longer written to.
_LEGACY_JSON_PATH = Path.home() / '.canary_history.json'


# ─────────────────────────────────────────────────────────────────────────
#  Connection handling (with corruption recovery)
# ─────────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time                  REAL NOT NULL,
    end_time                    REAL NOT NULL,
    duration_min                REAL,
    good_posture_pct            REAL,
    avg_stress                  REAL,
    avg_posture                 REAL,
    avg_blink_rate              REAL,
    posture_alerts              INTEGER,
    stress_alerts               INTEGER,
    appreciation_alerts         INTEGER,
    water_alerts                INTEGER,
    meaningful_alerts           INTEGER,
    horizon_uses                INTEGER,
    horizon_secs                REAL,
    calm_uses                   INTEGER,
    calm_secs                   REAL,
    unplug_uses                 INTEGER,
    unplug_secs                 REAL,
    unplug_exercises_completed  INTEGER,
    events_json                 TEXT
);

CREATE TABLE IF NOT EXISTS mode_usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mode            TEXT NOT NULL,      -- 'horizon' | 'calm' | 'unplug'
    start_time      REAL NOT NULL,
    end_time        REAL NOT NULL,
    duration_secs   REAL,
    during_session  INTEGER,            -- 1 if a monitoring session was active when this run started
    completed       INTEGER,            -- 1 if the mode's own finished/completion signal fired normally
    extra_json      TEXT                -- mode-specific extras, e.g. Unplug's exercise results
);

CREATE INDEX IF NOT EXISTS idx_sessions_start   ON sessions(start_time);
CREATE INDEX IF NOT EXISTS idx_mode_usage_mode  ON mode_usage(mode, start_time);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _recover_corrupted_db():
    """A file exists at DB_PATH but SQLite can't read it as a database.
    Move it aside rather than losing the app to a crash loop."""
    try:
        backup = DB_PATH.with_suffix(f'.corrupted-{int(time.time())}.db')
        shutil.move(str(DB_PATH), str(backup))
        print(f'[Canary] History database was unreadable — moved to {backup}')
    except Exception as e:
        print(f'[Canary] Could not back up corrupted history database: {e}')


def init_db():
    """
    Create the database/tables if this is a first launch, recover from a
    corrupted file if needed, and — once — migrate any pre-Phase-4 JSON
    history into the sessions table. Safe to call every startup.
    """
    try:
        conn = _connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        _recover_corrupted_db()
        try:
            conn = _connect()
            conn.executescript(_SCHEMA)
            conn.commit()
            conn.close()
        except Exception as e:
            print(f'[Canary] Could not create history database: {e}')
            return
    except Exception as e:
        print(f'[Canary] Could not open history database: {e}')
        return

    _migrate_legacy_json_once()


def _migrate_legacy_json_once():
    """One-time import of ~/.canary_history.json (Phase 1-3's flat-file
    history) into the sessions table, so upgrading to Phase 4 doesn't
    silently lose existing history. Only runs if the DB has no sessions
    yet and the legacy file hasn't already been migrated."""
    if not _LEGACY_JSON_PATH.exists():
        return
    try:
        if fetch_session_count() > 0:
            return  # DB already has data — don't re-import/duplicate
        legacy = json.loads(_LEGACY_JSON_PATH.read_text())
        if not isinstance(legacy, list) or not legacy:
            return
        conn = _connect()
        try:
            for s in legacy:
                conn.execute(
                    """INSERT INTO sessions
                       (start_time, end_time, duration_min, good_posture_pct,
                        avg_stress, avg_posture, avg_blink_rate,
                        posture_alerts, stress_alerts, appreciation_alerts, water_alerts,
                        meaningful_alerts, horizon_uses, horizon_secs,
                        calm_uses, calm_secs, unplug_uses, unplug_secs,
                        unplug_exercises_completed, events_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        s.get('start', 0.0), s.get('end', 0.0), s.get('duration_min', 0.0),
                        s.get('good_posture_pct', 0.0), s.get('avg_stress', 0.0),
                        s.get('avg_posture', 0.0), s.get('avg_blink_rate', 0.0),
                        s.get('posture_alerts', 0), s.get('stress_alerts', 0),
                        s.get('appreciation', 0), s.get('water', 0),
                        # These four Phase-3 fields didn't exist pre-Phase-4 —
                        # 0 rather than fabricating a value for old sessions.
                        0, 0, 0.0, 0, 0.0, 0, 0.0, 0,
                        json.dumps(s.get('events', [])),
                    )
                )
            conn.commit()
        finally:
            conn.close()
        _LEGACY_JSON_PATH.rename(_LEGACY_JSON_PATH.with_suffix('.json.migrated'))
        print(f'[Canary] Migrated {len(legacy)} session(s) from the old JSON history file.')
    except Exception as e:
        print(f'[Canary] Legacy history migration skipped (non-fatal): {e}')


# ─────────────────────────────────────────────────────────────────────────
#  Sessions
# ─────────────────────────────────────────────────────────────────────────

def insert_session(row: dict) -> Optional[int]:
    """Insert one completed session. Returns the new row id, or None on
    failure (caller already has the data in memory either way)."""
    try:
        conn = _connect()
        try:
            cur = conn.execute(
                """INSERT INTO sessions
                   (start_time, end_time, duration_min, good_posture_pct,
                    avg_stress, avg_posture, avg_blink_rate,
                    posture_alerts, stress_alerts, appreciation_alerts, water_alerts,
                    meaningful_alerts, horizon_uses, horizon_secs,
                    calm_uses, calm_secs, unplug_uses, unplug_secs,
                    unplug_exercises_completed, events_json)
                   VALUES (:start,:end,:duration_min,:good_posture_pct,
                           :avg_stress,:avg_posture,:avg_blink_rate,
                           :posture_alerts,:stress_alerts,:appreciation,:water,
                           :meaningful_alerts,:horizon_uses,:horizon_secs,
                           :calm_uses,:calm_secs,:unplug_uses,:unplug_secs,
                           :unplug_exercises_completed,:events_json)""",
                {
                    'start': row.get('start', 0.0), 'end': row.get('end', 0.0),
                    'duration_min': row.get('duration_min', 0.0),
                    'good_posture_pct': row.get('good_posture_pct', 0.0),
                    'avg_stress': row.get('avg_stress', 0.0),
                    'avg_posture': row.get('avg_posture', 0.0),
                    'avg_blink_rate': row.get('avg_blink_rate', 0.0),
                    'posture_alerts': row.get('posture_alerts', 0),
                    'stress_alerts': row.get('stress_alerts', 0),
                    'appreciation': row.get('appreciation', 0),
                    'water': row.get('water', 0),
                    'meaningful_alerts': row.get('meaningful_alerts', 0),
                    'horizon_uses': row.get('horizon_uses', 0),
                    'horizon_secs': row.get('horizon_secs', 0.0),
                    'calm_uses': row.get('calm_uses', 0),
                    'calm_secs': row.get('calm_secs', 0.0),
                    'unplug_uses': row.get('unplug_uses', 0),
                    'unplug_secs': row.get('unplug_secs', 0.0),
                    'unplug_exercises_completed': row.get('unplug_exercises_completed', 0),
                    'events_json': json.dumps(row.get('events', [])),
                }
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception as e:
        print(f'[Canary] Could not save session to history database: {e}')
        return None


def _row_to_dict(r: sqlite3.Row) -> dict:
    """Shaped to match the pre-Phase-4 in-memory session dict (same key
    names: 'start'/'end'/'appreciation'/'water') so the existing
    SessionResultsPage/LiveDashboardPage code needs no changes."""
    try:
        events = json.loads(r['events_json']) if r['events_json'] else []
    except Exception:
        events = []  # a corrupted single record shouldn't break the whole list
    return {
        'id': r['id'], 'start': r['start_time'], 'end': r['end_time'],
        'duration_min': r['duration_min'], 'good_posture_pct': r['good_posture_pct'],
        'avg_stress': r['avg_stress'], 'avg_posture': r['avg_posture'],
        'avg_blink_rate': r['avg_blink_rate'],
        'posture_alerts': r['posture_alerts'], 'stress_alerts': r['stress_alerts'],
        'appreciation': r['appreciation_alerts'], 'water': r['water_alerts'],
        'meaningful_alerts': r['meaningful_alerts'],
        'horizon_uses': r['horizon_uses'], 'horizon_secs': r['horizon_secs'],
        'calm_uses': r['calm_uses'], 'calm_secs': r['calm_secs'],
        'unplug_uses': r['unplug_uses'], 'unplug_secs': r['unplug_secs'],
        'unplug_exercises_completed': r['unplug_exercises_completed'],
        'events': events,
    }


def fetch_sessions(limit: Optional[int] = None) -> List[dict]:
    """All sessions, oldest first (same ordering the old JSON list used) —
    so history[-1] is the most recent, history[-2] the one before, etc.,
    exactly as the existing dashboard code already expects.
    Returns [] on any failure — never raises."""
    try:
        conn = _connect()
        try:
            if limit:
                cur = conn.execute(
                    'SELECT * FROM sessions ORDER BY start_time DESC LIMIT ?', (limit,))
                rows = list(reversed(cur.fetchall()))
            else:
                cur = conn.execute('SELECT * FROM sessions ORDER BY start_time ASC')
                rows = cur.fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        print(f'[Canary] Could not read history database: {e}')
        return []


def fetch_session_count() -> int:
    try:
        conn = _connect()
        try:
            return conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
        finally:
            conn.close()
    except Exception:
        return 0


def clear_sessions() -> bool:
    """Deletes all rows from `sessions` only — never touches `mode_usage`
    (Mode Statistics is a separate view with no clear action requested)
    and has no interaction with an in-progress session, which lives
    entirely in memory (SessionData on the active detector) until it's
    stopped and inserted."""
    try:
        conn = _connect()
        try:
            conn.execute('DELETE FROM sessions')
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        print(f'[Canary] Could not clear history database: {e}')
        return False


# ─────────────────────────────────────────────────────────────────────────
#  Mode usage (Horizon / Calm / Unplug runs — session or standalone)
# ─────────────────────────────────────────────────────────────────────────

def insert_mode_usage(mode: str, start_time: float, end_time: float,
                       during_session: bool, completed: bool,
                       extra: Optional[dict] = None) -> Optional[int]:
    try:
        conn = _connect()
        try:
            cur = conn.execute(
                """INSERT INTO mode_usage
                   (mode, start_time, end_time, duration_secs, during_session, completed, extra_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (mode, start_time, end_time, max(end_time - start_time, 0.0),
                 1 if during_session else 0, 1 if completed else 0,
                 json.dumps(extra or {}))
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception as e:
        print(f'[Canary] Could not record {mode} usage: {e}')
        return None


def fetch_mode_usage(mode: Optional[str] = None) -> List[dict]:
    """All runs for one mode (or all modes if mode=None), oldest first.
    Returns [] on any failure — never raises."""
    try:
        conn = _connect()
        try:
            if mode:
                cur = conn.execute(
                    'SELECT * FROM mode_usage WHERE mode=? ORDER BY start_time ASC', (mode,))
            else:
                cur = conn.execute('SELECT * FROM mode_usage ORDER BY start_time ASC')
            out = []
            for r in cur.fetchall():
                try:
                    extra = json.loads(r['extra_json']) if r['extra_json'] else {}
                except Exception:
                    extra = {}
                out.append({
                    'mode': r['mode'], 'start_time': r['start_time'], 'end_time': r['end_time'],
                    'duration_secs': r['duration_secs'],
                    'during_session': bool(r['during_session']), 'completed': bool(r['completed']),
                    'extra': extra,
                })
            return out
        finally:
            conn.close()
    except Exception as e:
        print(f'[Canary] Could not read mode-usage database: {e}')
        return []