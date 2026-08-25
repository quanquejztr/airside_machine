import express from "express";
import http from "http";
import { WebSocketServer } from "ws";
import path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const PORT = Number(process.env.MAP_PORT || 3000);
const app = express();
app.use(express.json({ limit: "2mb" }));

const publicDir = path.join(__dirname, "public");
app.use("/", express.static(publicDir, { etag: false, lastModified: false }));

const server = http.createServer(app);
const wss = new WebSocketServer({ server, path: "/ws" });

/** @type {Map<string, any>} */
const flights = new Map(); // id -> { id, from, to, progress, speed, status, route }

function deg2rad(d) {
  return (d * Math.PI) / 180;
}
function rad2deg(r) {
  return (r * 180) / Math.PI;
}

function greatCirclePoints(from, to, n = 80) {
  const lat1 = deg2rad(from.lat);
  const lon1 = deg2rad(from.lng);
  const lat2 = deg2rad(to.lat);
  const lon2 = deg2rad(to.lng);

  const d =
    2 *
    Math.asin(
      Math.sqrt(
        Math.sin((lat2 - lat1) / 2) ** 2 +
          Math.cos(lat1) *
            Math.cos(lat2) *
            Math.sin((lon2 - lon1) / 2) ** 2
      )
    );
  if (!isFinite(d) || d === 0) {
    return [[from.lng, from.lat], [to.lng, to.lat]];
  }
  const sinD = Math.sin(d);
  const out = [];
  for (let i = 0; i <= n; i++) {
    const f = i / n;
    const A = Math.sin((1 - f) * d) / sinD;
    const B = Math.sin(f * d) / sinD;
    const x =
      A * Math.cos(lat1) * Math.cos(lon1) + B * Math.cos(lat2) * Math.cos(lon2);
    const y =
      A * Math.cos(lat1) * Math.sin(lon1) + B * Math.cos(lat2) * Math.sin(lon2);
    const z = A * Math.sin(lat1) + B * Math.sin(lat2);
    const lat = Math.atan2(z, Math.sqrt(x * x + y * y));
    const lon = Math.atan2(y, x);
    out.push([rad2deg(lon), rad2deg(lat)]); // [lng, lat]
  }
  return out;
}

function snapshotPayload() {
  return {
    type: "flights",
    ts: Date.now(),
    flights: Array.from(flights.values()),
  };
}

function broadcast(obj) {
  const raw = JSON.stringify(obj);
  for (const client of wss.clients) {
    if (client.readyState === 1) client.send(raw);
  }
}

wss.on("connection", (ws) => {
  ws.send(JSON.stringify(snapshotPayload()));
});

app.get("/api/snapshot", (_req, res) => {
  res.json(snapshotPayload());
});

// Game -> server ingest
app.post("/api/update", (req, res) => {
  const body = req.body || {};
  const list = Array.isArray(body.flights) ? body.flights : [];

  const seen = new Set();
  for (const f of list) {
    if (!f || typeof f.id !== "string") continue;
    if (!f.from || !f.to) continue;
    if (
      typeof f.from.lat !== "number" ||
      typeof f.from.lng !== "number" ||
      typeof f.to.lat !== "number" ||
      typeof f.to.lng !== "number"
    )
      continue;
    const id = f.id;
    seen.add(id);

    const prev = flights.get(id);
    let route = prev?.route;
    if (!route) {
      route = greatCirclePoints(f.from, f.to, 100);
    }
    flights.set(id, {
      id,
      from: f.from,
      to: f.to,
      progress: Math.max(0, Math.min(1, Number(f.progress || 0))),
      speed: Number(f.speed || 0),
      status: String(f.status || ""),
      label: String(f.label || id),
      route,
    });
  }

  // prune
  for (const id of flights.keys()) {
    if (!seen.has(id)) flights.delete(id);
  }

  broadcast(snapshotPayload());
  res.json({ ok: true, count: flights.size });
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`map server: http://127.0.0.1:${PORT}/ (ws /ws)`);
});

