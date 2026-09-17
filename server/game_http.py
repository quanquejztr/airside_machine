"""
Localhost game UI server: full-screen map + overlay windows.

Bind 127.0.0.1 only. Same SQLite save as the CLI.
"""

from __future__ import annotations

import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from db import db
from engine import setup

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_server: Optional[ThreadingHTTPServer] = None
_server_lock = threading.Lock()


_ai_boot_lock = threading.Lock()
_ai_boot_started = False
_ops_seeded = False
_player_spawn_healed_week: int | None = None
_player_spawn_lock = threading.Lock()


def _seed_auctions_and_flights() -> None:
    try:
        from engine.gates import ensure_weekly_airport_auctions
        from engine.slots import (
            ensure_weekly_slot_auctions,
            grandfather_historic_slot_holdings,
            seed_slot_controlled_airports,
        )

        gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
        cur_week = int(gs["game_week"]) if gs and gs["game_week"] is not None else 1
        seed_slot_controlled_airports()
        grandfather_historic_slot_holdings(cur_week)
        ensure_weekly_slot_auctions(cur_week)
        ensure_weekly_airport_auctions(cur_week)
    except Exception:
        pass


def _ai_bootstrap_bg() -> None:
    try:
        from engine.ai import ai_bootstrap_if_needed, ensure_competitors_seeded
        from engine.ai_flights import spawn_ai_segments_for_week

        ensure_competitors_seeded()
        gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
        cur_week = int(gs["game_week"]) if gs and gs["game_week"] is not None else 1
        ai_bootstrap_if_needed(cur_week)
        spawn_ai_segments_for_week(cur_week)
        # Self-heal: if the AI still has no flights for this week (bootstrap skipped because a
        # turn was already logged, or a prior run died half-way), rebuild them. Without this the
        # competitors exist but never appear on the map or the board.
        row = db.fetch_one(
            "SELECT COUNT(*) AS n FROM ai_flight_segments WHERE game_week = ?",
            (cur_week,),
        )
        if int((row["n"] if row else 0) or 0) <= 0:
            spawn_ai_segments_for_week(cur_week)
    except Exception:
        # Never crash the server thread, but do not hide the reason either.
        import sys
        import traceback

        traceback.print_exc(file=sys.stderr)
        try:
            from engine.news_feed import push_news

            push_news("⚠ AI bootstrap failed — competitors may not be flying (see console).")
        except Exception:
            pass


def ensure_runtime_clock():
    """Start the game clock immediately. AI seed work is backgrounded so 20× is not blocked."""
    return _start_runtime_clock(full_bootstrap=True)


def ensure_clock_running():
    """Keep the clock thread alive without repeating heavy boot work."""
    return _start_runtime_clock(full_bootstrap=False)


def _on_week_roll(new_week: int) -> None:
    if new_week <= 1:
        return
    from engine.settlement import (
        enqueue_settlement_after_week_boundary,
        spawn_segments_for_calendar_week,
    )

    try:
        spawn_segments_for_calendar_week(new_week)
    except Exception:
        pass
    enqueue_settlement_after_week_boundary(new_week)


def _heal_player_segments_if_missing() -> None:
    """Spawn the current week's player flights if the week roll never fired.

    GameClock sets ``last_week = int(ghe // 168) + 1`` at construction, and the milestone
    loop only fires ``on_week`` when the calendar week exceeds it. So a server started
    while the clock already sits inside week N never fires on_week(N), and
    ``spawn_segments_for_calendar_week`` never runs for that week. Nothing raises — the
    step simply does not happen — so no error path can catch it.

    ``main.py`` has always had this net for the CLI; the web server did not, and the AI
    had its own self-heal in ``_ai_bootstrap_bg``. The result was a week with a full AI
    schedule and no player flights at all: every aircraft reading 0.0h/168h, which looks
    like deleted schedules rather than a step that was skipped.

    Spawning is idempotent and dedupes, so this is safe to attempt whenever the current
    week looks empty. Guarded to once per week per process to keep it off the hot path.
    """
    global _player_spawn_healed_week
    try:
        from engine.scheduling import spawn_rotation_segments_for_week

        gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
        cur_week = int(gs["game_week"]) if gs and gs["game_week"] is not None else 1
        with _player_spawn_lock:
            if _player_spawn_healed_week == cur_week:
                return
            _player_spawn_healed_week = cur_week
        tpl = db.fetch_one("SELECT COUNT(*) AS n FROM weekly_rotations")
        if int((tpl["n"] if tpl else 0) or 0) <= 0:
            return
        out = spawn_rotation_segments_for_week(cur_week)
        made = int((out or {}).get("inserted") or 0)
        # A normal mid-week start inserts nothing, because the roll already spawned this
        # week. Anything created here means the roll did not run, so say so — a week that
        # silently has no player flights is the failure this exists to catch.
        if made > 0:
            try:
                from engine.news_feed import push_news

                push_news(
                    f"Week {cur_week} player schedule was missing and has been rebuilt "
                    f"({made} flights) — the week roll did not run for this week."
                )
            except Exception:
                pass
    except Exception:
        pass


