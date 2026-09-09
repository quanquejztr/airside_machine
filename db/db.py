"""
Database wrapper module for airline sim.
Provides connection management and atomic save operations.
"""

import sqlite3
import threading
from pathlib import Path
from contextlib import contextmanager


# Database file path
DB_FILE = Path(__file__).parent / "airline_sim.db"

# Overlay HUD + clock tick share one SQLite file across threads.
_db_lock = threading.RLock()
_migrations_done = False
_tls = threading.local()


def _configure_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def get_connection():
    """
    Fresh SQLite connection (atomic helpers). Overlay reads reuse a per-thread connection.
    """
    return _configure_connection(sqlite3.connect(DB_FILE, timeout=30.0))


def _thread_connection() -> sqlite3.Connection:
    conn = getattr(_tls, "conn", None)
    if conn is None:
        conn = _configure_connection(sqlite3.connect(DB_FILE, timeout=30.0))
        _tls.conn = conn
        _tls.depth = 0
    return conn


@contextmanager
def get_cursor():
    """
    Context manager for database operations.
    Automatically handles connection and commit/rollback.
    
    Usage:
        with get_cursor() as cursor:
            cursor.execute("SELECT * FROM airports")
            results = cursor.fetchall()
    """
    with _db_lock:
        conn = _thread_connection()
        depth = int(getattr(_tls, "depth", 0) or 0) + 1
        _tls.depth = depth
        cursor = conn.cursor()
        try:
            yield cursor
            if depth == 1:
                conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            _tls.depth = depth - 1


def execute_atomic(func):
    """
    Decorator for atomic database operations.
    Wraps a function to automatically handle transactions.
    
    Usage:
        @execute_atomic
        def update_game_state(conn, game_week):
            cursor = conn.cursor()
            cursor.execute("UPDATE game_state SET game_week = ? WHERE id = 1", (game_week,))
    """
    def wrapper(*args, **kwargs):
        with _db_lock:
            conn = get_connection()
            try:
                result = func(conn, *args, **kwargs)
                conn.commit()
                return result
            except Exception as e:
                conn.rollback()
                raise e
            finally:
                conn.close()
    return wrapper


def atomic_save():
    """
    Checkpoint WAL onto the main database file.

    Do not copy/replace the .db while connections are open — that omits
    airline_sim.db-wal and can orphan -wal/-shm against a swapped main file.
    WAL already provides crash safety; TRUNCATE flushes committed pages.
    """
    if not DB_FILE.exists():
        raise FileNotFoundError(f"Database file not found: {DB_FILE}")

    try:
        with _db_lock:
            conn = _thread_connection()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return True
    except Exception as e:
        raise Exception(f"Atomic save failed: {e}") from e


# ============================================================================
# Helper functions for common queries
# ============================================================================

def fetch_one(query, params=()):
    """Execute query and return single row."""
    with get_cursor() as cursor:
        cursor.execute(query, params)
        return cursor.fetchone()


def fetch_all(query, params=()):
    """Execute query and return all rows."""
    with get_cursor() as cursor:
        cursor.execute(query, params)
        return cursor.fetchall()


def execute(query, params=()):
    """Execute query without returning results."""
    with get_cursor() as cursor:
        cursor.execute(query, params)


def executemany(query, params_seq):
    """Execute the same statement for a sequence of parameter tuples."""
    with get_cursor() as cursor:
        cursor.executemany(query, params_seq)


def get_airport(iata):
    """Get airport by IATA code."""
    return fetch_one("SELECT * FROM airports WHERE iata = ?", (iata,))


def get_aircraft_type(type_id):
    """Get aircraft type by type_id."""
    return fetch_one("SELECT * FROM aircraft_types WHERE type_id = ?", (type_id,))


def get_financial_constant(key):
    """Get a financial constant by key."""
    row = fetch_one("SELECT value FROM financial_constants WHERE key = ?", (key,))
    return row['value'] if row else None


def get_seasonality(month):
    """Get seasonality multipliers for a given month."""
    return fetch_one("SELECT * FROM seasonality WHERE month = ?", (month,))


def list_airports(category=None):
    """List all airports, optionally filtered by category."""
    if category:
        return fetch_all("SELECT * FROM airports WHERE category = ? ORDER BY iata", (category,))
    return fetch_all("SELECT * FROM airports ORDER BY iata")


def list_aircraft_types(category=None):
    """List all aircraft types, optionally filtered by category."""
    if category:
        return fetch_all("SELECT * FROM aircraft_types WHERE category = ? ORDER BY type_id", (category,))
    return fetch_all("SELECT * FROM aircraft_types ORDER BY type_id")


# ============================================================================
# Database initialization
# ============================================================================

def _delete_table_rows(table: str) -> None:
    """DELETE all rows; ignore missing tables on older saves."""
    try:
        execute(f"DELETE FROM {table}")
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e).lower():
            raise


def purge_all_player_game_data():
    """
    Remove all dynamic gameplay rows (flights, fleet, routes, ledgers, AI ops, etc.).
    Static reference data (airports, aircraft types, constants) is kept.
    AI competitor rows are wiped too; re-seed from data/competitors.json after purge.
    Call before inserting a new airline so no ghost flights/routes remain.
    """
    for table in (
        "player_notifications",
        "credit_events",
        "loans",
        "settlement_flags",
        "fuel_price_history",
        "event_log",
        "flight_segments",
        "flight_schedules",
        "weekly_rotations",
        "airport_gate_bids",
        "airport_gate_auctions",
        "airport_gate_allocations",
        "slot_bids",
        "slot_auctions",
        "slot_usages",
        "slot_allocations",
        "week_ledger",
        "fleet_cabin_config",
        "fleet",
        "player_routes",
        "routes",
        "ai_flight_segments",
        "ai_turn_log",
        "ai_route_candidates",
        "ai_memory",
        "ai_fleet",
        "competitor_bids",
        "competitor_routes",
        "competitors",
    ):
        _delete_table_rows(table)
    execute("DELETE FROM airline WHERE id = 1")
    execute(
        """
        UPDATE game_state SET
            game_week = 1,
            game_hours_elapsed = 0.0,
            speed_multiplier = 0,
            current_month = 1,
            fuel_price_current = COALESCE(
                (SELECT value FROM financial_constants WHERE key = 'fuel_base_price_bbl' LIMIT 1),
                195.0
            ),
            fuel_price_trend = 0.0,
            demand_noise_seed = 1,
            pause_on_week_summary = 0
        WHERE id = 1
        """
    )


