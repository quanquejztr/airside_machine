function clamp01(x) {
  return Math.max(0, Math.min(1, x));
}

function bearingDeg(a, b) {
  const toRad = (d) => (d * Math.PI) / 180;
  const toDeg = (r) => (r * 180) / Math.PI;
  const lat1 = toRad(a[1]);
  const lon1 = toRad(a[0]);
  const lat2 = toRad(b[1]);
  const lon2 = toRad(b[0]);
  const y = Math.sin(lon2 - lon1) * Math.cos(lat2);
  const x =
    Math.cos(lat1) * Math.sin(lat2) -
    Math.sin(lat1) * Math.cos(lat2) * Math.cos(lon2 - lon1);
  let brng = toDeg(Math.atan2(y, x));
  brng = (brng + 360) % 360;
  return brng;
}

/**
 * Position along a great-circle polyline [lng, lat][] at fraction t in [0, 1].
 * Linear interpolation per segment (matches sampling along the arc).
 */
export function pointAlongRoute(route, t) {
  const n = route.length;
  if (n === 0) return { coord: [0, 0], bearing: 0 };
  if (n === 1) return { coord: route[0], bearing: 0 };
  const tt = clamp01(t);
  const max = n - 1;
  const x = tt * max;
  const i = Math.min(Math.floor(x), max - 1);
  const frac = x - i;
  const a = route[i];
  const b = route[i + 1];
  const lng = a[0] + (b[0] - a[0]) * frac;
  const lat = a[1] + (b[1] - a[1]) * frac;
  const bearing = bearingDeg(a, b);
  return { coord: [lng, lat], bearing };
}

export const SRC_ROUTES = "flight_routes";
export const SRC_PLANES = "flight_planes";

export function isFlightMapReady(map) {
  try {
    return !!(map && map.style && map.getSource(SRC_PLANES));
  } catch {
    return false;
  }
}

export function makeFlightLayers(map) {
  // Text-only symbols need a placeholder icon on some MapLibre builds.
  if (!map.hasImage("empty-pixel")) {
    map.addImage(
      "empty-pixel",
      { width: 1, height: 1, data: new Uint8ClampedArray(4) },
      { pixelRatio: 1 }
    );
  }

  map.addSource(SRC_ROUTES, {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });
  map.addLayer({
    id: "routes",
    type: "line",
    source: SRC_ROUTES,
    paint: { "line-color": ["get", "color"], "line-width": 2, "line-opacity": 0.6 },
  });

  map.addSource(SRC_PLANES, {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });

  // Moving aircraft: filled dot + stroke (no sprite required).
  map.addLayer({
    id: "plane_dots",
    type: "circle",
    source: SRC_PLANES,
    paint: {
      "circle-radius": 6,
      "circle-color": ["get", "color"],
      "circle-stroke-width": 2,
      "circle-stroke-color": "#ffffff",
      "circle-opacity": 0.95,
    },
  });

  // Labels above the dot.
  map.addLayer({
    id: "plane_labels",
    type: "symbol",
    source: SRC_PLANES,
    layout: {
      "icon-image": "empty-pixel",
      "icon-size": 1,
      "icon-allow-overlap": true,
      "text-field": ["get", "label"],
      "text-size": 11,
      "text-offset": [0, 1.35],
      "text-anchor": "top",
      "text-allow-overlap": true,
    },
    paint: {
      "icon-opacity": 0,
      "text-color": "#e6eeff",
      "text-halo-color": "#0e1220",
      "text-halo-width": 1.2,
    },
  });
}

/**
 * @param {import('maplibre-gl').Map} map
 * @param {any[]} flights
 * @param {Record<string, number>} [progressById] smoothed 0–1 progress per flight id (falls back to f.progress)
 */
export function updateFlights(map, flights, progressById = {}) {
  const routeFeatures = [];
  const planeFeatures = [];

  for (const f of flights) {
    if (!f || !Array.isArray(f.route) || f.route.length < 2) continue;
    const raw = clamp01(Number(f.progress || 0));
    const p =
      progressById[f.id] !== undefined && progressById[f.id] !== null
        ? clamp01(Number(progressById[f.id]))
        : raw;
    const { coord, bearing } = pointAlongRoute(f.route, p);
    const color = f.status === "DIVERTED" ? "#d56cff" : "#4aa3ff";

    routeFeatures.push({
      type: "Feature",
      geometry: { type: "LineString", coordinates: f.route },
      properties: { id: f.id, color },
    });

    planeFeatures.push({
      type: "Feature",
      geometry: { type: "Point", coordinates: coord },
      properties: {
        id: f.id,
        label: f.label || f.id,
        bearing,
        color,
      },
    });
  }

  map.getSource(SRC_ROUTES).setData({
    type: "FeatureCollection",
    features: routeFeatures,
  });
  map.getSource(SRC_PLANES).setData({
    type: "FeatureCollection",
    features: planeFeatures,
  });
}