def _on_day_roll(game_day: int) -> None:
    """Charge a day of any open stand shortfall, and close the ones already cleared.

    main.py wired this for the CLI; the web server did not, so under the UI the daily
    accrual never ran at all: shortfalls never resolved themselves and no penalty was
    ever charged. Same shape of gap as the missing week-roll safety net in BUGFIX-10 —
    behaviour attached to one entry point and not the other.
    """
    try:
        from engine.gates import accrue_gate_shortfall_penalties

        accrue_gate_shortfall_penalties(int(game_day))
    except Exception:
        pass


def _start_runtime_clock(*, full_bootstrap: bool):
    from engine.clock import get_global_clock, start_game_clock
    from engine.scheduling import on_arrival, on_departure
    from engine.ai_flights import ai_on_arrival as ai_on_arrival_cb
    from engine.ai_flights import ai_on_departure as ai_on_departure_cb

    if not setup.airline_exists():
        return None

    existing = get_global_clock()
    if existing is not None and existing.is_alive():
        return existing

    def on_fuel_tick(game_hours_elapsed, speed_multiplier):
        try:
            from engine import fuel as fuel_mod

            fuel_mod.tick_fuel_price(game_hours_elapsed, speed_multiplier)
        except Exception:
            pass

    existing = start_game_clock(
        on_week=_on_week_roll,
        on_day=_on_day_roll,
        on_tick=on_fuel_tick,
        on_departure=on_departure,
        on_arrival=on_arrival,
        on_ai_departure=ai_on_departure_cb,
        on_ai_arrival=ai_on_arrival_cb,
    )

    # Must run in BOTH modes: the gap appears at process start, and a lightweight
    # session may never take the full-bootstrap path.
    _heal_player_segments_if_missing()

    if not full_bootstrap:
        return existing

    try:
        from engine.settlement import catch_up_missing_settlements

        catch_up_missing_settlements()
    except Exception as e:
        try:
            from engine.news_feed import push_news

            push_news(f"⚠ Settlement catch-up failed: {e}")
        except Exception:
            pass

    global _ops_seeded, _ai_boot_started
    if not _ops_seeded:
        _ops_seeded = True
        _seed_auctions_and_flights()
    # Retry the AI bootstrap if it has not actually produced anything yet. A one-shot flag
    # alone means a first attempt that died silently is never retried, and the competitors
    # then stay invisible for the whole session.
    ai_missing = True
    try:
        gs_w = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
        wk = int(gs_w["game_week"]) if gs_w and gs_w["game_week"] is not None else 1
        row = db.fetch_one(
            "SELECT COUNT(*) AS n FROM ai_flight_segments WHERE game_week = ?", (wk,)
        )
        ai_missing = int((row["n"] if row else 0) or 0) <= 0
    except Exception:
        ai_missing = True
    with _ai_boot_lock:
        already = _ai_boot_started and not ai_missing
        if not already:
            _ai_boot_started = True
    if not already:
        threading.Thread(target=_ai_bootstrap_bg, name="ai_bootstrap", daemon=True).start()
    return existing


