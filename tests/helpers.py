"""
Shared fixtures for the airline_sim test suite.

Two kinds of world:

  fresh_game()  — a brand-new database built from db/schema.sql + data/*.csv, with an
                  airline created. Deterministic, independent of whatever is in your save.
                  Use this for end-to-end tests.

  live_copy()   — a throwaway copy of db/airline_sim.db when that file exists locally.
                  In CI (no save checked in) it bootstraps a fresh game instead so the
                  same tests still run.

Both repoint db.DB_FILE at a temp file and restore it on exit. Neither ever writes to
db/airline_sim.db.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def stub_rich() -> None:
    """ui/* imports rich at module scope; tests must run without it installed."""
    try:
        import rich  # noqa: F401
        return
    except ImportError:
        pass
    names = ("rich", "rich.console", "rich.table", "rich.panel", "rich.live",
             "rich.text", "rich.box", "rich.align", "rich.progress")
    for n in names:
        sys.modules.setdefault(n, types.ModuleType(n))
    attrs = {"rich.console": ["Console", "Group"], "rich.table": ["Table"],
             "rich.panel": ["Panel"], "rich.live": ["Live"], "rich.text": ["Text"],
             "rich.align": ["Align"]}
    for mod, syms in attrs.items():
        for s in syms:
            setattr(sys.modules[mod], s,
                    type(s, (object,), {"__init__": lambda self, *a, **k: None}))
    sys.modules["rich.box"].SIMPLE = None
    sys.modules["rich.box"].ROUNDED = None


stub_rich()

LIVE_DB = ROOT / "db" / "airline_sim.db"
SCHEMA = ROOT / "db" / "schema.sql"


@contextlib.contextmanager
def _quiet():
    """Engine and seed code print to stdout; keep test output readable."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf



def reset_db_connection(db) -> None:
    """
    Drop db.py's cached per-thread connection.

    db.get_cursor() reuses `db._tls.conn`, which is bound to whatever DB_FILE was set when it
    was first opened. Without this, repointing DB_FILE has no effect and every test after the
    first would silently read and write the previous world.
    """
    tls = getattr(db, "_tls", None)
    if tls is None:
        return
    conn = getattr(tls, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    for attr in ("conn", "depth"):
        if hasattr(tls, attr):
            try:
                delattr(tls, attr)
            except Exception:
                pass



def reset_engine_state() -> None:
    """
    Clear process-level caches in the engine so each test world starts clean.

    engine.ai keeps a one-shot _competitors_seeded flag, engine.fuel memoises the last
    fuel tick hour, and engine.ai_economics caches aircraft types. None of that should
    survive from one test world into the next.
    """
    try:
        import engine.ai as _ai
        _ai.reset_competitor_seed_cache()
    except Exception:
        pass
    for mod, attr, val in (
        ("engine.fuel", "_last_fuel_tick_game_hour", None),
        ("engine.fuel", "_dip_alert_fired", set()),
    ):
        try:
            m = __import__(mod, fromlist=["x"])
            if hasattr(m, attr):
                setattr(m, attr, val() if callable(val) else val)
        except Exception:
            pass
    for mod, fn in (("engine.ai_economics", "clear_type_cache"),):
        try:
            m = __import__(mod, fromlist=["x"])
            if hasattr(m, fn):
                getattr(m, fn)()
        except Exception:
            pass
    try:
        import engine.fuel as _f
        if hasattr(_f, "_week_ohlc"):
            _f._week_ohlc.clear()
    except Exception:
        pass
    try:
        import engine.environment as _env
        for cand in ("clear_all_closures",):
            if hasattr(_env, cand):
                getattr(_env, cand)()
    except Exception:
        pass


class _World:
    """Base: temp DB + db.DB_FILE redirection."""

    def __init__(self):
        self.dir = None
        self.path = None
        self.db = None
        self._orig = None

    def _start(self):
        self.dir = tempfile.mkdtemp(prefix="airsim_")
        self.path = Path(self.dir) / "airline_sim.db"
        from db import db
        self.db = db
        self._orig = db.DB_FILE
        reset_db_connection(db)
        reset_engine_state()
        db.DB_FILE = self.path
        db._migrations_done = False

    def close(self):
        if self.db is not None:
            reset_db_connection(self.db)
            self.db.DB_FILE = self._orig
            self.db._migrations_done = False
        if self.dir:
            shutil.rmtree(self.dir, ignore_errors=True)

    def __getattr__(self, name):
        """
        Delegate unknown attributes to the db module, so a test can use either
        `world.fetch_all(...)` or `world.db.fetch_all(...)` interchangeably.
        """
        dbmod = self.__dict__.get("db")
        if dbmod is not None and hasattr(dbmod, name):
            return getattr(dbmod, name)
        raise AttributeError(name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _bootstrap_fresh_db(
    world: _World,
    *,
    hub: str = "TPA",
    callsign: str = "TST",
    name: str = "Test Air",
    cash: float | None = None,
) -> None:
    """Schema + seed + airline in world.path (used by FreshGame and CI LiveCopy fallback)."""
    con = sqlite3.connect(world.path)
    try:
        con.executescript(SCHEMA.read_text())
        con.commit()
    finally:
        con.close()
    from db.seed import run_seed

    with _quiet():
        run_seed(world.path)
    world.db.ensure_schema_migrations()
    from engine import setup

    with _quiet():
        setup.create_airline(name=name, callsign=callsign, home_hub_iata=hub.upper())
    base = world.db.get_financial_constant("fuel_base_price_bbl") or 195.0
    world.db.execute(
        "UPDATE game_state SET fuel_price_current = ? WHERE id = 1", (float(base),)
    )
    if cash is not None:
        world.db.execute("UPDATE airline SET cash = ? WHERE id = 1", (float(cash),))


class FreshGame(_World):
    """
    A new world: schema + seed + airline. Clock is paused at hour 0, week 1.
    Exposes convenience builders so tests read like a play session.
    """

    def __init__(self, hub="TPA", callsign="TST", name="Test Air", cash=None):
        super().__init__()
        self.hub = hub.upper()
        self.callsign = callsign
        self.name = name
        self._start()
        self._build(cash)

    def _build(self, cash):
        _bootstrap_fresh_db(
            self,
            hub=self.hub,
            callsign=self.callsign,
            name=self.name,
            cash=cash,
        )

    # ---- builders -------------------------------------------------------
    def lease(self, type_id="B738", tail=None):
        from engine import aircraft
        with _quiet():
            ac = aircraft.lease_aircraft(type_id, 52, tail_number=tail)
        return ac["tail_number"]

    def buy(self, type_id="B738", tail=None):
        from engine import aircraft
        with _quiet():
            ac = aircraft.buy_aircraft(type_id, tail_number=tail)
        return ac["tail_number"]

    def open_route(self, origin, dest):
        from engine import routes
        with _quiet():
            routes.open_route(origin.upper(), dest.upper(), silent=True)
        return f"{origin.upper()}-{dest.upper()}"

    def open_round_trip(self, other):
        """Open hub->other and other->hub, returning both route ids."""
        a = self.open_route(self.hub, other)
        b = self.open_route(other, self.hub)
        return a, b

    def set_hour(self, hour):
        """Move the clock without running the GameClock thread."""
        wk = int(float(hour) // 168.0) + 1
        self.db.execute(
            "UPDATE game_state SET game_hours_elapsed = ?, game_week = ? WHERE id = 1",
            (float(hour), wk),
        )

    def cash(self):
        return float(self.db.fetch_one("SELECT cash FROM airline WHERE id = 1")["cash"])

    def fly_all(self, week=None):
        """Depart and land every SCHEDULED segment in a week. Returns (departed, landed)."""
        from engine.scheduling import on_arrival, on_departure
        if week is None:
            week = int(self.db.fetch_one("SELECT game_week FROM game_state WHERE id=1")["game_week"])
        segs = self.db.fetch_all(
            "SELECT segment_id FROM flight_segments WHERE game_week = ? AND status = 'SCHEDULED'"
            " ORDER BY scheduled_dep_game_hour",
            (week,),
        )
        ids = [s["segment_id"] for s in segs]
        dep = 0
        for sid in ids:
            with _quiet():
                on_departure(sid)
            dep += 1
        landed = 0
        for sid in ids:
            with _quiet():
                on_arrival(sid)
            landed += 1
        return dep, landed

    def settle(self, week):
        from engine.settlement import run_settlement
        with _quiet():
            return run_settlement(int(week))


class LiveCopy(_World):
    """Disposable copy of the real save, or a fresh bootstrap when no save exists (CI)."""

    def __init__(self):
        super().__init__()
        self._start()
        if LIVE_DB.is_file():
            shutil.copy2(LIVE_DB, self.path)
            self.db.ensure_schema_migrations()
        else:
            _bootstrap_fresh_db(self)
        self.db._migrations_done = False


def fresh_game(**kw):
    return FreshGame(**kw)


def live_copy():
    return LiveCopy()


def has_live_save() -> bool:
    return LIVE_DB.exists()


def pick_type(db, category="NARROW", min_range=0, max_seats=None):
    """Pick a seeded aircraft type that can actually be configured and flown."""
    rows = db.fetch_all(
        """
        SELECT t.type_id, t.range_nm, t.runway_req_ft, t.eec,
               c.seats_economy + c.seats_premium_economy + c.seats_business + c.seats_first AS seats
        FROM aircraft_types t
        JOIN aircraft_default_config c ON c.type_id = t.type_id
        WHERE t.category = ? AND t.range_nm >= ? AND t.eec IS NOT NULL
        ORDER BY t.range_nm
        """,
        (category, int(min_range)),
    )
    for r in rows:
        if max_seats is not None and int(r["seats"] or 0) > max_seats:
            continue
        return str(r["type_id"])
    return None


def near_airports(db, origin, min_nm=200, max_nm=1200, limit=6, require_runway_ft=0):
    """Airports within a distance band of origin, so tests do not exceed aircraft range."""
    from engine.routes import haversine_distance
    o = db.fetch_one("SELECT lat, lon FROM airports WHERE iata = ?", (origin.upper(),))
    if not o:
        return []
    out = []
    for r in db.fetch_all(
        "SELECT iata, lat, lon, runway_length_ft FROM airports WHERE iata != ?", (origin.upper(),)
    ):
        if require_runway_ft and (r["runway_length_ft"] or 0) < require_runway_ft:
            continue
        d = haversine_distance(float(o["lat"]), float(o["lon"]), float(r["lat"]), float(r["lon"]))
        if min_nm <= d <= max_nm:
            out.append((d, str(r["iata"])))
    out.sort()
    return [i for _d, i in out[:limit]]
