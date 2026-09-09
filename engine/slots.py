"""
Runway slot caps (hourly movements), independent of apron/gate concurrency.

Stage 1: JFK, LHR, HND, IAD, DCA, DXB, PEK, ICN, LAX, SIN, SYD, TPE, HKG, CDG.

Game-scale declared_hourly_cap (all Stage-1 airports start at 26 movements/hour).
Real IATA Level-3 caps (60–90/hr) would rarely bind with this sim's traffic; 26/hr
still allows congestion at peak banks without locking starters out of their hub.
"""

from __future__ import annotations

import math
import uuid
from typing import Dict, List, Optional, Tuple

from db import db


STAGE1_SLOT_AIRPORTS: Dict[str, int] = {
    "JFK": 26,
    "LHR": 26,
    "HND": 26,
    "IAD": 26,
    "DCA": 26,
    "DXB": 26,
    "PEK": 26,
    "ICN": 26,
    "LAX": 26,
    "SIN": 26,
    "SYD": 26,
    "TPE": 26,
    "HKG": 26,
    "CDG": 26,
}

_MISSING_AIRPORTS = {
    "CDG": {
        "icao": "LFPG",
        "name": "Charles de Gaulle Airport",
        "city": "Paris",
        "country": "FR",
        "lat": 49.009724,
        "lon": 2.547778,
        "runway_length_ft": 13829,
        "gate_count": 120,
        "timezone": "Europe/Paris",
        "score": 1_450_000,
        "category": "large_airport",
    },
    "HKG": {
        "icao": "VHHH",
        "name": "Hong Kong International Airport",
        "city": "Hong Kong",
        "country": "HK",
        "lat": 22.308047,
        "lon": 113.918527,
        "runway_length_ft": 12467,
        "gate_count": 90,
        "timezone": "Asia/Hong_Kong",
        "score": 1_480_000,
        "category": "large_airport",
    },
}


class SlotUnavailableError(ValueError):
    """Raised when a slot-controlled airport is full in that clock-hour."""