def _json_bytes(payload: dict, status: int = 200) -> tuple[bytes, int]:
    return json.dumps(payload, default=str).encode("utf-8"), status


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _send_json(self, payload: dict, status: int | None = None) -> None:
        if not isinstance(payload, dict):
            payload = {"ok": False, "error": "Server returned an empty response."}
        if status is None:
            status = 200 if payload.get("ok", True) else 400
        raw, status = _json_bytes(payload, status)
        self._send(raw, "application/json; charset=utf-8", status)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.end_headers()

    def _guard(self, fn) -> None:
        """
        Turn any API exception into a visible JSON error plus a stderr traceback.

        Without this, an engine exception escapes do_GET/do_POST, no JSON body is sent, and
        log_message() is suppressed — so the dock renders blank and nothing is logged anywhere.
        """
        try:
            fn()
        except Exception as e:
            import sys
            import traceback

            traceback.print_exc(file=sys.stderr)
            try:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            except Exception:
                pass

    def do_GET(self) -> None:
        self._guard(self._handle_get)

    def do_POST(self) -> None:
        self._guard(self._handle_post)

    def _handle_get(self) -> None:
        parsed = urlparse(self.path or "/")
        path = parsed.path.rstrip("/") or "/"
        q = {k: v[0] if v else "" for k, v in parse_qs(parsed.query).items()}
        from server import game_api as api

        if path == "/api/state":
            ensure_clock_running()
            self._send_json(api.get_state())
            return
        if path == "/api/flight-map.json":
            ensure_clock_running()
            self._send_json(api.flight_map())
            return
        if path == "/api/airports-map.json":
            self._send_json(api.airports_map())
            return
        if path == "/api/airports/routes":
            self._send_json(api.airport_routes(q.get("iata", "")))
            return
        if path == "/api/airports":
            self._send_json(api.search_airports(q.get("q", "")))
            return
        if path == "/api/catalog":
            self._send_json(api.list_catalog(q.get("category")))
            return
        if path == "/api/catalog/cabin":
            self._send_json(api.cabin_layout(q.get("type_id", "")))
            return
        if path == "/api/hub-candidates":
            self._send_json(api.hub_candidates(q.get("limit", ""), q.get("min_score", "")))
            return
        if path == "/api/hub-profile":
            self._send_json(api.hub_profile(q.get("iata", "")))
            return
        if path == "/api/fleet/disposal-quote":
            self._send_json(api.fleet_disposal_quote(q.get("tail", "")))
            return
        if path == "/api/fleet":
            self._send_json(api.list_fleet())
            return
        if path == "/api/routes":
            self._send_json(api.list_player_routes())
            return
        if path == "/api/routes/overview":
            self._send_json(api.player_routes_overview())
            return
        if path == "/api/routes/preview":
            self._send_json(api.preview_route(q.get("origin", ""), q.get("dest", "")))
            return
        if path == "/api/routes/suggestions":
            lim = q.get("limit", "25")
            try:
                lim_i = int(lim)
            except (TypeError, ValueError):
                lim_i = 25
            self._send_json(api.route_suggestions(q.get("origin", ""), limit=lim_i))
            return
        if path == "/api/routes/detail":
            self._send_json(api.route_detail(q.get("route_id", "")))
            return
        if path == "/api/gates/auctions":
            self._send_json(api.gate_auctions())
            return
        if path == "/api/gates/bids":
            self._send_json(api.player_gate_bids())
            return
        if path == "/api/gates":
            self._send_json(api.player_gates())
            return
        if path == "/api/slots":
            self._send_json(api.slot_status())
            return
        if path == "/api/slots/bids":
            self._send_json(api.player_slot_bids())
            return
        if path == "/api/notifications":
            self._send_json(api.player_notifications())
            return
        if path == "/api/board":
            self._send_json(api.flight_board())
            return
        if path == "/api/board/airport":
            self._send_json(api.airport_flight_board(q.get("iata", "")))
            return
        if path == "/api/competitors":
            self._send_json(api.competitors_overview())
            return
        if path == "/api/competitors/routes":
            self._send_json(api.competitor_routes(q.get("id", "")))
            return
        if path == "/api/competitors/contested":
            self._send_json(api.contested_markets())
            return
        if path == "/api/schedule":
            self._send_json(api.tail_schedule(q.get("tail", "")))
            return
        if path == "/api/bank":
            self._send_json(api.bank_status(q.get("amount", "")))
            return
        if path == "/api/books":
            self._send_json(api.books_status())
            return
        if path == "/api/hubs":
            self._send_json(api.hubs_status())
            return
        if path == "/api/hubs/preview":
            self._send_json(api.hub_open_preview(q.get("iata", "")))
            return
        if path == "/api/gates/sale-quote":
            self._send_json(api.gate_sale_quote_api(q.get("iata", ""), q.get("units", "1")))
            return
        if path == "/api/gates/shortfalls":
            self._send_json(api.gate_shortfalls())
            return
        if path == "/api/books/pending":
            self._send_json(api.pop_week_summaries())
            return
        if path.startswith("/api/"):
            self._send_json({"ok": False, "error": "Not found"}, 404)
            return
        self._serve_static(path)

    def _handle_post(self) -> None:
        parsed = urlparse(self.path or "/")
        path = parsed.path.rstrip("/") or "/"
        body = self._read_json()
        from server import game_api as api

        if path == "/api/airline":
            out = api.create_airline(body)
            if out.get("ok"):
                ensure_runtime_clock()
            self._send_json(out)
            return
        if path == "/api/airline/reset":
            out = api.reset_airline()
            if out.get("ok"):
                ensure_runtime_clock()
            self._send_json(out)
            return
        if path == "/api/airline/delete":
            self._send_json(api.delete_airline())
            return
        if path == "/api/clock":
            ensure_runtime_clock()
            self._send_json(api.set_clock(body))
            return
        if path == "/api/hubs/open":
            self._send_json(api.open_new_hub(body))
            return
        if path == "/api/gates/sell":
            self._send_json(api.sell_gate(body))
            return
        if path == "/api/gates/sell/cancel":
            self._send_json(api.cancel_gate_sale_api(body))
            return
        if path == "/api/gates/shortfalls/resolve":
            self._send_json(api.resolve_gate_shortfall_request(body))
            return
        if path == "/api/fleet":
            self._send_json(api.acquire_aircraft(body))
            return
        if path == "/api/fleet/reposition":
            self._send_json(api.reposition_aircraft(body))
            return
        if path == "/api/fleet/dispose":
            self._send_json(api.dispose_aircraft(body))
            return
        if path == "/api/fleet/dispose/cancel":
            self._send_json(api.cancel_aircraft_disposal(body))
            return
        if path == "/api/routes":
            self._send_json(api.open_player_route(body))
            return
        if path == "/api/routes/prices":
            self._send_json(api.update_route_prices(body))
            return
        if path == "/api/schedule/preview":
            self._send_json(api.preview_schedule(body))
            return
        if path == "/api/schedule/suggest":
            self._send_json(api.suggest_schedule_times(body))
            return
        if path == "/api/schedule":
            self._send_json(api.assign_schedule(body))
            return
        if path == "/api/gates/bid":
            self._send_json(api.place_gate_bid(body))
            return
        if path == "/api/slots/bid":
            self._send_json(api.place_slot_bid(body))
            return
        if path == "/api/notifications/ack":
            self._send_json(api.ack_notifications(body))
            return
        if path == "/api/bank/originate":
            self._send_json(api.bank_originate(body))
            return
        if path == "/api/bank/payoff":
            self._send_json(api.bank_payoff(body))
            return
        if path == "/api/fuel":
            self._send_json(api.fuel_action(body))
            return
        self._send_json({"ok": False, "error": "Not found"}, 404)

    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path == "/" else path.lstrip("/")
        if ".." in rel:
            self.send_error(400, "Bad path")
            return
        fp = (WEB_DIR / rel).resolve()
        if not str(fp).startswith(str(WEB_DIR.resolve())):
            self.send_error(400, "Bad path")
            return
        if not fp.is_file():
            self.send_error(404, "Not found")
            return
        ctype = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
        if fp.suffix == ".js":
            ctype = "application/javascript; charset=utf-8"
        elif fp.suffix == ".css":
            ctype = "text/css; charset=utf-8"
        elif fp.suffix == ".html":
            ctype = "text/html; charset=utf-8"
        self._send(fp.read_bytes(), ctype)


def start_game_ui_server(port: int = 8765) -> ThreadingHTTPServer:
    global _server
    with _server_lock:
        if _server is not None:
            return _server
        db.ensure_schema_migrations()
        ensure_runtime_clock()
        server = ThreadingHTTPServer(("127.0.0.1", int(port)), _Handler)
        server.daemon_threads = True
        t = threading.Thread(target=server.serve_forever, name="airline_sim_ui", daemon=True)
        t.start()
        _server = server
        return server
