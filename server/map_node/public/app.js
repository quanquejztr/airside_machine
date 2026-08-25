import {
  makeFlightLayers,
  updateFlights,
  isFlightMapReady,
} from "./flights.js";

const meta = document.getElementById("meta");

const map = new maplibregl.Map({
  container: "map",
  style: {
    version: 8,
    sources: {
      osm: {
        type: "raster",
        tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
        tileSize: 256,
        attribution: "© OpenStreetMap contributors",
      },
    },
    layers: [{ id: "osm", type: "raster", source: "osm" }],
  },
  center: [-98.5, 39.8],
  zoom: 3.6,
});

map.addControl(new maplibregl.NavigationControl(), "top-right");

let state = { flights: [] };
let lastMsgAt = 0;

/** Server target progress per flight id (0–1). */
const targetProgress = {};
/** Visually smoothed progress toward target (interactive motion). */
const smoothProgress = {};

function clamp01(x) {
  return Math.max(0, Math.min(1, x));
}

let lastFrameTime = performance.now();

map.on("load", () => {
  makeFlightLayers(map);
  connect();
  lastFrameTime = performance.now();
  requestAnimationFrame(animate);
});

function connect() {
  const wsUrl = `ws://${location.host}/ws`;
  const ws = new WebSocket(wsUrl);

  ws.addEventListener("open", () => {
    meta.textContent = `connected`;
  });
  ws.addEventListener("close", () => {
    meta.textContent = `disconnected (retrying…)`;
    setTimeout(connect, 800);
  });
  ws.addEventListener("message", (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg && msg.type === "flights" && Array.isArray(msg.flights)) {
        lastMsgAt = Date.now();
        state = msg;

        const alive = new Set();
        for (const f of msg.flights) {
          if (!f || typeof f.id !== "string") continue;
          alive.add(f.id);
          const tgt = clamp01(Number(f.progress || 0));
          targetProgress[f.id] = tgt;
          if (smoothProgress[f.id] === undefined) {
            smoothProgress[f.id] = tgt;
          }
        }
        for (const id of Object.keys(smoothProgress)) {
          if (!alive.has(id)) {
            delete smoothProgress[id];
            delete targetProgress[id];
          }
        }
      }
    } catch {
      // ignore
    }
  });
}

function animate(now) {
  const t = now ?? performance.now();
  const dt = Math.min(0.1, (t - lastFrameTime) / 1000);
  lastFrameTime = t;

  // Exponential smoothing: frame-rate independent.
  const k = 1 - Math.exp(-dt * 14);

  const flights = state.flights || [];
  for (const f of flights) {
    if (!f || typeof f.id !== "string") continue;
    const id = f.id;
    const tgt = targetProgress[id] ?? clamp01(Number(f.progress || 0));
    let cur = smoothProgress[id];
    if (cur === undefined) cur = tgt;
    // Large jumps (new leg / reconnect): snap instead of flying across the map.
    if (Math.abs(tgt - cur) > 0.45) {
      smoothProgress[id] = tgt;
    } else {
      smoothProgress[id] = cur + (tgt - cur) * k;
    }
  }

  const age = Date.now() - lastMsgAt;
  if (age < 3000) meta.textContent = `live`;
  else meta.textContent = age < 15000 ? `stale` : `disconnected?`;

  if (isFlightMapReady(map)) {
    updateFlights(map, flights, smoothProgress);
  }

  requestAnimationFrame(animate);
}