def clock_hour_of(abs_game_hour: float) -> int:
    week_start = float(int(float(abs_game_hour) // 168.0) * 168)
    h = int(float(abs_game_hour) - week_start)
    return max(0, min(167, h))


def _ensure_slot_tables() -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS slot_controlled_airports (
            iata TEXT PRIMARY KEY,
            declared_hourly_cap INTEGER NOT NULL,
            slot_season TEXT NOT NULL DEFAULT 'IATA',
            effective_week INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    db.execute(
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
    db.execute(
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
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_slot_usages_airport_week ON slot_usages(airport_iata, game_week, clock_hour)"
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS slot_auctions (
            auction_id TEXT PRIMARY KEY,
            airport_iata TEXT NOT NULL,
            opens_week INTEGER NOT NULL,
            closes_week INTEGER NOT NULL,
            units_available INTEGER NOT NULL,
            current_price_per_unit REAL NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('OPEN','RESOLVED','CANCELLED'))
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS slot_bids (
            bid_id TEXT PRIMARY KEY,
            auction_id TEXT NOT NULL,
            bidder_id TEXT NOT NULL,
            units_requested INTEGER NOT NULL,
            price_per_unit REAL NOT NULL,
            submitted_week INTEGER NOT NULL,
            UNIQUE(auction_id, bidder_id)
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_slot_auctions_status ON slot_auctions(status)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_slot_bids_auction ON slot_bids(auction_id)")


def _ensure_airport_row(iata: str) -> None:
    ap = str(iata).upper().strip()
    if db.fetch_one("SELECT 1 FROM airports WHERE iata = ?", (ap,)):
        return
    meta = _MISSING_AIRPORTS.get(ap)
    if not meta:
        return
    db.execute(
        """
        INSERT OR IGNORE INTO airports (
            iata, icao, name, city, country, lat, lon,
            runway_length_ft, gate_count, timezone, score, category
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ap,
            meta["icao"],
            meta["name"],
            meta["city"],
            meta["country"],
            float(meta["lat"]),
            float(meta["lon"]),
            int(meta["runway_length_ft"]),
            int(meta["gate_count"]),
            meta["timezone"],
            int(meta["score"]),
            meta["category"],
        ),
    )


def seed_slot_controlled_airports() -> None:
    _ensure_slot_tables()
    for iata, cap in STAGE1_SLOT_AIRPORTS.items():
        _ensure_airport_row(iata)
        if not db.fetch_one("SELECT 1 FROM airports WHERE iata = ?", (iata,)):
            continue
        db.execute(
            """
            INSERT OR IGNORE INTO slot_controlled_airports
                (iata, declared_hourly_cap, slot_season, effective_week)
            VALUES (?, ?, 'IATA', 1)
            """,
            (iata, int(cap)),
        )
        # Raise caps on existing saves when STAGE1 values increase (INSERT OR IGNORE
        # alone would leave stale low caps forever).
        db.execute(
            """
            UPDATE slot_controlled_airports
            SET declared_hourly_cap = ?
            WHERE iata = ?
              AND COALESCE(declared_hourly_cap, 0) < ?
              AND slot_season != 'OPEN'
            """,
            (int(cap), iata, int(cap)),
        )


def is_slot_controlled(iata: str) -> bool:
    ap = str(iata or "").upper().strip()
    if not ap:
        return False
    row = db.fetch_one(
        "SELECT 1 FROM slot_controlled_airports WHERE iata = ? AND slot_season != 'OPEN'",
        (ap,),
    )
    return bool(row)


def declared_hourly_cap(iata: str) -> int:
    row = db.fetch_one(
        "SELECT declared_hourly_cap FROM slot_controlled_airports WHERE iata = ?",
        (str(iata).upper().strip(),),
    )
    if not row:
        return 9999
    return int(row["declared_hourly_cap"] or 9999)


def hourly_movements_at(
    iata: str, game_week: int, holder_id: Optional[str] = None
) -> Dict[int, int]:
    """Count live DEP+ARR movements from player and AI segments (source of truth)."""
    ap = str(iata).upper().strip()
    gw = int(game_week)
    out: Dict[int, int] = {}

    def _bump(abs_h: float) -> None:
        ch = clock_hour_of(abs_h)
        out[ch] = int(out.get(ch, 0)) + 1

    player_sql = """
        SELECT fs.scheduled_dep_game_hour AS dep_h, fs.scheduled_arr_game_hour AS arr_h,
               COALESCE(fs.origin_iata, r.origin_iata) AS oi,
               COALESCE(fs.dest_iata, r.dest_iata) AS di
        FROM flight_segments fs
        JOIN routes r ON r.route_id = fs.route_id
        WHERE fs.game_week = ?
          AND fs.status != 'CANCELLED'
    """
    for r in db.fetch_all(player_sql, (gw,)):
        if holder_id and holder_id != "PLAYER":
            break
        oi = str(r["oi"] or "").upper()
        di = str(r["di"] or "").upper()
        if oi == ap:
            _bump(float(r["dep_h"] or 0.0))
        if di == ap:
            _bump(float(r["arr_h"] or 0.0))
        if holder_id == "PLAYER":
            continue

    ai_sql = """
        SELECT scheduled_dep_game_hour AS dep_h, scheduled_arr_game_hour AS arr_h,
               origin_iata AS oi, dest_iata AS di, competitor_id
        FROM ai_flight_segments
        WHERE game_week = ?
          AND status != 'CANCELLED'
    """
    params: tuple = (gw,)
    if holder_id and holder_id != "PLAYER":
        ai_sql += " AND competitor_id = ?"
        params = (gw, str(holder_id))
    if holder_id == "PLAYER":
        return out
    for r in db.fetch_all(ai_sql, params):
        oi = str(r["oi"] or "").upper()
        di = str(r["di"] or "").upper()
        if oi == ap:
            _bump(float(r["dep_h"] or 0.0))
        if di == ap:
            _bump(float(r["arr_h"] or 0.0))
    return out


def check_slot_available(
    iata: str, clock_hour: int, game_week: int
) -> Tuple[bool, int, int]:
    ap = str(iata).upper().strip()
    if not is_slot_controlled(ap):
        return (True, 0, 9999)
    cap = declared_hourly_cap(ap)
    current = int(hourly_movements_at(ap, int(game_week)).get(int(clock_hour), 0))
    return (current < cap, current, cap)


def assert_player_slots_for_new_segments(
    game_week: int, segments: list[dict], *, ferry: bool = False
) -> None:
    """
    Pre-insert check: existing live movements + these planned extras must stay under
    the hourly cap AND the player's weekly purchased quota at slot-controlled airports.
    segments: origin_iata, dest_iata, dep_abs, arr_abs.

    ferry=True skips the weekly purchased-quota check (repositioning is not commercial
    service) but still enforces the airport's declared hourly movement cap.
    """
    gw = int(game_week)
    grandfather_historic_slot_holdings(gw)
    extra: Dict[Tuple[str, int], int] = {}
    extra_ap: Dict[str, int] = {}
    for s in segments or []:
        oi = str(s.get("origin_iata") or "").upper().strip()
        di = str(s.get("dest_iata") or "").upper().strip()
        dep = float(s.get("dep_abs") or 0.0)
        arr = float(s.get("arr_abs") or 0.0)
        if is_slot_controlled(oi):
            key = (oi, clock_hour_of(dep))
            extra[key] = int(extra.get(key, 0)) + 1
            extra_ap[oi] = int(extra_ap.get(oi, 0)) + 1
        if is_slot_controlled(di):
            key = (di, clock_hour_of(arr))
            extra[key] = int(extra.get(key, 0)) + 1
            extra_ap[di] = int(extra_ap.get(di, 0)) + 1
    for (ap, ch), n in extra.items():
        _ok, cur, cap = check_slot_available(ap, ch, gw)
        if cur + n > cap:
            raise SlotUnavailableError(
                f"{ap} is full at hour {ch} ({cur}/{cap} movements; this schedule adds {n}). "
                f"Pick a different departure time."
            )
    if ferry:
        return
    for ap, n in extra_ap.items():
        held = slots_held(ap, "PLAYER", gw)
        used = int(sum(hourly_movements_at(ap, gw, holder_id="PLAYER").values()))
        if used + n > held:
            raise SlotUnavailableError(
                f"{ap} weekly slot quota {used + n}/{held} would be exceeded "
                f"(you hold {held}, already using {used}, this schedule adds {n}). "
                f"Bid on runway slots this week; awards start next week."
            )


def _write_usage(
    segment_id: str,
    iata: str,
    holder_id: str,
    game_week: int,
    movement_type: str,
    clock_hour: int,
) -> None:
    db.execute(
        """
        INSERT OR IGNORE INTO slot_usages (
            usage_id, airport_iata, holder_id, game_week,
            segment_id, movement_type, clock_hour
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            str(iata).upper().strip(),
            str(holder_id),
            int(game_week),
            str(segment_id),
            str(movement_type),
            int(clock_hour),
        ),
    )


def claim_slots(
    segment_id: str,
    origin_iata: str,
    dest_iata: str,
    dep_abs: float,
    arr_abs: float,
    holder_id: str,
    game_week: int,
) -> None:
    sid = str(segment_id)
    hid = str(holder_id)
    gw = int(game_week)
    for iata, mv_type, abs_h in (
        (str(origin_iata).upper().strip(), "DEP", float(dep_abs)),
        (str(dest_iata).upper().strip(), "ARR", float(arr_abs)),
    ):
        if not iata or not is_slot_controlled(iata):
            continue
        ch = clock_hour_of(abs_h)
        ok, cur, cap = check_slot_available(iata, ch, gw)
        if not ok:
            raise SlotUnavailableError(
                f"{iata} is full at hour {ch} ({cur}/{cap} movements). Pick a different departure time."
            )
        _write_usage(sid, iata, hid, gw, mv_type, ch)


def find_available_cycle(
    *,
    origin_iata: str,
    dest_iata: str,
    dep_out: float,
    out_dur: float,
    mtt: float,
    in_dur: float,
    game_week: int,
    max_shift: int = 3,
) -> Optional[Tuple[float, float, float, float]]:
    """
    Shift an outbound bank by ± max_shift hours until origin/dest hours fit.
    Returns (dep_out, arr_out, dep_in, arr_in) or None.
    """
    oi = str(origin_iata).upper().strip()
    di = str(dest_iata).upper().strip()
    gw = int(game_week)
    base = float(dep_out)

    def _ok(dep: float) -> Optional[Tuple[float, float, float, float]]:
        arr_o = dep + float(out_dur)
        dep_i = arr_o + float(mtt)
        arr_i = dep_i + float(in_dur)
        extra: Dict[Tuple[str, int], int] = {}
        for ap, abs_h in ((oi, dep), (di, arr_o), (di, dep_i), (oi, arr_i)):
            if not is_slot_controlled(ap):
                continue
            key = (ap, clock_hour_of(abs_h))
            extra[key] = int(extra.get(key, 0)) + 1
        for (ap, ch), n in extra.items():
            _ok_slot, cur, cap = check_slot_available(ap, ch, gw)
            if cur + n > cap:
                return None
        return (dep, arr_o, dep_i, arr_i)

    for delta in range(0, int(max_shift) + 1):
        signs = [0] if delta == 0 else [1, -1]
        for sign in signs:
            got = _ok(base + float(delta * sign))
            if got:
                return got
    return None


def slots_held_vs_used(iata: str, holder_id: str, game_week: int) -> Dict[str, float]:
    ap = str(iata).upper().strip()
    hid = str(holder_id)
    gw = int(game_week)
    hmap = hourly_movements_at(ap, gw, holder_id=hid)
    used = int(sum(hmap.values()))
    alloc = db.fetch_one(
        """
        SELECT slots_held FROM slot_allocations
        WHERE airport_iata = ? AND holder_id = ? AND game_week = ?
        """,
        (ap, hid, gw),
    )
    held = int(alloc["slots_held"] or 0) if alloc else 0
    peak_h, peak_n = -1, 0
    total_map = hourly_movements_at(ap, gw)
    for h, n in total_map.items():
        if int(n) > peak_n:
            peak_h, peak_n = int(h), int(n)
    cap = declared_hourly_cap(ap)
    return {
        "iata": ap,
        "holder_id": hid,
        "used": used,
        "held": held,
        "util": (float(used) / float(held)) if held > 0 else 0.0,
        "peak_hour": peak_h,
        "peak_movements": peak_n,
        "cap": cap,
    }


def _record_segment_usages(
    segment_id: str, origin: str, dest: str, dep_abs: float, arr_abs: float, holder_id: str, game_week: int
) -> int:
    n = 0
    if is_slot_controlled(origin):
        _write_usage(segment_id, origin, holder_id, game_week, "DEP", clock_hour_of(dep_abs))
        n += 1
    if is_slot_controlled(dest):
        _write_usage(segment_id, dest, holder_id, game_week, "ARR", clock_hour_of(arr_abs))
        n += 1
    return n


def rebuild_slot_usages_for_week(game_week: int) -> int:
    gw = int(game_week)
    _ensure_slot_tables()
    db.execute("DELETE FROM slot_usages WHERE game_week = ?", (gw,))
    n = 0
    for r in db.fetch_all(
        """
        SELECT fs.segment_id,
               fs.scheduled_dep_game_hour AS dep_h, fs.scheduled_arr_game_hour AS arr_h,
               COALESCE(fs.origin_iata, rt.origin_iata) AS oi,
               COALESCE(fs.dest_iata, rt.dest_iata) AS di
        FROM flight_segments fs
        JOIN routes rt ON rt.route_id = fs.route_id
        WHERE fs.game_week = ? AND fs.status != 'CANCELLED'
        """,
        (gw,),
    ):
        n += _record_segment_usages(
            str(r["segment_id"]),
            str(r["oi"] or ""),
            str(r["di"] or ""),
            float(r["dep_h"] or 0.0),
            float(r["arr_h"] or 0.0),
            "PLAYER",
            gw,
        )
    for r in db.fetch_all(
        """
        SELECT segment_id, competitor_id, origin_iata, dest_iata,
               scheduled_dep_game_hour AS dep_h, scheduled_arr_game_hour AS arr_h
        FROM ai_flight_segments
        WHERE game_week = ? AND status != 'CANCELLED'
        """,
        (gw,),
    ):
        n += _record_segment_usages(
            str(r["segment_id"]),
            str(r["origin_iata"] or ""),
            str(r["dest_iata"] or ""),
            float(r["dep_h"] or 0.0),
            float(r["arr_h"] or 0.0),
            str(r["competitor_id"]),
            gw,
        )
    _sync_used_this_week(gw)
    return n


def _const_num(key: str, default: float) -> float:
    row = db.fetch_one("SELECT value FROM financial_constants WHERE key = ?", (key,))
    if not row or row["value"] is None:
        return default
    try:
        return float(row["value"])
    except (TypeError, ValueError):
        return default


def clock_hour_label(clock_hour: int) -> str:
    ch = int(clock_hour)
    if ch < 0:
        return "—"
    h = ch % 24
    if 6 <= h < 10:
        bank = "morning bank"
    elif 16 <= h < 20:
        bank = "evening bank"
    else:
        bank = "off-peak"
    return f"hour {ch} ({bank})"


def list_slot_airport_rows(game_week: int, holder_id: Optional[str] = None) -> List[dict]:
    """One row per seeded slot airport for CLI / intel."""
    seed_slot_controlled_airports()
    gw = int(game_week)
    hid = holder_id
    rows: List[dict] = []
    for r in db.fetch_all(
        "SELECT iata, declared_hourly_cap FROM slot_controlled_airports ORDER BY iata"
    ):
        ap = str(r["iata"])
        cap = int(r["declared_hourly_cap"] or 0)
        total = hourly_movements_at(ap, gw)
        mine = hourly_movements_at(ap, gw, holder_id=hid) if hid else total
        used = int(sum(mine.values()))
        peak_h, peak_n = -1, 0
        for h, n in total.items():
            if int(n) > peak_n:
                peak_h, peak_n = int(h), int(n)
        rows.append(
            {
                "iata": ap,
                "cap": cap,
                "used": used,
                "held": int(slots_held(ap, hid, gw) if hid else 0),
                "peak_hour": peak_h,
                "peak_movements": peak_n,
                "full": bool(peak_n >= cap and cap > 0),
            }
        )
    return rows


def slot_season_weeks() -> int:
    """Length of a slot season. Quota lapses at the season boundary, not every week."""
    return max(1, int(_const_num("slot_season_weeks", 12.0)))


def slot_enforcement_mode() -> str:
    """'season' (default) lapses unused quota at season boundaries; 'weekly' is the legacy tax."""
    row = db.fetch_one("SELECT value FROM financial_constants WHERE key = 'slot_enforcement_weekly'")
    try:
        weekly = bool(row and float(row["value"]) >= 1.0)
    except (TypeError, ValueError):
        weekly = False
    return "weekly" if weekly else "season"


def _holder_home_hub(holder_id: str) -> Optional[str]:
    hid = str(holder_id)
    if hid == "PLAYER":
        row = db.fetch_one("SELECT home_hub_iata FROM airline WHERE id = 1")
    else:
        row = db.fetch_one(
            "SELECT home_hub_iata FROM competitors WHERE competitor_id = ?", (hid,)
        )
    if not row or not row["home_hub_iata"]:
        return None
    return str(row["home_hub_iata"]).upper().strip()


def _disrupted_movements(iata: str, holder_id: str, game_week: int) -> int:
    """
    Movements this holder planned at `iata` that were cancelled.

    slot_usages is rebuilt from non-cancelled segments, so an AOG / strike / weather week
    silently drops utilisation. Crediting cancelled movements means the game does not fine
    you for its own disruptions.
    """
    ap = str(iata).upper().strip()
    gw = int(game_week)
    hid = str(holder_id)
    n = 0
    if hid == "PLAYER":
        row = db.fetch_one(
            """
            SELECT
              SUM(CASE WHEN COALESCE(fs.origin_iata, r.origin_iata) = ? THEN 1 ELSE 0 END) +
              SUM(CASE WHEN COALESCE(fs.dest_iata,   r.dest_iata)   = ? THEN 1 ELSE 0 END) AS n
            FROM flight_segments fs
            JOIN routes r ON r.route_id = fs.route_id
            WHERE fs.game_week = ? AND fs.status = 'CANCELLED'
            """,
            (ap, ap, gw),
        )
    else:
        row = db.fetch_one(
            """
            SELECT
              SUM(CASE WHEN origin_iata = ? THEN 1 ELSE 0 END) +
              SUM(CASE WHEN dest_iata   = ? THEN 1 ELSE 0 END) AS n
            FROM ai_flight_segments
            WHERE game_week = ? AND competitor_id = ? AND status = 'CANCELLED'
            """,
            (ap, ap, gw, hid),
        )
    try:
        n = int((row["n"] if row else 0) or 0)
    except (TypeError, ValueError):
        n = 0
    return max(0, n)


def enforce_slot_utilization(settled_game_week: int) -> None:
    """
    Weekly quota use-it-or-lose-it, seasonal by default.

    Every week: compute utilisation (crediting movements cancelled by disruption), track the
    low-use streak, and warn the player before anything is taken.
    At the season boundary (or every week in legacy 'weekly' mode): shed units in proportion
    to the shortfall, never taking the holder's last unit at their home hub.
    """
    gw = int(settled_game_week)
    thr = _const_num("slot_utilization_threshold", 0.70)
    grace = int(_const_num("slot_grace_weeks", 2.0))
    mode = slot_enforcement_mode()
    season = slot_season_weeks()
    season_end = (gw % season) == 0
    enforcing = season_end if mode == "season" else True

    rows = db.fetch_all(
        "SELECT * FROM slot_allocations WHERE game_week = ? AND slots_held > 0",
        (gw,),
    )
    for r in rows:
        held = int(r["slots_held"] or 0)
        if held <= 0:
            continue
        holder = str(r["holder_id"])
        iata = str(r["airport_iata"]).upper().strip()
        used = int(r["used_this_week"] or 0)

        # (2) credit intent: movements this holder lost to cancellations still count as used.
        credited = used + _disrupted_movements(iata, holder, gw)
        util = float(min(credited, held)) / float(held)
        below = int(r["below_threshold_weeks"] or 0)
        if util < thr:
            below += 1
        else:
            below = 0

        # (1) warn before taking anything.
        if holder == "PLAYER" and below > 0:
            when = "at the end of this slot season" if mode == "season" else "next low week"
            if below >= max(1, grace):
                _notify_player_slot(
                    gw,
                    f"{iata}: used {credited}/{held} movements ({util * 100:.0f}%), below the "
                    f"{thr * 100:.0f}% requirement for {below} week(s). Unused quota lapses {when}.",
                )
            elif below == max(1, grace) - 1:
                _notify_player_slot(
                    gw,
                    f"{iata}: used {credited}/{held} movements ({util * 100:.0f}%). One more low "
                    f"week and unused quota starts lapsing {when}.",
                )

        new_held = held
        if enforcing and below >= max(1, grace) and util < thr:
            # (3) scale the loss to the shortfall instead of always exactly one unit.
            shortfall = max(0.0, thr - util)
            lose = int(math.ceil(shortfall * float(held)))
            lose = max(1, min(2, lose))
            # (4) never strip the last unit at the holder's own hub.
            floor = 1 if _holder_home_hub(holder) == iata else 0
            new_held = max(floor, held - lose)
            if new_held != held:
                below = 0
                if holder == "PLAYER":
                    _notify_player_slot(
                        gw,
                        f"{iata}: lost {held - new_held} movement unit(s) to use-it-or-lose-it "
                        f"({credited}/{held} used). You now hold {new_held}.",
                    )
            else:
                below = 0

        db.execute(
            """
            UPDATE slot_allocations
            SET slots_held = ?, below_threshold_weeks = ?
            WHERE allocation_id = ?
            """,
            (int(new_held), int(below), str(r["allocation_id"])),
        )


def ensure_slot_allocations_for_week(next_week: int) -> None:
    nw = int(next_week)
    prev = nw - 1
    if prev < 1:
        return
    rows = db.fetch_all("SELECT * FROM slot_allocations WHERE game_week = ?", (prev,))
    for r in rows:
        held = int(r["slots_held"] or 0)
        if held <= 0:
            continue
        db.execute(
            """
            INSERT OR IGNORE INTO slot_allocations (
                allocation_id, airport_iata, holder_id, game_week,
                slots_held, used_this_week, below_threshold_weeks
            ) VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (
                str(uuid.uuid4()),
                str(r["airport_iata"]),
                str(r["holder_id"]),
                nw,
                held,
                int(r["below_threshold_weeks"] or 0),
            ),
        )


def current_game_week() -> int:
    row = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    return int(row["game_week"] or 1) if row else 1


def slot_min_price_per_unit() -> float:
    return float(_const_num("slot_min_price_per_unit", 8000.0))


def slot_price_step() -> float:
    return float(_const_num("slot_price_step", 1000.0))


def slot_auction_units() -> int:
    return max(1, int(_const_num("slot_auction_units", 30.0)))


def slots_held(iata: str, holder_id: str, game_week: int) -> int:
    row = db.fetch_one(
        """
        SELECT slots_held FROM slot_allocations
        WHERE airport_iata = ? AND holder_id = ? AND game_week = ?
        """,
        (str(iata).upper().strip(), str(holder_id), int(game_week)),
    )
    return int(row["slots_held"] or 0) if row else 0


def ensure_min_slots_held(
    iata: str, holder_id: str, game_week: int, minimum: int
) -> None:
    """Raise a holder's weekly allocation to at least ``minimum`` (never lower)."""
    ap = str(iata).upper().strip()
    hid = str(holder_id)
    gw = int(game_week)
    need = max(0, int(minimum))
    if not ap or need <= 0 or not is_slot_controlled(ap):
        return
    row = db.fetch_one(
        """
        SELECT allocation_id, slots_held FROM slot_allocations
        WHERE airport_iata = ? AND holder_id = ? AND game_week = ?
        """,
        (ap, hid, gw),
    )
    if row and int(row["slots_held"] or 0) >= need:
        return
    if row:
        db.execute(
            "UPDATE slot_allocations SET slots_held = ? WHERE allocation_id = ?",
            (need, str(row["allocation_id"])),
        )
        return
    db.execute(
        """
        INSERT INTO slot_allocations (
            allocation_id, airport_iata, holder_id, game_week,
            slots_held, used_this_week, below_threshold_weeks
        ) VALUES (?, ?, ?, ?, ?, 0, 0)
        """,
        (str(uuid.uuid4()), ap, hid, gw, need),
    )


def seed_competitor_slot_capacity(
    competitor_id: str,
    airport_freq: Dict[str, int],
    *,
    start_week: int = 1,
    weeks_ahead: int | None = None,
) -> None:
    """
    Playable slot bootstrap for AI: hub + spokes get enough movements for their
    declared weekly frequency. Auctions still matter for growth beyond this floor.
    """
    if weeks_ahead is None:
        weeks_ahead = int(_const_num("ai_slot_seed_weeks", 16.0))
    cid = str(competitor_id)
    gw0 = max(1, int(start_week))
    for ap, freq in airport_freq.items():
        if not is_slot_controlled(ap):
            continue
        # Each weekly frequency is one round-trip cycle -> 2 movements per endpoint.
        need = max(8, 2 * max(1, int(freq)))
        for w in range(gw0, gw0 + max(1, int(weeks_ahead))):
            ensure_min_slots_held(ap, cid, w, need)


def slot_freq_cap(competitor_id: str, origin: str, dest: str, game_week: int) -> int:
    """Max weekly frequency allowed by slot holdings on this pair (one-way cycles)."""
    cid = str(competitor_id)
    gw = int(game_week)
    cap = 99
    for ap in (str(origin).upper(), str(dest).upper()):
        if not is_slot_controlled(ap):
            continue
        held = slots_held(ap, cid, gw)
        if held <= 0:
            return 0
        cap = min(cap, max(0, held // 2))
    return cap


def _upsert_slot_held(iata: str, holder_id: str, game_week: int, delta: int) -> int:
    ap = str(iata).upper().strip()
    hid = str(holder_id)
    gw = int(game_week)
    row = db.fetch_one(
        """
        SELECT allocation_id, slots_held FROM slot_allocations
        WHERE airport_iata = ? AND holder_id = ? AND game_week = ?
        """,
        (ap, hid, gw),
    )
    if not row:
        held = max(0, int(delta))
        db.execute(
            """
            INSERT INTO slot_allocations (
                allocation_id, airport_iata, holder_id, game_week,
                slots_held, used_this_week, below_threshold_weeks
            ) VALUES (?, ?, ?, ?, ?, 0, 0)
            """,
            (str(uuid.uuid4()), ap, hid, gw, held),
        )
        return held
    new_held = max(0, int(row["slots_held"] or 0) + int(delta))
    db.execute(
        "UPDATE slot_allocations SET slots_held = ? WHERE allocation_id = ?",
        (new_held, str(row["allocation_id"])),
    )
    return new_held


def _holders_for_week(game_week: int) -> list[str]:
    holders = ["PLAYER"]
    for r in db.fetch_all("SELECT competitor_id FROM competitors"):
        holders.append(str(r["competitor_id"]))
    return holders


def grandfather_historic_slot_holdings(game_week: int) -> None:
    """
    One-time historic grant: if a holder already flies a slot airport this week
    and has no allocation row, lock in current movements as slots_held so
    existing schedules are not wiped when quotas go live.
    """
    gw = int(game_week)
    seed_slot_controlled_airports()
    for r in db.fetch_all("SELECT iata FROM slot_controlled_airports"):
        ap = str(r["iata"])
        for hid in _holders_for_week(gw):
            exists = db.fetch_one(
                """
                SELECT 1 FROM slot_allocations
                WHERE airport_iata = ? AND holder_id = ? AND game_week = ?
                """,
                (ap, hid, gw),
            )
            if exists:
                continue
            used = int(sum(hourly_movements_at(ap, gw, holder_id=hid).values()))
            if used <= 0:
                continue
            _upsert_slot_held(ap, hid, gw, used)


def _sync_used_this_week(game_week: int) -> None:
    gw = int(game_week)
    grandfather_historic_slot_holdings(gw)
    rows = db.fetch_all(
        "SELECT allocation_id, airport_iata, holder_id FROM slot_allocations WHERE game_week = ?",
        (gw,),
    )
    for r in rows:
        used = int(
            sum(
                hourly_movements_at(str(r["airport_iata"]), gw, holder_id=str(r["holder_id"])).values()
            )
        )
        db.execute(
            "UPDATE slot_allocations SET used_this_week = ? WHERE allocation_id = ?",
            (used, str(r["allocation_id"])),
        )


def _notify_player_slot(game_week: int, body: str) -> None:
    try:
        from engine.gates import _notify_player

        _notify_player(int(game_week), "SLOT_AUCTION", body)
    except Exception:
        pass


def _round_up_step(x: float, step: float) -> float:
    import math

    if step <= 0:
        return float(x)
    return float(math.ceil(float(x) / step) * step)


def list_open_slot_auctions() -> list[dict]:
    seed_slot_controlled_airports()
    gw = current_game_week()
    rows = db.fetch_all(
        """
        SELECT * FROM slot_auctions
        WHERE status = 'OPEN' AND opens_week <= ? AND closes_week >= ?
        ORDER BY airport_iata
        """,
        (gw, gw),
    )
    return [dict(r) for r in (rows or [])]


def ensure_weekly_slot_auctions(opens_week: int) -> int:
    """One OPEN weekly auction per slot-controlled airport; closes same week."""
    ow = int(opens_week)
    seed_slot_controlled_airports()
    n = 0
    units = slot_auction_units()
    floor = slot_min_price_per_unit()
    for r in db.fetch_all("SELECT iata FROM slot_controlled_airports ORDER BY iata"):
        iata = str(r["iata"])
        exists = db.fetch_one(
            """
            SELECT auction_id, units_available FROM slot_auctions
            WHERE airport_iata = ? AND status = 'OPEN' AND opens_week = ?
            """,
            (iata, ow),
        )
        if exists:
            # Top up an auction that was created before slot_auction_units was raised,
            # otherwise a live week keeps offering the old (smaller) supply and the new
            # constant only takes effect next week. Never reduce: bids may already be in
            # against the larger pool.
            try:
                cur_units = int(exists["units_available"] or 0)
            except (KeyError, IndexError, TypeError, ValueError):
                cur_units = None
            if cur_units is not None and cur_units < int(units):
                db.execute(
                    "UPDATE slot_auctions SET units_available = ? WHERE auction_id = ?",
                    (int(units), str(exists["auction_id"])),
                )
            continue
        db.execute(
            """
            INSERT INTO slot_auctions (
                auction_id, airport_iata, opens_week, closes_week,
                units_available, current_price_per_unit, status
            ) VALUES (?, ?, ?, ?, ?, ?, 'OPEN')
            """,
            (str(uuid.uuid4()), iata, ow, ow, int(units), float(floor)),
        )
        n += 1
    return n


def submit_slot_bid(
    auction_id: str, units: int, price_per_unit: float, bidder_id: str = "PLAYER"
) -> None:
    aid = str(auction_id)
    u = int(units)
    if u <= 0:
        raise ValueError("units must be >= 1")
    p = float(price_per_unit)
    if p < slot_min_price_per_unit():
        raise ValueError(f"price_per_unit must be at least ${slot_min_price_per_unit():,.0f}")
    a = db.fetch_one("SELECT * FROM slot_auctions WHERE auction_id = ?", (aid,))
    if not a or str(a["status"]) != "OPEN":
        raise ValueError("Auction not found or not OPEN.")
    gw = current_game_week()
    if gw > int(a["closes_week"] or 0):
        raise ValueError("Auction already closed.")
    if bidder_id == "PLAYER":
        cash = db.fetch_one("SELECT cash FROM airline WHERE id = 1")
        if not cash or float(cash["cash"] or 0.0) < p * u:
            raise ValueError("Not enough cash to cover this max bid.")
    else:
        cash = db.fetch_one("SELECT cash FROM competitors WHERE competitor_id = ?", (bidder_id,))
        if not cash or float(cash["cash"] or 0.0) < p * u:
            return
    db.execute("DELETE FROM slot_bids WHERE auction_id = ? AND bidder_id = ?", (aid, bidder_id))
    db.execute(
        """
        INSERT INTO slot_bids (bid_id, auction_id, bidder_id, units_requested, price_per_unit, submitted_week)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (str(uuid.uuid4()), aid, bidder_id, u, p, gw),
    )
    cur = _round_up_step(p, slot_price_step())
    if cur > float(a["current_price_per_unit"] or 0.0):
        db.execute(
            "UPDATE slot_auctions SET current_price_per_unit = ? WHERE auction_id = ?",
            (cur, aid),
        )


def resolve_closing_slot_auctions(completed_game_week: int) -> int:
    """Pay-as-bid; awards increase next week's slots_held."""
    gw = int(completed_game_week)
    next_week = gw + 1
    auctions = db.fetch_all(
        "SELECT * FROM slot_auctions WHERE status = 'OPEN' AND closes_week = ?",
        (gw,),
    )
    n = 0
    for a in auctions:
        aid = str(a["auction_id"])
        iata = str(a["airport_iata"]).upper()
        avail = int(a["units_available"] or 0)
        player_bid = db.fetch_one(
            """
            SELECT units_requested, price_per_unit
            FROM slot_bids WHERE auction_id = ? AND bidder_id = 'PLAYER'
            """,
            (aid,),
        )
        bids = db.fetch_all(
            """
            SELECT bidder_id, units_requested, price_per_unit
            FROM slot_bids WHERE auction_id = ?
            ORDER BY price_per_unit DESC, units_requested DESC
            """,
            (aid,),
        )
        if not bids or avail <= 0:
            db.execute("UPDATE slot_auctions SET status = 'CANCELLED' WHERE auction_id = ?", (aid,))
            if player_bid:
                _notify_player_slot(gw, f"Slot auction cancelled: {iata} had no units this week.")
            continue
        remaining = avail
        player_won = 0
        player_cost = 0.0
        for b in bids:
            if remaining <= 0:
                break
            bidder = str(b["bidder_id"])
            want = max(0, int(b["units_requested"] or 0))
            if want <= 0:
                continue
            take = min(remaining, want)
            price = float(b["price_per_unit"] or 0.0)
            cost = price * float(take)
            if bidder == "PLAYER":
                db.execute("UPDATE airline SET cash = cash - ? WHERE id = 1", (cost,))
                player_won += int(take)
                player_cost += float(cost)
            else:
                db.execute(
                    "UPDATE competitors SET cash = cash - ? WHERE competitor_id = ?",
                    (cost, bidder),
                )
            _upsert_slot_held(iata, bidder, next_week, take)
            remaining -= take
            if bidder != "PLAYER" and take > 0:
                try:
                    from engine.ai_log import append_ai_narrative, competitor_display_name, push_ai_news

                    append_ai_narrative(bidder, f"WON_SLOT:{iata}:{int(take)}")
                    push_ai_news(
                        f"⏱ {competitor_display_name(bidder)} wins {int(take)} slot(s) at {iata}"
                    )
                except Exception:
                    pass
        db.execute("UPDATE slot_auctions SET status = 'RESOLVED' WHERE auction_id = ?", (aid,))
        n += 1
        if player_bid:
            want_u = int(player_bid["units_requested"] or 0)
            price = float(player_bid["price_per_unit"] or 0.0)
            if player_won > 0:
                _notify_player_slot(
                    gw,
                    f"Slot auction result: {iata} — WON {player_won}/{want_u} movement right(s) "
                    f"@ ${price:,.0f} (paid ${player_cost:,.0f}). Effective week {next_week}.",
                )
            else:
                _notify_player_slot(
                    gw,
                    f"Slot auction result: {iata} — lost {want_u} unit bid @ ${price:,.0f}/unit.",
                )
    return n


def holder_can_add_movements(
    iata: str, holder_id: str, game_week: int, extra: int
) -> bool:
    if extra <= 0:
        return True
    ap = str(iata).upper().strip()
    if not is_slot_controlled(ap):
        return True
    grandfather_historic_slot_holdings(int(game_week))
    held = slots_held(ap, holder_id, int(game_week))
    used = int(sum(hourly_movements_at(ap, int(game_week), holder_id=holder_id).values()))
    return used + int(extra) <= held