def sync_financial_constants_from_csv():
    """
    Apply data/financial_constants.csv to the database.

    The game reads financial constants from SQLite at runtime, not from the CSV.
    Call this on startup so edits to the CSV (e.g. weekly airborne cap) take effect
    without resetting the whole database.
    """
    from db.seed import load_csv

    try:
        rows = load_csv("financial_constants.csv")
    except OSError:
        return

    old_share = None
    try:
        prev = fetch_one(
            "SELECT value FROM financial_constants WHERE key = 'bts_target_market_share'"
        )
        if prev is not None and prev["value"] is not None:
            old_share = float(prev["value"])
    except Exception:
        old_share = None

    new_share = None
    for row in rows:
        key = row.get("key")
        if not key:
            continue
        try:
            val = float(row["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if key == "bts_target_market_share":
            new_share = val
        execute(
            "INSERT OR REPLACE INTO financial_constants (key, value) VALUES (?, ?)",
            (key, val),
        )

    # Sticky route templates were calibrated under the previous share; rescale
    # BTS/GRAVITY bases so live saves pick up the new playable market size.
    if (
        old_share is not None
        and new_share is not None
        and old_share > 0
        and abs(new_share - old_share) > 1e-9
    ):
        ratio = float(new_share) / float(old_share)
        try:
            execute(
                """
                UPDATE routes
                SET base_demand_business = MAX(0, ROUND(base_demand_business * ?)),
                    base_demand_leisure = MAX(0, ROUND(base_demand_leisure * ?))
                WHERE demand_source IN ('BTS', 'GRAVITY')
                """,
                (ratio, ratio),
            )
        except Exception:
            pass


def _ensure_bts_demand_anchors_table() -> None:
    execute(
        """
        CREATE TABLE IF NOT EXISTS bts_demand_anchors (
            origin_iata TEXT NOT NULL,
            dest_iata TEXT NOT NULL,
            anchor_annual REAL NOT NULL,
            anchor_weekly REAL NOT NULL,
            years_used INTEGER,
            first_year INTEGER,
            last_year INTEGER,
            method TEXT,
            PRIMARY KEY (origin_iata, dest_iata)
        )
        """
    )
    execute(
        "CREATE INDEX IF NOT EXISTS idx_bts_anchors_dest ON bts_demand_anchors(dest_iata)"
    )


def sync_bts_gravity_constants_from_json() -> None:
    """
    Overlay fitted gravity parameters into financial_constants (runtime reads the
    DB, not the JSON files).

    Two files feed this. bts_gravity_params.json holds the ingest settings and the
    legacy single-beta fit; gravity_bands.json holds the Phase 6 banded fit and
    wins on any key they share, since it is fitted on both anchor sets. The banded
    region-prior matrix cannot be expressed as scalar constants and is read
    directly from JSON by engine.route_demand.
    """
    import json

    base = Path(__file__).resolve().parent.parent / "data"

    def _read(name: str) -> dict:
        path = base / name
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {}

    raw = _read("bts_gravity_params.json")
    bands = _read("gravity_bands.json")
    if not raw and not bands:
        return

    mapping = {
        "bts_gravity_k": raw.get("k"),
        "bts_gravity_alpha": raw.get("alpha"),
        "bts_gravity_beta": raw.get("beta"),
        "bts_small_small_damp": raw.get("small_small_damp"),
        "bts_ref_year": raw.get("ref_year"),
        "bts_growth_rate": raw.get("growth_rate"),
        "bts_decay_lambda": raw.get("decay_lambda"),
    }
    if bands:
        mapping.update(
            {
                "bts_gravity_k": bands.get("k"),
                "bts_gravity_alpha": bands.get("alpha"),
                "bts_small_small_damp": bands.get("small_small_damp"),
                "bts_gravity_beta_short": bands.get("beta_short"),
                "bts_gravity_beta_medium": bands.get("beta_medium"),
                "bts_gravity_beta_long": bands.get("beta_long"),
                "bts_gravity_band1_nm": bands.get("band1_nm"),
                "bts_gravity_band2_nm": bands.get("band2_nm"),
            }
        )

    for key, val in mapping.items():
        if val is None:
            continue
        try:
            execute(
                "INSERT OR REPLACE INTO financial_constants (key, value) VALUES (?, ?)",
                (key, float(val)),
            )
        except (TypeError, ValueError):
            continue


def add_missing_airports_from_csv() -> int:
    """
    Insert airports present in data/airports.csv but absent from the save.

    Seeding only runs on an empty table, so a save created before an airports.csv
    update never sees the new entries — Phase 6 added 51 international hubs that
    existing games could not fly to at all. Insert-only by design: player-facing
    columns (fee overrides, curfews) and any hand-edited rows are left untouched.
    """
    from db.seed import load_csv

    rows = load_csv("airports.csv")
    if not rows:
        return 0
    have = {
        str(r["iata"]) for r in fetch_all("SELECT iata FROM airports") if r["iata"]
    }
    cols = [
        "iata", "icao", "name", "city", "country", "lat", "lon",
        "runway_length_ft", "gate_count", "timezone", "score", "category",
    ]
    placeholders = ",".join("?" * len(cols))
    added = 0
    for r in rows:
        iata = str(r.get("iata") or "").strip()
        if not iata or iata in have:
            continue
        try:
            execute(
                f"INSERT INTO airports ({','.join(cols)}) VALUES ({placeholders})",
                tuple((r.get(c) or None) for c in cols),
            )
            added += 1
        except sqlite3.Error:
            continue
    return added


def load_bts_demand_anchors_if_empty() -> int:
    """
    Bulk-load data/bts_demand_anchors.csv when the reference table is empty
    or still on an older calibration method tag.

    Returns number of rows inserted (0 if skipped or file missing).
    """
    import csv

    _ensure_bts_demand_anchors_table()
    # Phase 6: the table now holds two calibrations (US domestic from DB1B and
    # international from T-100). Both tags must be present, otherwise the table
    # predates the merge and needs a full reload.
    expected_methods = {
        "bts_trend_wm_max_recent_v2",
        "t100i_trend_wm_max_recent_v2",
        "kr_trend_wm_max_recent_v2",
        "jp_trend_wm_max_recent_v2",
        "eu_trend_wm_max_recent_v2",
        "au_trend_wm_max_recent_v2",
    }
    row = fetch_one("SELECT COUNT(*) AS n FROM bts_demand_anchors")
    n = int(row["n"] or 0) if row else 0
    if n > 0:
        present = {
            str(r["method"] or "")
            for r in fetch_all("SELECT DISTINCT method FROM bts_demand_anchors")
        }
        if expected_methods <= present:
            return 0
        # Stale calibration — replace with regenerated CSV.
        execute("DELETE FROM bts_demand_anchors")

    csv_path = Path(__file__).resolve().parent.parent / "data" / "bts_demand_anchors.csv"
    if not csv_path.is_file():
        return 0

    batch: list[tuple] = []
    inserted = 0

    def _opt_int(val) -> int | None:
        if val is None or str(val).strip() == "":
            return None
        return int(float(val))

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            o = str(row.get("origin_iata") or "").strip().upper()
            d = str(row.get("dest_iata") or "").strip().upper()
            if not o or not d:
                continue
            batch.append(
                (
                    o,
                    d,
                    float(row.get("anchor_annual") or 0),
                    float(row.get("anchor_weekly") or 0),
                    _opt_int(row.get("years_used")),
                    _opt_int(row.get("first_year")),
                    _opt_int(row.get("last_year")),
                    str(row.get("method") or "").strip() or None,
                )
            )
            if len(batch) >= 5000:
                executemany(
                    """
                    INSERT OR REPLACE INTO bts_demand_anchors (
                        origin_iata, dest_iata, anchor_annual, anchor_weekly,
                        years_used, first_year, last_year, method
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    batch,
                )
                inserted += len(batch)
                batch.clear()
    if batch:
        executemany(
            """
            INSERT OR REPLACE INTO bts_demand_anchors (
                origin_iata, dest_iata, anchor_annual, anchor_weekly,
                years_used, first_year, last_year, method
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            batch,
        )
        inserted += len(batch)
    return inserted


def lookup_bts_anchor_weekly(origin_iata: str, dest_iata: str):
    """Return anchor_weekly for a directional US pair, or None if unknown."""
    o = str(origin_iata or "").strip().upper()
    d = str(dest_iata or "").strip().upper()
    if not o or not d:
        return None
    row = fetch_one(
        """
        SELECT anchor_weekly FROM bts_demand_anchors
        WHERE origin_iata = ? AND dest_iata = ?
        """,
        (o, d),
    )
    if not row or row["anchor_weekly"] is None:
        return None
    try:
        val = float(row["anchor_weekly"])
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def bts_demand_anchors_loaded() -> int:
    """Row count in bts_demand_anchors (0 if table missing)."""
    try:
        row = fetch_one("SELECT COUNT(*) AS n FROM bts_demand_anchors")
        return int(row["n"] or 0) if row else 0
    except sqlite3.OperationalError:
        return 0


def _add_column_if_missing(table: str, column: str, decl: str) -> None:
    """ALTER TABLE ADD COLUMN; ignore if column already exists."""
    try:
        execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "duplicate column" not in msg and "already exists" not in msg:
            raise


def _repair_us_airport_timezones() -> None:
    """Undo the swapped Alaska / Pacific timezone names in existing saves.

    The airport generator had a -180..-130 band named Pacific/Honolulu, which swallowed
    all of Alaska (Anchorage itself came out as Honolulu), and a -130..-117 band named
    America/Anchorage that shadowed the Los_Angeles band, so the whole US west coast —
    LAX, SFO, SEA, PDX — was tagged Alaska time. 185 airports were affected.

    Order matters: fix the Pacific coast first, while genuine Alaska rows are still
    labelled Honolulu and so cannot be caught by that update. The latitude guards then
    make both statements idempotent, since Alaska sits above 50N and the contiguous
    west coast below it.
    """
    execute(
        """
        UPDATE airports SET timezone = 'America/Los_Angeles'
        WHERE country = 'US' AND timezone = 'America/Anchorage' AND lat < 50
        """
    )
    execute(
        """
        UPDATE airports SET timezone = 'America/Anchorage'
        WHERE country = 'US' AND timezone = 'Pacific/Honolulu' AND lat > 50
        """
    )


def _repair_catalog_reference_data() -> None:
    """Fix C750's shifted CSV fields and factory cabins that exceed type EEC."""
    execute(
        """
        UPDATE aircraft_types
        SET purchase_price = 4500000,
            weekly_lease_cost = 45000,
            eec = 9,
            maintenance_interval_weeks = COALESCE(maintenance_interval_weeks, 10)
        WHERE type_id = 'C750'
          AND (
            purchase_price < 1000000
            OR eec IS NULL
            OR CAST(eec AS INTEGER) >= 80000
            OR weekly_lease_cost IS NULL
            OR weekly_lease_cost = 0
          )
        """
    )
    rows = fetch_all(
        """
        SELECT c.type_id, c.seats_economy, c.seats_premium_economy,
               c.seats_business, c.seats_first, t.eec
        FROM aircraft_default_config c
        JOIN aircraft_types t ON t.type_id = c.type_id
        """
    )
    if not rows:
        return
    ce = 1.0
    cpe = 1.5
    cb = 2.0
    cf = 4.0
    try:
        consts = {
            str(r["key"]): float(r["value"])
            for r in fetch_all(
                "SELECT key, value FROM financial_constants WHERE key LIKE 'eec_cost_%'"
            )
            or []
        }
        ce = float(consts.get("eec_cost_economy", ce))
        cpe = float(consts.get("eec_cost_prem_eco", cpe))
        cb = float(consts.get("eec_cost_business", cb))
        cf = float(consts.get("eec_cost_first", cf))
    except Exception:
        pass
    for raw in rows:
        cap = raw["eec"]
        if cap is None:
            continue
        cap_f = float(cap)
        eco = int(raw["seats_economy"] or 0)
        pe = int(raw["seats_premium_economy"] or 0)
        biz = int(raw["seats_business"] or 0)
        first = int(raw["seats_first"] or 0)

        def used() -> float:
            return eco * ce + pe * cpe + biz * cb + first * cf

        while used() > cap_f + 1e-9:
            if first > 0:
                first -= 1
            elif biz > 0:
                biz -= 1
            elif pe > 0:
                pe -= 1
            elif eco > 0:
                eco -= 1
            else:
                break
        eec_used = int(round(used()))
        execute(
            """
            UPDATE aircraft_default_config
            SET seats_economy = ?,
                seats_premium_economy = ?,
                seats_business = ?,
                seats_first = ?,
                eec_used = ?
            WHERE type_id = ?
            """,
            (eco, pe, biz, first, eec_used, raw["type_id"]),
        )


def ensure_schema_migrations():
    """
    Idempotent migrations for existing databases (tables added after first release).
    Safe to call on every startup. Subsequent calls in-process are no-ops so the
    overlay HUD cannot rewrite financial_constants on every /api/state poll.
    """
    global _migrations_done
    if _migrations_done:
        return
    execute(
        """
        CREATE TABLE IF NOT EXISTS weekly_rotations (
            tail_number TEXT PRIMARY KEY,
            legs_json TEXT NOT NULL,
            created_game_week INTEGER NOT NULL,
            FOREIGN KEY (tail_number) REFERENCES fleet(tail_number)
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS event_log (
            event_id TEXT PRIMARY KEY,
            game_week INTEGER NOT NULL,
            game_time_hours REAL NOT NULL,
            event_type TEXT NOT NULL,
            affected_iata TEXT,
            affected_tail TEXT,
            description TEXT NOT NULL,
            financial_impact REAL NOT NULL DEFAULT 0,
            resolved INTEGER NOT NULL DEFAULT 0 CHECK(resolved IN (0, 1))
        )
        """
    )
    # Phase 9: event_log compatibility for older DBs that created message/game_hours_elapsed
    _add_column_if_missing("event_log", "game_time_hours", "REAL")
    _add_column_if_missing("event_log", "affected_iata", "TEXT")
    _add_column_if_missing("event_log", "affected_tail", "TEXT")
    _add_column_if_missing("event_log", "description", "TEXT")
    _add_column_if_missing("event_log", "financial_impact", "REAL NOT NULL DEFAULT 0")
    _add_column_if_missing("event_log", "resolved", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing("event_log", "message", "TEXT")
    _add_column_if_missing("event_log", "game_hours_elapsed", "REAL")

    # Phase 4: flight_segments cabin revenue + fuel snapshot
    _add_column_if_missing("flight_segments", "fuel_spot_price_per_gallon", "REAL")
    # Positioning (ferry) legs: flown to reposition a tail, no revenue passengers.
    _add_column_if_missing("flight_segments", "is_ferry", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing("flight_segments", "revenue_economy", "REAL NOT NULL DEFAULT 0")
    _add_column_if_missing("flight_segments", "revenue_premium_economy", "REAL NOT NULL DEFAULT 0")
    _add_column_if_missing("flight_segments", "revenue_business_cabin", "REAL NOT NULL DEFAULT 0")
    _add_column_if_missing("flight_segments", "revenue_first", "REAL NOT NULL DEFAULT 0")
    _add_column_if_missing("flight_segments", "pax_economy", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing("flight_segments", "pax_premium_economy", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing("flight_segments", "pax_business_cabin", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing("flight_segments", "pax_first", "INTEGER NOT NULL DEFAULT 0")
    # Phase 9: divert fields
    _add_column_if_missing("flight_segments", "divert_airport_iata", "TEXT")
    _add_column_if_missing("flight_segments", "divert_surcharge", "REAL NOT NULL DEFAULT 0")
    # Phase 11+: airport board support (denormalized endpoints for fast queries)
    _add_column_if_missing("flight_segments", "origin_iata", "TEXT")
    _add_column_if_missing("flight_segments", "dest_iata", "TEXT")
    # Immutable published plan; operational delays only touch scheduled_* until week rollover
    _add_column_if_missing("flight_segments", "baseline_dep_game_hour", "REAL")
    _add_column_if_missing("flight_segments", "baseline_arr_game_hour", "REAL")
    execute(
        "UPDATE flight_segments SET baseline_dep_game_hour = scheduled_dep_game_hour "
        "WHERE baseline_dep_game_hour IS NULL"
    )
    execute(
        "UPDATE flight_segments SET baseline_arr_game_hour = scheduled_arr_game_hour "
        "WHERE baseline_arr_game_hour IS NULL"
    )
    # Backfill origin/dest from routes where missing.
    try:
        execute(
            """
            UPDATE flight_segments
            SET origin_iata = (SELECT origin_iata FROM routes r WHERE r.route_id = flight_segments.route_id),
                dest_iata   = (SELECT dest_iata   FROM routes r WHERE r.route_id = flight_segments.route_id)
            WHERE origin_iata IS NULL OR dest_iata IS NULL
            """
        )
    except Exception:
        pass
    # Per-leg turnaround (minutes) used for gate MTT and schedule spacing.
    _add_column_if_missing("flight_segments", "turn_minutes", "INTEGER")
    try:
        default_mtt = int(float(get_financial_constant("mtt_minutes") or 30))
    except Exception:
        default_mtt = 30
    execute(
        "UPDATE flight_segments SET turn_minutes = ? WHERE turn_minutes IS NULL",
        (default_mtt,),
    )
    # Prefer turnaround stored on weekly rotation templates over the generic default.
    try:
        import json as _json

        for wr in fetch_all(
            "SELECT tail_number, legs_json FROM weekly_rotations WHERE legs_json IS NOT NULL"
        ):
            try:
                blob = _json.loads(wr["legs_json"] or "null")
            except Exception:
                continue
            legs = blob if isinstance(blob, list) else (blob or {}).get("legs") or []
            for leg in legs:
                if not isinstance(leg, dict):
                    continue
                rid = leg.get("route_id")
                tm = leg.get("turn_minutes")
                if not rid or tm is None or str(tm).strip() == "":
                    continue
                execute(
                    """
                    UPDATE flight_segments
                    SET turn_minutes = ?
                    WHERE tail_number = ?
                      AND route_id = ?
                      AND turn_minutes = ?
                    """,
                    (int(round(float(tm))), str(wr["tail_number"]), str(rid), default_mtt),
                )
            if isinstance(blob, dict):
                for chain in blob.get("chains") or []:
                    for leg in (chain or {}).get("legs") or []:
                        if not isinstance(leg, dict):
                            continue
                        rid = leg.get("route_id")
                        tm = leg.get("turn_minutes")
                        if not rid or tm is None or str(tm).strip() == "":
                            continue
                        execute(
                            """
                            UPDATE flight_segments
                            SET turn_minutes = ?
                            WHERE tail_number = ?
                              AND route_id = ?
                              AND turn_minutes = ?
                            """,
                            (
                                int(round(float(tm))),
                                str(wr["tail_number"]),
                                str(rid),
                                default_mtt,
                            ),
                        )
    except Exception:
        pass
    # Phase 4: game_state preferences + demand noise
    _add_column_if_missing("game_state", "demand_noise_seed", "INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing("game_state", "pause_on_week_summary", "INTEGER NOT NULL DEFAULT 0")

    # Allow additional speed multipliers (e.g., 20× speedrun).
    # SQLite can't ALTER CHECK constraints; recreate game_state if still using the old constraint.
    try:
        ms = fetch_one(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='game_state'"
        )
        ddl = str(ms["sql"] or "") if ms else ""
        if "speed_multiplier" in ddl and "IN (0, 1, 2, 4)" in ddl and "20" not in ddl:
            execute(
                """
                CREATE TABLE IF NOT EXISTS game_state_new (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    game_week INTEGER NOT NULL DEFAULT 1,
                    game_hours_elapsed REAL NOT NULL DEFAULT 0.0,
                    speed_multiplier INTEGER NOT NULL DEFAULT 0 CHECK(speed_multiplier IN (0, 1, 2, 4, 20)),
                    current_month INTEGER NOT NULL CHECK(current_month >= 1 AND current_month <= 12),
                    fuel_price_current REAL NOT NULL,
                    fuel_price_trend REAL NOT NULL,
                    demand_noise_seed INTEGER NOT NULL DEFAULT 1,
                    pause_on_week_summary INTEGER NOT NULL DEFAULT 0 CHECK(pause_on_week_summary IN (0, 1)),
                    fuel_shock_pending INTEGER NOT NULL DEFAULT 0 CHECK(fuel_shock_pending IN (0, 1)),
                    fuel_shock_message TEXT,
                    ui_blackout INTEGER NOT NULL DEFAULT 0 CHECK(ui_blackout IN (0, 1))
                )
                """
            )
            execute(
                """
                INSERT OR REPLACE INTO game_state_new (
                    id, schema_version, game_week, game_hours_elapsed, speed_multiplier,
                    current_month, fuel_price_current, fuel_price_trend,
                    demand_noise_seed, pause_on_week_summary,
                    fuel_shock_pending, fuel_shock_message, ui_blackout
                )
                SELECT
                    id, schema_version, game_week, game_hours_elapsed,
                    CASE
                        WHEN speed_multiplier IN (0, 1, 2, 4) THEN speed_multiplier
                        ELSE 0
                    END AS speed_multiplier,
                    current_month, fuel_price_current, fuel_price_trend,
                    COALESCE(demand_noise_seed, 1),
                    COALESCE(pause_on_week_summary, 0),
                    COALESCE(fuel_shock_pending, 0),
                    fuel_shock_message,
                    COALESCE(ui_blackout, 0)
                FROM game_state
                WHERE id = 1
                """
            )
            execute("DROP TABLE game_state")
            execute("ALTER TABLE game_state_new RENAME TO game_state")
    except Exception:
        pass

    # Allow 60× speed (extends 20× migration).
    try:
        ms = fetch_one(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='game_state'"
        )
        ddl = str(ms["sql"] or "") if ms else ""
        if "speed_multiplier" in ddl and "60" not in ddl:
            execute(
                """
                CREATE TABLE IF NOT EXISTS game_state_new (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    game_week INTEGER NOT NULL DEFAULT 1,
                    game_hours_elapsed REAL NOT NULL DEFAULT 0.0,
                    speed_multiplier INTEGER NOT NULL DEFAULT 0 CHECK(speed_multiplier IN (0, 1, 2, 4, 20, 60)),
                    current_month INTEGER NOT NULL CHECK(current_month >= 1 AND current_month <= 12),
                    fuel_price_current REAL NOT NULL,
                    fuel_price_trend REAL NOT NULL,
                    demand_noise_seed INTEGER NOT NULL DEFAULT 1,
                    pause_on_week_summary INTEGER NOT NULL DEFAULT 0 CHECK(pause_on_week_summary IN (0, 1)),
                    fuel_shock_pending INTEGER NOT NULL DEFAULT 0 CHECK(fuel_shock_pending IN (0, 1)),
                    fuel_shock_message TEXT,
                    ui_blackout INTEGER NOT NULL DEFAULT 0 CHECK(ui_blackout IN (0, 1))
                )
                """
            )
            execute(
                """
                INSERT OR REPLACE INTO game_state_new (
                    id, schema_version, game_week, game_hours_elapsed, speed_multiplier,
                    current_month, fuel_price_current, fuel_price_trend,
                    demand_noise_seed, pause_on_week_summary,
                    fuel_shock_pending, fuel_shock_message, ui_blackout
                )
                SELECT
                    id, schema_version, game_week, game_hours_elapsed,
                    CASE
                        WHEN speed_multiplier IN (0, 1, 2, 4, 20) THEN speed_multiplier
                        ELSE 0
                    END AS speed_multiplier,
                    current_month, fuel_price_current, fuel_price_trend,
                    COALESCE(demand_noise_seed, 1),
                    COALESCE(pause_on_week_summary, 0),
                    COALESCE(fuel_shock_pending, 0),
                    fuel_shock_message,
                    COALESCE(ui_blackout, 0)
                FROM game_state
                WHERE id = 1
                """
            )
            execute("DROP TABLE game_state")
            execute("ALTER TABLE game_state_new RENAME TO game_state")
    except Exception:
        pass
    # Phase 10: competitor market share display
    _add_column_if_missing("routes", "competitor_share_this_week", "REAL NOT NULL DEFAULT 0.0")
    _add_column_if_missing("routes", "is_active", "INTEGER NOT NULL DEFAULT 1")
    # Cabin-specific ticket prices (W/F); legacy DBs backfilled from B/L bases × yield
    _add_column_if_missing("routes", "price_premium_economy", "REAL")
    _add_column_if_missing("routes", "price_first", "REAL")
    _add_column_if_missing("routes", "demand_source", "TEXT")
    try:
        execute(
            """
            UPDATE routes
            SET price_premium_economy = price_leisure * (
                SELECT value FROM financial_constants WHERE key = 'eec_yield_prem_eco' LIMIT 1
            )
            WHERE price_premium_economy IS NULL
            """
        )
        execute(
            """
            UPDATE routes
            SET price_first = price_business * (
                SELECT value FROM financial_constants WHERE key = 'eec_yield_first' LIMIT 1
            )
            WHERE price_first IS NULL
            """
        )
    except Exception:
        pass

    # Phase 11+: gate auctions allocation metadata (new gates become effective next week)
    _add_column_if_missing("airport_gate_allocations", "effective_week", "INTEGER NOT NULL DEFAULT 1")

    # Phase 10: AI competitors + auctions
    execute(
        """
        CREATE TABLE IF NOT EXISTS competitors (
            competitor_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            callsign TEXT NOT NULL,
            home_hub_iata TEXT NOT NULL,
            cash REAL NOT NULL,
            strategy TEXT NOT NULL DEFAULT 'HUBSPOKE',
            aggressiveness REAL NOT NULL,
            risk_tolerance REAL NOT NULL DEFAULT 0.5,
            expansion_rate INTEGER NOT NULL DEFAULT 2,
            fleet_size INTEGER NOT NULL DEFAULT 0,
            max_fleet_size INTEGER NOT NULL DEFAULT 20,
            weekly_route_budget REAL NOT NULL DEFAULT 500000,
            bid_probability REAL NOT NULL,
            weekly_slot_budget REAL NOT NULL,
            reputation REAL NOT NULL,
            brand_power REAL NOT NULL,
            consecutive_loss_weeks INTEGER NOT NULL DEFAULT 0,
            last_evaluation_week INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (home_hub_iata) REFERENCES airports(iata)
        )
        """
    )
    _add_column_if_missing("competitors", "strategy", "TEXT NOT NULL DEFAULT 'HUBSPOKE'")
    _add_column_if_missing("competitors", "risk_tolerance", "REAL NOT NULL DEFAULT 0.5")
    _add_column_if_missing("competitors", "expansion_rate", "INTEGER NOT NULL DEFAULT 2")
    _add_column_if_missing("competitors", "fleet_size", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing("competitors", "max_fleet_size", "INTEGER NOT NULL DEFAULT 20")
    _add_column_if_missing("competitors", "weekly_route_budget", "REAL NOT NULL DEFAULT 500000")
    _add_column_if_missing("competitors", "consecutive_loss_weeks", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing("competitors", "last_evaluation_week", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing("competitors", "stance", "TEXT NOT NULL DEFAULT 'GROW'")
    _add_column_if_missing("competitors", "stance_since_week", "INTEGER NOT NULL DEFAULT 0")
    execute(
        """
        CREATE TABLE IF NOT EXISTS competitor_routes (
            competitor_id TEXT NOT NULL,
            route_pair_id TEXT NOT NULL,
            outbound_route_id TEXT NOT NULL,
            inbound_route_id TEXT NOT NULL,
            fare_business REAL NOT NULL,
            fare_leisure REAL NOT NULL,
            frequency_per_week INTEGER NOT NULL,
            aircraft_type_id TEXT,
            opened_week INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            estimated_weekly_profit REAL NOT NULL DEFAULT 0.0,
            actual_weekly_revenue_avg REAL NOT NULL DEFAULT 0.0,
            consecutive_loss_weeks INTEGER NOT NULL DEFAULT 0,
            contested INTEGER NOT NULL DEFAULT 0,
            market_share REAL NOT NULL DEFAULT 0.0,
            PRIMARY KEY (competitor_id, route_pair_id)
        )
        """
    )
    # If the table already existed with only a subset of columns, add missing ones.
    _add_column_if_missing("competitor_routes", "route_pair_id", "TEXT")
    _add_column_if_missing("competitor_routes", "outbound_route_id", "TEXT")
    _add_column_if_missing("competitor_routes", "inbound_route_id", "TEXT")
    _add_column_if_missing("competitor_routes", "aircraft_type_id", "TEXT")
    _add_column_if_missing("competitor_routes", "opened_week", "INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing("competitor_routes", "status", "TEXT NOT NULL DEFAULT 'ACTIVE'")
    _add_column_if_missing("competitor_routes", "estimated_weekly_profit", "REAL NOT NULL DEFAULT 0.0")
    _add_column_if_missing("competitor_routes", "actual_weekly_revenue_avg", "REAL NOT NULL DEFAULT 0.0")
    _add_column_if_missing("competitor_routes", "consecutive_loss_weeks", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing("competitor_routes", "contested", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing("competitor_routes", "market_share", "REAL NOT NULL DEFAULT 0.0")
    # Legacy migration: if older competitor_routes exists, replace it (small table; safe).
    # Perform a hard migration if the legacy route_id column exists (SQLite can't drop columns).
    cols = []
    try:
        cols = fetch_all("PRAGMA table_info(competitor_routes)")
    except Exception:
        cols = []
    names = {str(r["name"]) for r in (cols or []) if r and r["name"]}
    if ("route_id" in names) or ("outbound_route_id" not in names) or ("route_pair_id" not in names):
        # Rename legacy table if possible; if rename fails, drop it.
        try:
            execute("ALTER TABLE competitor_routes RENAME TO competitor_routes_legacy")
        except Exception:
            try:
                execute("DROP TABLE competitor_routes")
            except Exception:
                pass
        execute(
            """
            CREATE TABLE IF NOT EXISTS competitor_routes (
                competitor_id TEXT NOT NULL,
                route_pair_id TEXT NOT NULL,
                outbound_route_id TEXT NOT NULL,
                inbound_route_id TEXT NOT NULL,
                fare_business REAL NOT NULL,
                fare_leisure REAL NOT NULL,
                frequency_per_week INTEGER NOT NULL,
                aircraft_type_id TEXT,
                opened_week INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                estimated_weekly_profit REAL NOT NULL DEFAULT 0.0,
                actual_weekly_revenue_avg REAL NOT NULL DEFAULT 0.0,
                consecutive_loss_weeks INTEGER NOT NULL DEFAULT 0,
                contested INTEGER NOT NULL DEFAULT 0,
                market_share REAL NOT NULL DEFAULT 0.0,
                PRIMARY KEY (competitor_id, route_pair_id)
            )
            """
        )

    # Create indexes after ensuring table shape.
    try:
        execute("CREATE INDEX IF NOT EXISTS idx_competitor_routes_outbound ON competitor_routes(outbound_route_id)")
        execute("CREATE INDEX IF NOT EXISTS idx_competitor_routes_inbound ON competitor_routes(inbound_route_id)")
    except Exception:
        pass

    _add_column_if_missing("competitor_routes", "actual_weekly_net_avg", "REAL NOT NULL DEFAULT 0.0")
    _add_column_if_missing("competitor_routes", "actual_lf_avg", "REAL NOT NULL DEFAULT 0.0")
    _add_column_if_missing("competitor_routes", "paper_loss_weeks", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing("competitor_routes", "low_lf_weeks", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing("competitor_routes", "fare_war_weeks", "INTEGER NOT NULL DEFAULT 0")
    execute(
        """
        CREATE TABLE IF NOT EXISTS ai_route_candidates (
            candidate_id TEXT PRIMARY KEY,
            competitor_id TEXT NOT NULL,
            route_pair_id TEXT NOT NULL,
            score REAL NOT NULL,
            estimated_weekly_profit REAL NOT NULL,
            estimated_market_share REAL NOT NULL,
            estimated_entry_fare_leisure REAL NOT NULL,
            estimated_entry_fare_business REAL NOT NULL,
            evaluated_week INTEGER NOT NULL,
            decision TEXT NOT NULL,
            rejection_reason TEXT
        )
        """
    )
    execute("CREATE INDEX IF NOT EXISTS idx_ai_candidates_competitor ON ai_route_candidates(competitor_id)")
    execute("CREATE INDEX IF NOT EXISTS idx_ai_candidates_score ON ai_route_candidates(score)")

    execute(
        """
        CREATE TABLE IF NOT EXISTS ai_memory (
            memory_id TEXT PRIMARY KEY,
            competitor_id TEXT NOT NULL,
            route_pair_id TEXT NOT NULL,
            event TEXT NOT NULL,
            game_week INTEGER NOT NULL,
            cooldown_weeks INTEGER NOT NULL
        )
        """
    )
    execute("CREATE INDEX IF NOT EXISTS idx_ai_memory_competitor ON ai_memory(competitor_id)")
    execute("CREATE INDEX IF NOT EXISTS idx_ai_memory_pair ON ai_memory(route_pair_id)")

    execute(
        """
        CREATE TABLE IF NOT EXISTS ai_fleet (
            ai_tail TEXT PRIMARY KEY,
            competitor_id TEXT NOT NULL,
            type_id TEXT NOT NULL,
            status TEXT NOT NULL,
            assigned_route_pair_id TEXT
        )
        """
    )
    execute("CREATE INDEX IF NOT EXISTS idx_ai_fleet_competitor ON ai_fleet(competitor_id)")

    execute(
        """
        CREATE TABLE IF NOT EXISTS ai_turn_log (
            log_id TEXT PRIMARY KEY,
            competitor_id TEXT NOT NULL,
            game_week INTEGER NOT NULL,
            routes_opened TEXT,
            routes_closed TEXT,
            fares_adjusted TEXT,
            auctions_bid TEXT,
            candidates_evaluated INTEGER NOT NULL DEFAULT 0,
            duration_ms REAL NOT NULL DEFAULT 0.0,
            error TEXT
        )
        """
    )
    execute("CREATE INDEX IF NOT EXISTS idx_ai_turn_log_competitor ON ai_turn_log(competitor_id, game_week)")
    _add_column_if_missing("ai_turn_log", "narrative", "TEXT")

    execute(
        """
        CREATE TABLE IF NOT EXISTS ai_flight_segments (
            segment_id TEXT PRIMARY KEY,
            competitor_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            game_week INTEGER NOT NULL,
            flight_number TEXT NOT NULL,
            origin_iata TEXT NOT NULL,
            dest_iata TEXT NOT NULL,
            scheduled_dep_game_hour REAL NOT NULL,
            scheduled_arr_game_hour REAL NOT NULL,
            actual_dep_game_hour REAL,
            actual_arr_game_hour REAL,
            frequency INTEGER NOT NULL,
            status TEXT NOT NULL,
            simulated_pax_leisure INTEGER NOT NULL DEFAULT 0,
            simulated_pax_business INTEGER NOT NULL DEFAULT 0,
            simulated_revenue REAL NOT NULL DEFAULT 0.0,
            simulated_load_factor REAL NOT NULL DEFAULT 0.0
        )
        """
    )
    execute("CREATE INDEX IF NOT EXISTS idx_ai_segments_origin ON ai_flight_segments(origin_iata, game_week)")
    execute("CREATE INDEX IF NOT EXISTS idx_ai_segments_dest ON ai_flight_segments(dest_iata, game_week)")
    _add_column_if_missing("ai_flight_segments", "simulated_net", "REAL NOT NULL DEFAULT 0.0")

    execute(
        """
        CREATE TABLE IF NOT EXISTS player_notifications (
            notification_id TEXT PRIMARY KEY,
            game_week INTEGER NOT NULL,
            type TEXT NOT NULL,
            route_pair_id TEXT,
            body TEXT NOT NULL,
            read INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    execute("CREATE INDEX IF NOT EXISTS idx_player_notifications_read ON player_notifications(read, game_week)")

    try:
        execute("CREATE INDEX IF NOT EXISTS idx_flight_segments_origin_week ON flight_segments(origin_iata, game_week)")
        execute("CREATE INDEX IF NOT EXISTS idx_flight_segments_dest_week ON flight_segments(dest_iata, game_week)")
        execute(
            "CREATE INDEX IF NOT EXISTS idx_flight_segments_week_status_dep "
            "ON flight_segments(game_week, status, scheduled_dep_game_hour)"
        )
        execute(
            "CREATE INDEX IF NOT EXISTS idx_flight_segments_week_status_arr "
            "ON flight_segments(game_week, status, scheduled_arr_game_hour)"
        )
        execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_segments_week_status_dep "
            "ON ai_flight_segments(game_week, status, scheduled_dep_game_hour)"
        )
        execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_segments_week_status_arr "
            "ON ai_flight_segments(game_week, status, scheduled_arr_game_hour)"
        )
    except Exception:
        pass

    # Phase 11 (revised): airport gate-use auctions (replaces route_licences entirely)
    # Drop legacy route licence tables if present.
    for t in ("route_licences", "route_licence_auctions"):
        try:
            if fetch_one("SELECT 1 AS x FROM sqlite_master WHERE type='table' AND name=?", (t,)):
                execute(f"DROP TABLE {t}")
        except Exception:
            pass
    # competitor_bids is no longer used; keep table if it exists, but it is ignored.

    execute(
        """
        CREATE TABLE IF NOT EXISTS airport_gate_allocations (
            allocation_id TEXT PRIMARY KEY,
            airport_iata TEXT NOT NULL,
            holder_id TEXT NOT NULL,
            gate_units INTEGER NOT NULL,
            used_this_week INTEGER NOT NULL DEFAULT 0,
            scheduled_this_week INTEGER NOT NULL DEFAULT 0,
            below_threshold_weeks INTEGER NOT NULL DEFAULT 0,
            effective_week INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL CHECK(status IN ('ACTIVE','REVOKED')),
            FOREIGN KEY (airport_iata) REFERENCES airports(iata)
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS airport_gate_auctions (
            auction_id TEXT PRIMARY KEY,
            airport_iata TEXT NOT NULL,
            opens_week INTEGER NOT NULL,
            closes_week INTEGER NOT NULL,
            units_available INTEGER NOT NULL,
            current_price_per_unit REAL NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('OPEN','RESOLVED','CANCELLED')),
            FOREIGN KEY (airport_iata) REFERENCES airports(iata)
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS airport_gate_bids (
            bid_id TEXT PRIMARY KEY,
            auction_id TEXT NOT NULL,
            bidder_id TEXT NOT NULL,
            units_requested INTEGER NOT NULL,
            price_per_unit REAL NOT NULL,
            submitted_week INTEGER NOT NULL,
            FOREIGN KEY (auction_id) REFERENCES airport_gate_auctions(auction_id)
        )
        """
    )
    execute("CREATE INDEX IF NOT EXISTS idx_gate_alloc_airport ON airport_gate_allocations(airport_iata)")
    execute("CREATE INDEX IF NOT EXISTS idx_gate_alloc_holder ON airport_gate_allocations(holder_id)")
    execute("CREATE INDEX IF NOT EXISTS idx_gate_auctions_status ON airport_gate_auctions(status)")
    execute("CREATE INDEX IF NOT EXISTS idx_gate_bids_auction ON airport_gate_bids(auction_id)")

    execute(
        """
        CREATE TABLE IF NOT EXISTS slot_controlled_airports (
            iata TEXT PRIMARY KEY,
            declared_hourly_cap INTEGER NOT NULL,
            slot_season TEXT NOT NULL DEFAULT 'IATA',
            effective_week INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS slot_allocations (
            allocation_id TEXT PRIMARY KEY,
            airport_iata TEXT NOT NULL,
            holder_id TEXT NOT NULL,
            game_week INTEGER NOT NULL,
            slots_held INTEGER NOT NULL DEFAULT 0,
            used_this_week INTEGER NOT NULL DEFAULT 0,
            below_threshold_weeks INTEGER NOT NULL DEFAULT 0,
            UNIQUE(airport_iata, holder_id, game_week)
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS slot_usages (
            usage_id TEXT PRIMARY KEY,
            airport_iata TEXT NOT NULL,
            holder_id TEXT NOT NULL,
            game_week INTEGER NOT NULL,
            segment_id TEXT NOT NULL,
            movement_type TEXT NOT NULL,
            clock_hour INTEGER NOT NULL,
            UNIQUE(segment_id, movement_type)
        )
        """
    )
    execute("CREATE INDEX IF NOT EXISTS idx_slot_usages_airport_week ON slot_usages(airport_iata, game_week, clock_hour)")
    execute(
        """
        CREATE TABLE IF NOT EXISTS slot_auctions (
            auction_id TEXT PRIMARY KEY,
            airport_iata TEXT NOT NULL,
            opens_week INTEGER NOT NULL,
            closes_week INTEGER NOT NULL,
            units_available INTEGER NOT NULL,
            current_price_per_unit REAL NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('OPEN','RESOLVED','CANCELLED')),
            FOREIGN KEY (airport_iata) REFERENCES airports(iata)
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS slot_bids (
            bid_id TEXT PRIMARY KEY,
            auction_id TEXT NOT NULL,
            bidder_id TEXT NOT NULL,
            units_requested INTEGER NOT NULL,
            price_per_unit REAL NOT NULL,
            submitted_week INTEGER NOT NULL,
            UNIQUE(auction_id, bidder_id),
            FOREIGN KEY (auction_id) REFERENCES slot_auctions(auction_id)
        )
        """
    )
    execute("CREATE INDEX IF NOT EXISTS idx_slot_auctions_status ON slot_auctions(status)")
    execute("CREATE INDEX IF NOT EXISTS idx_slot_bids_auction ON slot_bids(auction_id)")
    try:
        from engine.slots import seed_slot_controlled_airports

        seed_slot_controlled_airports()
    except Exception:
        pass

    # Track which routes belong to the player (avoid listing AI-generated market routes in "View Routes").
    execute(
        """
        CREATE TABLE IF NOT EXISTS player_routes (
            route_id TEXT PRIMARY KEY,
            opened_week INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (route_id) REFERENCES routes(route_id)
        )
        """
    )
    execute("CREATE INDEX IF NOT EXISTS idx_player_routes_opened_week ON player_routes(opened_week)")
    # Backfill for legacy saves: any route the player has ever scheduled/flown counts as a player route.
    try:
        execute(
            """
            INSERT OR IGNORE INTO player_routes (route_id, opened_week)
            SELECT DISTINCT fs.route_id, MIN(fs.game_week)
            FROM flight_segments fs
            WHERE fs.route_id IS NOT NULL
            GROUP BY fs.route_id
            """
        )
    except Exception:
        pass

    _add_column_if_missing("airline", "negative_cash_weeks", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing("airline", "last_chapter11_week", "INTEGER")
    _add_column_if_missing("airline", "marketing_brand_bonus", "REAL NOT NULL DEFAULT 0")
    _add_column_if_missing("fleet", "lease_prepaid", "INTEGER NOT NULL DEFAULT 0")
    execute(
        """
        CREATE TABLE IF NOT EXISTS settlement_flags (
            game_week INTEGER PRIMARY KEY,
            cash_applied INTEGER NOT NULL DEFAULT 0,
            banking_done INTEGER NOT NULL DEFAULT 0,
            post_ops_done INTEGER NOT NULL DEFAULT 0,
            loan_collected REAL NOT NULL DEFAULT 0
        )
        """
    )
    _add_column_if_missing("settlement_flags", "loan_collected", "REAL NOT NULL DEFAULT 0")

    # Backfill legacy saves:
    # `settlement_flags` is a newer table/column, so older saves may already have
    # completed weeks in `week_ledger` but still show `post_ops_done = 0`.
    # That causes `--ui` startup to "retry" AI post-ops for every historical week.
    # If the week has a ledger entry and cash was applied, treat it as completed.
    try:
        execute(
            """
            UPDATE settlement_flags
            SET
                banking_done = 1,
                cash_applied = 1,
                post_ops_done = 1
            WHERE post_ops_done = 0
              AND cash_applied = 1
              AND game_week IN (SELECT game_week FROM week_ledger)
            """
        )
    except Exception:
        pass
    execute(
        """
        CREATE TABLE IF NOT EXISTS loans (
            loan_id TEXT PRIMARY KEY,
            principal_original REAL NOT NULL,
            principal_remaining REAL NOT NULL,
            weekly_interest_rate REAL NOT NULL,
            weekly_payment REAL NOT NULL,
            weeks_remaining INTEGER NOT NULL,
            originated_week INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('ACTIVE','CLOSED','DEFAULTED'))
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS credit_events (
            event_id TEXT PRIMARY KEY,
            game_week INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            score_delta INTEGER NOT NULL,
            description TEXT
        )
        """
    )
    execute("CREATE INDEX IF NOT EXISTS idx_loans_status ON loans(status)")
    execute("CREATE INDEX IF NOT EXISTS idx_credit_events_week ON credit_events(game_week)")
    execute(
        """
        CREATE TABLE IF NOT EXISTS route_flight_numbers (
            route_id TEXT PRIMARY KEY,
            flight_number TEXT NOT NULL,
            updated_week INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    sync_financial_constants_from_csv()
    sync_bts_gravity_constants_from_json()
    try:
        load_bts_demand_anchors_if_empty()
    except Exception:
        pass

    # Ensure static reference data exists (some flows create schema but never seed).
    try:
        cnt = fetch_one("SELECT COUNT(*) AS n FROM airports")
        n = int(cnt["n"] or 0) if cnt else 0
        if n <= 0:
            from db.seed import run_seed
            run_seed(DB_FILE)
        else:
            add_missing_airports_from_csv()
    except Exception:
        # Never block startup if seeding fails; gameplay will surface missing ref data quickly.
        pass
    # Catalog repairs (C750 malformed row, factory cabins over EEC). Idempotent.
    try:
        _repair_catalog_reference_data()
    except Exception:
        pass
    try:
        _repair_us_airport_timezones()
    except Exception:
        pass
    _migrations_done = True


def init_db():
    """
    Initialize the database from schema.
    Creates all tables if they don't exist.
    """
    schema_file = Path(__file__).parent / "schema.sql"
    
    if not schema_file.exists():
        raise FileNotFoundError(f"Schema file not found: {schema_file}")
    
    with open(schema_file, 'r') as f:
        schema_sql = f.read()
    
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.executescript(schema_sql)
        conn.commit()
        print(f"✓ Database initialized: {DB_FILE}")
    except Exception as e:
        conn.rollback()
        raise Exception(f"Failed to initialize database: {e}")
    finally:
        conn.close()


def db_exists():
    """Check if database file exists."""
    return DB_FILE.exists()


def get_schema_version():
    """Get the current schema version from game_state."""
    try:
        row = fetch_one("SELECT schema_version FROM game_state WHERE id = 1")
        return row['schema_version'] if row else None
    except sqlite3.OperationalError:
        # Table doesn't exist yet
        return None
