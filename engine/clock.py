"""
Continuous GameClock — threading.Thread advancing game time with speed multipliers.

Time scale: 30 real seconds = 1 game hour at 1× (same as existing product docs).
game_hours_elapsed += (real_seconds_elapsed × speed_multiplier) / real_seconds_per_game_hour
which matches (real_seconds × speed) / 30 for the 30s/hour model (not /3600).
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
import sys
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from db import db

# Pause + simulation speed tiers exposed in UI/CLI/API.
ALLOWED_SPEED_MULTIPLIERS = (0, 1, 2, 4, 20, 60)

# The run loop refreshes the time snapshot about once a second. Projecting further
# than this beyond the last catch-up means the clock thread is wedged, and the honest
# answer is "time stopped", not a HUD that teleports hours into the future.
MAX_EXTRAPOLATION_REAL_SECONDS = 2.0

# Readers treat the clock as stalled once the run loop has missed this many seconds.
STALL_WARN_SECONDS = 5.0

# Callbacks run on a worker thread. If it falls this far behind, auto-pause rather
# than let the backlog grow without bound (a visible pause beats a silent wedge).
MAX_PENDING_CALLBACK_JOBS = 120


def calendar_week_from_game_hours(game_hours_elapsed: float) -> int:
    """1-based calendar week from absolute game hours (week 1 starts at hour 0)."""
    return int(float(game_hours_elapsed) // 168.0) + 1


def clock_thread_active() -> bool:
    clk = get_global_clock()
    return clk is not None and clk.is_alive()


def _cash_warning_threshold() -> float:
    row = db.fetch_one(
        "SELECT value FROM financial_constants WHERE key = ?",
        ("cash_warning_threshold",),
    )
    if row:
        return float(row["value"])
    return 25_000_000.0


class GameClock(threading.Thread):
    """
    Advances game_hours_elapsed; fires departure/arrival callbacks and weekly settlement hook.
    Persists game_state each tick and whenever speed changes.

    Threading contract — `self.lock` guards ONLY the time snapshot fields
    (game_hours_elapsed, speed_multiplier, _snapshot_*, _last_tick, milestone
    counters, last_auto_pause_reason). It is never held across a callback, a DB
    write, or any engine call.

    Callbacks (on_tick/on_hour/on_day/on_week/on_departure/on_arrival/AI variants)
    run on a dedicated FIFO worker thread, never on the clock thread. That matters
    because those callbacks legitimately call back into the clock — trigger_aog()
    reaches request_auto_pause(), and the week-roll spawn reaches
    get_display_game_hours(). Holding the lock across them deadlocked the clock
    thread outright, which froze every HTTP reader waiting on the same lock.
    The lock is an RLock as a second line of defence for any path not yet audited.
    """

    def __init__(
        self,
        on_tick=None,
        on_hour=None,
        on_day=None,
        on_week=None,
        on_departure=None,
        on_arrival=None,
        on_ai_departure=None,
        on_ai_arrival=None,
        resume_from_hours=0.0,
    ):
        super().__init__(daemon=True)

        self.real_seconds_per_game_hour = 30

        st = db.fetch_one(
            "SELECT game_hours_elapsed, speed_multiplier FROM game_state WHERE id = 1"
        )
        if st:
            self.game_hours_elapsed = float(
                st["game_hours_elapsed"] if st["game_hours_elapsed"] is not None else 0.0
            )
            self.speed_multiplier = int(
                st["speed_multiplier"] if st["speed_multiplier"] is not None else 0
            )
        else:
            self.game_hours_elapsed = float(resume_from_hours)
            self.speed_multiplier = 0

        self.running = True

        self.on_tick = on_tick
        self.on_hour = on_hour
        self.on_day = on_day
        self.on_week = on_week
        self.on_departure = on_departure
        self.on_arrival = on_arrival
        self.on_ai_departure = on_ai_departure
        self.on_ai_arrival = on_ai_arrival

        ghe = self.game_hours_elapsed
        self.last_hour = int(ghe)
        self.last_day = int(ghe // 24)
        self.last_week = int(ghe // 168) + 1
        self._last_flight_event_hour = float(ghe)

        # The database this clock belongs to. A clock must never persist into a
        # different file than the one it read its hours from: tests repoint
        # db.DB_FILE at a temp copy and restore it on teardown, and a clock whose
        # first write lands after that restore would otherwise stamp its temp-world
        # hour (near 0) onto the player's real save.
        self._db_file = db.DB_FILE

        # RLock, not Lock: callbacks re-enter the clock (see class docstring).
        self.lock = threading.RLock()
        self._snapshot_ghe = float(ghe)
        self._snapshot_wall_time = time.time()
        self._last_tick = self._snapshot_wall_time

        # Callback dispatch runs off the clock thread, in submission order.
        self._jobs: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._backlog_paused = False

        self._known_aog_tails: set[str] = set()
        self._cash_was_safe = True
        self._weather_pause_sent = False
        self.last_auto_pause_reason: Optional[str] = None
        self._sync_aog_from_db()

    def _catch_up_locked(self, now: float | None = None) -> None:
        """Apply wall-clock time since the last snapshot at the current speed."""
        now = time.time() if now is None else float(now)
        sp = int(self.speed_multiplier)
        if sp != 0:
            dt = max(0.0, now - float(self._snapshot_wall_time))
            if dt > 0:
                self.game_hours_elapsed = float(self._snapshot_ghe) + (
                    dt / float(self.real_seconds_per_game_hour)
                ) * sp
        self._snapshot_ghe = float(self.game_hours_elapsed)
        self._snapshot_wall_time = now
        self._last_tick = now

    # ------------------------------------------------------------------
    # Callback worker — every user callback runs here, never on the clock thread.
    # ------------------------------------------------------------------

    def _start_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._worker_loop, name="game-clock-callbacks", daemon=True
        )
        self._worker.start()

    def _enqueue(self, kind: str, payload) -> None:
        self._jobs.put((kind, payload))

    def _worker_loop(self) -> None:
        while True:
            item = self._jobs.get()
            try:
                if item is None:
                    return
                kind, payload = item
                self._run_job(kind, payload)
            except Exception:
                pass
            finally:
                self._jobs.task_done()

    def _run_job(self, kind: str, payload) -> None:
        try:
            if kind == "tick":
                if self.on_tick:
                    self.on_tick(payload[0], payload[1])
            elif kind == "hour":
                if self.on_hour:
                    self.on_hour(payload)
            elif kind == "day":
                if self.on_day:
                    self.on_day(payload)
            elif kind == "week":
                if self.on_week:
                    self.on_week(payload)
            elif kind == "flights":
                self._dispatch_flight_events(payload[0], payload[1])
        except Exception as exc:
            self._push_news_once(f"clock_job_{kind}", f"⚠ Clock {kind} handler failed: {exc}")

    def _push_news_once(self, key: str, message: str) -> None:
        """Report a recurring failure once per healthy streak, not once per tick."""
        sent = getattr(self, "_news_sent", None)
        if sent is None:
            sent = set()
            self._news_sent = sent
        if key in sent:
            return
        sent.add(key)
        try:
            from engine.news_feed import push_news

            push_news(message)
        except Exception:
            pass

    def _clear_news_once(self, key: str) -> None:
        sent = getattr(self, "_news_sent", None)
        if sent:
            sent.discard(key)

    def run(self):
        self._start_worker()
        with self.lock:
            self._last_tick = time.time()
            self._snapshot_wall_time = self._last_tick
        while self.running:
            time.sleep(1)
            try:
                now = time.time()
                with self.lock:
                    if self.speed_multiplier == 0:
                        self._snapshot_wall_time = now
                        self._last_tick = now
                        continue
                    self._catch_up_locked(now)
                    ghe = float(self.game_hours_elapsed)
                    sp = int(self.speed_multiplier)
                self._safe_persist()
                self._enqueue("tick", (ghe, sp))
                # Catch up time spent in persist, then flush BEFORE queuing week
                # milestones so on_week / spawn cannot run ahead of SQLite.
                with self.lock:
                    self._catch_up_locked()
                self._safe_persist()
                # Counters advance under the lock; the handlers run on the worker.
                with self.lock:
                    milestones = self._collect_milestones_locked()
                    window = self._collect_flight_window_locked()
                for kind, arg in milestones:
                    self._enqueue(kind, arg)
                if window is not None:
                    self._enqueue("flights", window)

                self._check_backlog()
                self._check_auto_pause_conditions()
            except Exception:
                with self.lock:
                    self._last_tick = time.time()
                    self._snapshot_wall_time = self._last_tick

    def _safe_persist(self) -> None:
        try:
            self._persist_game_state_unlocked()
            self._clear_news_once("clock_persist")
        except Exception as exc:
            self._push_news_once("clock_persist", f"⚠ Clock persist failed: {exc}")

    def _check_backlog(self) -> None:
        """Auto-pause if callbacks cannot keep up, instead of growing the queue forever."""
        depth = self._jobs.qsize()
        if depth > MAX_PENDING_CALLBACK_JOBS:
            if not self._backlog_paused:
                self._backlog_paused = True
                self.request_auto_pause(
                    "Simulation fell behind at this speed — paused to catch up"
                )
        elif depth == 0:
            self._backlog_paused = False

    def pending_callback_jobs(self) -> int:
        return self._jobs.qsize()

    def stall_seconds(self) -> float:
        """Real seconds since the run loop last refreshed the snapshot."""
        with self.lock:
            last = float(self._last_tick)
        return max(0.0, time.time() - last)

    def is_stalled(self) -> bool:
        return self.is_alive() and self.stall_seconds() > STALL_WARN_SECONDS

    def get_interpolated_game_hours(self):
        with self.lock:
            base = self._snapshot_ghe
            t0 = self._snapshot_wall_time
            sp = self.speed_multiplier
        if sp == 0:
            return base
        dt = max(0.0, time.time() - t0)
        # A live run loop refreshes the snapshot every ~1s. When it stops doing so
        # the clock is wedged; projecting further would race the HUD hours ahead of
        # the sim. Unstarted clocks (tests, direct use) keep the raw projection.
        if self.is_alive():
            dt = min(dt, MAX_EXTRAPOLATION_REAL_SECONDS)
        return base + (dt / self.real_seconds_per_game_hour) * sp

    def get_committed_game_hours(self) -> float:
        """Last catch-up'd hours — matches what persist writes / restart reloads."""
        with self.lock:
            return float(self._snapshot_ghe)

    def _persist_game_state_unlocked(self):
        """Snapshot under the lock, write outside it — readers never wait on SQLite."""
        if db.DB_FILE != self._db_file:
            # The save this clock belongs to is no longer the active database.
            # Writing now would corrupt whatever took its place.
            return
        with self.lock:
            ghe = float(self.game_hours_elapsed)
            sp = int(self.speed_multiplier)
        current_week = int(ghe // 168) + 1
        db.execute(
            """
            UPDATE game_state
            SET game_hours_elapsed = ?,
                game_week = ?,
                speed_multiplier = ?
            WHERE id = 1
            """,
            (ghe, current_week, sp),
        )

    def _collect_milestones_locked(self):
        """Advance milestone counters and return the handlers to run off-thread."""
        out: list[tuple[str, int]] = []
        ghe = self.game_hours_elapsed

        ch = int(ghe)
        while ch > self.last_hour:
            self.last_hour += 1
            if self.on_hour:
                out.append(("hour", self.last_hour))

        cd = int(ghe // 24)
        while cd > self.last_day:
            self.last_day += 1
            if self.on_day:
                out.append(("day", self.last_day))

        new_calendar_week = int(ghe // 168) + 1
        while new_calendar_week > self.last_week:
            self.last_week += 1
            if self.on_week:
                out.append(("week", self.last_week))
        return out

    def _collect_flight_window_locked(self):
        """Claim the (t_prev, t_now] event window; dispatch happens off-thread."""
        if not self.on_departure and not self.on_arrival and not self.on_ai_departure and not self.on_ai_arrival:
            return None
        t0 = float(self._last_flight_event_hour)
        t1 = float(self.game_hours_elapsed)
        if t1 <= t0:
            return None
        self._last_flight_event_hour = t1
        return (t0, t1)

    def _check_time_milestones_locked(self):
        """Synchronous collect+dispatch. Test/CLI helper — run() uses the worker instead."""
        for kind, arg in self._collect_milestones_locked():
            self._run_job(kind, arg)

    def _check_flight_events_locked(self):
        """Synchronous collect+dispatch. Test/CLI helper — run() uses the worker instead."""
        window = self._collect_flight_window_locked()
        if window is not None:
            self._dispatch_flight_events(window[0], window[1])

    def _dispatch_flight_events(self, t0: float, t1: float):
        try:
            from engine.events import process_events_queue

            process_events_queue(t0, t1)

            from engine.scheduling import get_flight_events_between

            events = get_flight_events_between(t0, t1)
            if self.on_departure:
                for segment_id in events.get("departures", []):
                    try:
                        self.on_departure(segment_id)
                    except Exception as e:
                        try:
                            from engine.news_feed import push_news

                            push_news(f"⚠ Departure handler failed for {segment_id}: {e}")
                        except Exception:
                            pass
            if self.on_arrival:
                for segment_id in events.get("arrivals", []):
                    try:
                        self.on_arrival(segment_id)
                    except Exception as e:
                        try:
                            from engine.news_feed import push_news

                            push_news(f"⚠ Arrival handler failed for {segment_id}: {e}")
                        except Exception:
                            pass
            if self.on_ai_departure:
                for segment_id in events.get("ai_departures", []):
                    try:
                        self.on_ai_departure(segment_id)
                    except Exception:
                        pass
            if self.on_ai_arrival:
                for segment_id in events.get("ai_arrivals", []):
                    try:
                        self.on_ai_arrival(segment_id)
                    except Exception:
                        pass
            self._clear_news_once("clock_events")
        except Exception as exc:
            # The window was already claimed by _collect_flight_window_locked(), so
            # a failure here drops that slice rather than replaying it forever.
            self._push_news_once("clock_events", f"⚠ Clock event dispatch failed: {exc}")

    def _sync_aog_from_db(self) -> None:
        rows = db.fetch_all("SELECT tail_number FROM fleet WHERE status = 'AOG'")
        self._known_aog_tails = {str(r["tail_number"]) for r in rows}

    def request_auto_pause(self, reason: str) -> None:
        """Engine-driven pause (AOG, weather, low cash). Does not stop the thread."""
        with self.lock:
            self._catch_up_locked()
            self.speed_multiplier = 0
            self._snapshot_ghe = float(self.game_hours_elapsed)
            self._snapshot_wall_time = time.time()
            self._last_tick = self._snapshot_wall_time
            self.last_auto_pause_reason = reason
        self._persist_game_state_unlocked()
        self._sync_aog_from_db()
        try:
            from engine.news_feed import push_news

            push_news(f"⚠ AUTO-PAUSE: {reason}")
        except Exception:
            pass

    def clear_auto_pause_alert(self) -> None:
        with self.lock:
            self.last_auto_pause_reason = None

    def set_speed(self, multiplier, *, player_initiated: bool = False):
        if multiplier not in ALLOWED_SPEED_MULTIPLIERS:
            allowed = ", ".join(str(x) for x in ALLOWED_SPEED_MULTIPLIERS)
            return False, f"Invalid speed multiplier: {multiplier}. Must be one of: {allowed}."

        with self.lock:
            old_speed = self.speed_multiplier
            # Accrue time at the *old* speed first so 20× → 1× does not rewind.
            self._catch_up_locked()
            self.speed_multiplier = multiplier
            if player_initiated and multiplier > 0:
                self.last_auto_pause_reason = None
            self._snapshot_ghe = float(self.game_hours_elapsed)
            self._snapshot_wall_time = time.time()
            self._last_tick = self._snapshot_wall_time

        self._persist_game_state_unlocked()

        if multiplier == 0:
            return True, "Game paused"
        if old_speed == 0:
            return True, f"Game resumed at {multiplier}× speed"
        return True, f"Speed changed to {multiplier}×"

    def pause(self, *, player_initiated: bool = True):
        return self.set_speed(0, player_initiated=player_initiated)

    def resume(self, speed=1, *, player_initiated: bool = True):
        return self.set_speed(speed, player_initiated=player_initiated)

    def stop(self):
        """Signal the thread to exit and flush catch-up'd hours to SQLite."""
        self.running = False
        try:
            with self.lock:
                self._catch_up_locked()
            self._persist_game_state_unlocked()
        except Exception:
            pass
        # Wake the callback worker so it can exit instead of blocking on get().
        try:
            self._jobs.put(None)
            if self._worker is not None and self._worker.is_alive():
                self._worker.join(timeout=2)
        except Exception:
            pass

    def _check_auto_pause_conditions(self):
        """AOG, weather+active flights, cash warning — only when clock was running."""
        from engine.environment import (
            active_weather_closures,
            weather_closure_hits_active_flights,
        )

        # AOG
        rows = db.fetch_all(
            "SELECT tail_number FROM fleet WHERE status = 'AOG'"
        )
        aog_tails = {str(r["tail_number"]) for r in rows}
        new_aog = aog_tails - self._known_aog_tails
        self._known_aog_tails = aog_tails
        if new_aog:
            tail = sorted(new_aog)[0]
            row = db.fetch_one(
                "SELECT aog_reason FROM fleet WHERE tail_number = ?", (tail,)
            )
            reason = (row["aog_reason"] if row else None) or "AOG"
            self.request_auto_pause(f"Aircraft {tail} is AOG ({reason})")
            return

        # Weather
        if active_weather_closures():
            try:
                wreason = weather_closure_hits_active_flights()
            except Exception:
                wreason = None
            if wreason:
                if not self._weather_pause_sent:
                    self._weather_pause_sent = True
                    self.request_auto_pause(wreason)
                return
        self._weather_pause_sent = False

        # Cash
        air = db.fetch_one("SELECT cash FROM airline WHERE id = 1")
        if not air:
            return
        cash = float(air["cash"] or 0.0)
        thr = _cash_warning_threshold()
        if cash >= thr:
            self._cash_was_safe = True
            return
        if self._cash_was_safe and cash < thr:
            self._cash_was_safe = False
            self.request_auto_pause(
                f"Cash ${cash:,.0f} below warning threshold (${thr:,.0f})"
            )

    def get_status(self):
        # Week/day labels use committed hours (what SQLite stores). Interpolated
        # hours stay available for smooth map motion without racing the calendar.
        with self.lock:
            committed = float(self._snapshot_ghe)
            sp = int(self.speed_multiplier)
            alert = self.last_auto_pause_reason
            sec = float(self.real_seconds_per_game_hour)
        ghe_live = float(self.get_interpolated_game_hours())
        current_week = int(committed // 168) + 1
        hours_in_week = committed % 168.0
        current_day = int(hours_in_week // 24) + 1
        hod = hours_in_week % 24.0
        current_hour = int(hod)
        mins = int((hod - current_hour) * 60.0) % 60
        spd = "paused" if sp == 0 else f"{sp}×"
        return {
            "game_hours_elapsed": committed,
            "committed_game_hour": committed,
            "current_game_hour": ghe_live,
            "current_week": current_week,
            "current_day": current_day,
            "current_hour": current_hour,
            "speed_multiplier": sp,
            "is_paused": sp == 0,
            "real_seconds_per_game_hour": sec,
            "time_display": (
                f"Week {current_week} · Day {current_day} · {current_hour:02d}:{mins:02d} · {spd}"
            ),
            "auto_pause_alert": alert,
            "clock_stalled": self.is_stalled(),
            "pending_callback_jobs": self.pending_callback_jobs(),
        }


def is_clock_running():
    game_state = db.fetch_one("SELECT speed_multiplier FROM game_state WHERE id = 1")
    if not game_state:
        return False
    return game_state["speed_multiplier"] > 0


def get_display_game_hours():
    clk = get_global_clock()
    if clk is not None and clk.is_alive():
        return clk.get_interpolated_game_hours()
    gt = get_game_time()
    return float(gt["game_hours_elapsed"]) if gt else 0.0


def get_api_clock_status() -> dict:
    """
    Authoritative clock payload for HTTP/UI.

    When the clock thread is not running, report speed 0 so the browser does not
    extrapolate ahead of persisted game_hours_elapsed (stale DB speed_multiplier
    would otherwise make the HUD race weeks ahead of the sim).

    When alive: week/day/time_display come from committed hours; current_game_hour
    may be wall-interpolated for smooth map motion.
    """
    clk = get_global_clock()
    if clk is not None and clk.is_alive():
        status = clk.get_status()
        status["clock_alive"] = True
        return status

    ghe = float(get_display_game_hours())
    week = calendar_week_from_game_hours(ghe)
    hours_in_week = ghe % 168.0
    day = int(hours_in_week // 24) + 1
    hod = hours_in_week % 24.0
    hh = int(hod)
    mm = int((hod - hh) * 60.0) % 60
    return {
        "game_hours_elapsed": ghe,
        "committed_game_hour": ghe,
        "current_game_hour": ghe,
        "current_week": week,
        "current_day": day,
        "current_hour": hh,
        "speed_multiplier": 0,
        "is_paused": True,
        "real_seconds_per_game_hour": 30.0,
        "time_display": f"Week {week} · Day {day} · {hh:02d}:{mm:02d} · paused",
        "auto_pause_alert": None,
        "clock_alive": False,
        "clock_stalled": False,
        "pending_callback_jobs": 0,
    }


def get_game_time():
    game_state = db.fetch_one(
        """
        SELECT game_hours_elapsed, game_week, speed_multiplier
        FROM game_state WHERE id = 1
        """
    )
    if not game_state:
        return None
    return {
        "game_hours_elapsed": game_state["game_hours_elapsed"],
        "game_week": game_state["game_week"],
        "speed_multiplier": game_state["speed_multiplier"],
    }


def can_schedule_flights():
    game_time = get_game_time()
    if not game_time:
        return False, "Game not initialized"
    return True, "Ready to schedule flights"


def init_game_state():
    fuel0 = 195.0
    row = db.fetch_one(
        "SELECT value FROM financial_constants WHERE key = 'fuel_base_price_bbl'"
    )
    if row:
        try:
            fuel0 = float(row["value"])
        except (TypeError, ValueError):
            fuel0 = 195.0
    db.execute(
        """
        INSERT OR REPLACE INTO game_state (
            id, schema_version, game_week, game_hours_elapsed,
            speed_multiplier, current_month,
            fuel_price_current, fuel_price_trend,
            demand_noise_seed, pause_on_week_summary, ui_blackout
        ) VALUES (1, 1, 1, 0.0, 0, 1, ?, 0.0, 1, 0, 0)
        """,
        (fuel0,),
    )


_global_clock: GameClock | None = None


def start_game_clock(
    on_week=None,
    on_tick=None,
    on_departure=None,
    on_arrival=None,
    *,
    on_ai_departure=None,
    on_ai_arrival=None,
):
    global _global_clock
    if _global_clock is not None and _global_clock.is_alive():
        try:
            from engine.events import schedule_weekly_events

            gt = get_game_time()
            if gt:
                schedule_weekly_events(int(gt["game_week"]))
        except Exception:
            pass
        return _global_clock
    game_time = get_game_time()
    resume_from = game_time["game_hours_elapsed"] if game_time else 0.0
    _global_clock = GameClock(
        on_week=on_week,
        on_tick=on_tick,
        on_departure=on_departure,
        on_arrival=on_arrival,
        on_ai_departure=on_ai_departure,
        on_ai_arrival=on_ai_arrival,
        resume_from_hours=resume_from,
    )
    _global_clock.start()
    try:
        from engine.events import schedule_weekly_events

        gt = get_game_time()
        if gt:
            schedule_weekly_events(int(gt["game_week"]))
    except Exception:
        pass
    return _global_clock


def get_global_clock():
    return _global_clock


def stop_game_clock():
    global _global_clock
    if _global_clock:
        _global_clock.stop()
        # join() raises RuntimeError if start() never ran (e.g. failed boot / test teardown).
        if getattr(_global_clock, "ident", None) is not None:
            _global_clock.join(timeout=2)
        _global_clock = None
