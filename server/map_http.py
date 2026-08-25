"""
Minimal localhost-only HTTP server for the flight map (Leaflet in the browser).
Does not run game logic; reads the same SQLite DB as the CLI.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

_server: Optional[ThreadingHTTPServer] = None
_server_lock = threading.Lock()


def _html_page() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Airside Machine — Flight map</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
    crossorigin="" />
  <style>
    * { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; font-family: system-ui, sans-serif; }
    #bar {
      padding: 8px 12px; background: #1a1a2e; color: #eee; font-size: 13px;
      display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
    }
    #bar strong { color: #7ec8e3; }
    #map { height: calc(100% - 44px); width: 100%; }
    a { color: #7ec8e3; }
  </style>
</head>
<body>
  <div id="bar">
    <strong>Flight map</strong>
    <span id="meta">Loading…</span>
    <span style="opacity:.75">·</span>
    <a href="/api/flight-map.json" target="_blank">JSON</a>
    <span style="opacity:.75;font-size:11px;margin-left:auto">Read-only · same DB as python main.py</span>
  </div>
  <div id="map"></div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
    crossorigin=""></script>
  <script>
  <script>
    var mapInst = null;
    var lineLayer = null;
    var acLayer = null;

    var flights = {}; // keyed by flight id

    function tailColor(t) {
      let h = 0;
      for (let i = 0; i < t.length; i++) h = ((h << 5) - h) + t.charCodeAt(i) | 0;
      h = Math.abs(h) % 300;
      return "hsl(" + h + " 65% 42%)";
    }

    // 🌍 Great-circle interpolation
    function interpolateGreatCircle(lat1, lon1, lat2, lon2, numPoints = 80) {
      const toRad = d => d * Math.PI / 180;
      const toDeg = r => r * 180 / Math.PI;

      lat1 = toRad(lat1); lon1 = toRad(lon1);
      lat2 = toRad(lat2); lon2 = toRad(lon2);

      const d = 2 * Math.asin(Math.sqrt(
        Math.sin((lat2 - lat1)/2)**2 +
        Math.cos(lat1)*Math.cos(lat2)*Math.sin((lon2 - lon1)/2)**2
      ));

      const points = [];

      for (let i = 0; i <= numPoints; i++) {
        const f = i / numPoints;

        const A = Math.sin((1 - f) * d) / Math.sin(d);
        const B = Math.sin(f * d) / Math.sin(d);

        const x = A * Math.cos(lat1) * Math.cos(lon1) + B * Math.cos(lat2) * Math.cos(lon2);
        const y = A * Math.cos(lat1) * Math.sin(lon1) + B * Math.cos(lat2) * Math.sin(lon2);
        const z = A * Math.sin(lat1) + B * Math.sin(lat2);

        const lat = Math.atan2(z, Math.sqrt(x*x + y*y));
        const lon = Math.atan2(y, x);

        points.push([toDeg(lat), toDeg(lon)]);
      }

      return points;
    }

    // ✈️ Update or create flights
    function updateFlights(data) {
      const seen = {};

      (data.segments || []).forEach(seg => {
        if (seg.origin_lat == null || seg.dest_lat == null) return;

        const id = seg.tail_number + "_" + (seg.route_id || "");
        seen[id] = true;

        if (!flights[id]) {
          const route = interpolateGreatCircle(
            seg.origin_lat, seg.origin_lon,
            seg.dest_lat, seg.dest_lon
          );

          const color = tailColor(seg.tail_number || "");

          const line = L.polyline(route, {
            color: color,
            weight: 2,
            opacity: 0.6
          }).addTo(lineLayer);

          const marker = L.circleMarker(route[0], {
            radius: 6,
            color: "#111",
            weight: 2,
            fillColor: color,
            fillOpacity: 1
          }).addTo(acLayer);

          flights[id] = {
            route,
            marker,
            line,
            progress: seg.progress || 0,
            targetProgress: seg.progress || 0,
            color
          };
        } else {
          flights[id].targetProgress = seg.progress || 0;
        }
      });

      // remove old flights
      Object.keys(flights).forEach(id => {
        if (!seen[id]) {
          mapInst.removeLayer(flights[id].marker);
          mapInst.removeLayer(flights[id].line);
          delete flights[id];
        }
      });
    }

    // 🎬 Smooth animation loop
    function animate() {
      Object.values(flights).forEach(f => {
        // smooth interpolation
        f.progress += (f.targetProgress - f.progress) * 0.05;

        const idx = Math.floor(f.progress * (f.route.length - 1));
        const pos = f.route[idx];

        if (pos) {
          f.marker.setLatLng(pos);
        }
      });

      requestAnimationFrame(animate);
    }

    // 🔄 Load data
    async function load(first) {
      const r = await fetch("/api/flight-map.json?t=" + Date.now(), { cache: "no-store" });
      const data = await r.json();

      const meta = document.getElementById("meta");
      if (!data.ok) {
        meta.textContent = "Error loading data";
        return;
      }

      meta.textContent = "Week " + data.game_week + " · " + (data.airline?.name || "");

      if (first) {
        mapInst = L.map("map", { worldCopyJump: true });

        L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
          maxZoom: 18,
          attribution: "&copy; OpenStreetMap"
        }).addTo(mapInst);

        lineLayer = L.layerGroup().addTo(mapInst);
        acLayer = L.layerGroup().addTo(mapInst);

        mapInst.setView([39.8, -98.5], 4);

        animate(); // start animation loop
      }

      updateFlights(data);
    }

    // 🚀 init
    load(true);
    setInterval(() => load(false), 5000);
    </script>
</body>
</html>"""


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

    def do_GET(self) -> None:
        path = (self.path or "").split("?", 1)[0].rstrip("/") or "/"
        if path == "/api/flight-map.json":
            from engine.flight_map_data import get_flight_map_payload

            try:
                payload = get_flight_map_payload()
            except Exception as e:
                payload = {"ok": False, "error": str(e)}
            raw = json.dumps(payload, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/":
            body = _html_page().encode("utf-8")
            self._send(body, "text/html; charset=utf-8")
            return
        self.send_error(404, "Not found")


def start_flight_map_server(port: int = 8775) -> ThreadingHTTPServer:
    """Bind 127.0.0.1 only; daemon thread; safe to call once."""
    global _server
    with _server_lock:
        if _server is not None:
            return _server
        server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        server.daemon_threads = True
        t = threading.Thread(target=server.serve_forever, name="airline_sim_map", daemon=True)
        t.start()
        _server = server
        return server
