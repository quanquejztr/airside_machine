const $ = (sel, root = document) => root.querySelector(sel);

let mapInst = null;
let lineLayer = null;
let acLayer = null;
let airportLayer = null;
let airportRenderer = null;
let airportRouteLayer = null;
let airportDraftLayer = null;
const flights = {};
const airportPins = {};
let airportCatalog = null;
let airportCatalogPromise = null;
let airportNetwork = new Set();
let airportHub = "";
let airportPinRefreshTimer = null;
let selectedAirportIata = "";
let selectedAirportRoutesSeq = 0;
/** @type {null | { origin: string, dest: string | null, phase: 'pick_dest' | 'confirm' }} */
let routeDraft = null;
let routeDraftPreviewSeq = 0;
let zTop = 40;
let winOffset = 0;
let lastState = null;
let hudSeq = 0;
let didFitFlights = false;
const mapClock = {
  hour: 0,
  speed: 0,
  realSecPerHour: 30,
  at: 0,
  alive: true,
  serverHour: 0,
  serverAt: 0,
};
const seenNoticeIds = new Set();
let mapLoadSeq = 0;
let mapPollTimer = null;

function syncMapClock(src) {
  if (!src) return;
  const incoming = src.current_game_hour != null ? Number(src.current_game_hour)
    : src.game_hours_elapsed != null ? Number(src.game_hours_elapsed)
    : null;
  // Always trust the server stamp — never keep a raced-ahead extrapolated hour.
  if (incoming != null && Number.isFinite(incoming)) {
    mapClock.hour = incoming;
    mapClock.serverHour = incoming;
    mapClock.serverAt = performance.now();
  }
  if (src.speed_multiplier != null) mapClock.speed = Number(src.speed_multiplier);
  if (src.real_seconds_per_game_hour) mapClock.realSecPerHour = Number(src.real_seconds_per_game_hour);
  if (src.clock_alive != null) mapClock.alive = Boolean(src.clock_alive);
  mapClock.at = performance.now();
  scheduleMapPoll();
}

function liveGameHour() {
  if (mapClock.alive === false) return mapClock.hour;
  const now = performance.now();
  const elapsed = (now - (mapClock.at || now)) / 1000;
  const raw = mapClock.hour + (elapsed / (mapClock.realSecPerHour || 30)) * (mapClock.speed || 0);
  // Cap runaway extrapolation while a poll is hung (common at 60× + DB lock).
  // Allow at most ~2 real seconds of predicted advance beyond the last server stamp.
  if (mapClock.serverAt) {
    const sinceServer = (now - mapClock.serverAt) / 1000;
    const maxAhead = ((Math.min(sinceServer, 2.0) / (mapClock.realSecPerHour || 30))
      * (mapClock.speed || 0));
    const ceiling = mapClock.serverHour + maxAhead;
    if (Number.isFinite(ceiling)) return Math.min(raw, ceiling);
  }
  return raw;
}

function freezeMapClock() {
  mapClock.hour = liveGameHour();
  mapClock.speed = 0;
  mapClock.alive = false;
  mapClock.at = performance.now();
}

function applyClockSpeed(speed) {
  const hour = mapClock.at ? liveGameHour() : mapClock.hour;
  mapClock.hour = hour;
  mapClock.speed = Number(speed) || 0;
  mapClock.at = performance.now();
}

function formatHudTime(ghe, speed) {
  const hour = Number(ghe) || 0;
  const week = Math.floor(hour / 168) + 1;
  const hiw = ((hour % 168) + 168) % 168;
  const day = Math.floor(hiw / 24) + 1;
  const hod = hiw % 24;
  const hh = Math.floor(hod);
  const mm = Math.floor((hod - hh) * 60) % 60;
  const sp = Number(speed) || 0;
  const spd = sp === 0 ? "paused" : `${sp}×`;
  const pad = (n) => String(n).padStart(2, "0");
  return `Week ${week} · Day ${day} · ${pad(hh)}:${pad(mm)} · ${spd}`;
}

function paintHudTime() {
  const el = $("#hud-time");
  if (!el) return;
  if (mapClock.at) el.textContent = formatHudTime(liveGameHour(), mapClock.speed);
  else if (lastState && lastState.clock && lastState.clock.time_display) el.textContent = lastState.clock.time_display;
}

function toast(msg) {
  const el = $("#toast");
  el.hidden = false;
  const inner = el.querySelector(".toast-inner") || el;
  inner.textContent = msg;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 3500);
}

async function api(path, opts) {
  const r = await fetch(path, {
    cache: "no-store",
    headers: opts && opts.body ? { "Content-Type": "application/json" } : undefined,
    ...opts,
  });
  let data;
  try { data = await r.json(); } catch { data = { ok: false, error: "Bad response" }; }
  if (!data.ok) throw new Error(data.error || "Request failed");
  return data;
}

function money(n) {
  return Number(n || 0).toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });
}

function signedMoney(n) {
  const v = Number(n || 0);
  const body = money(Math.abs(v));
  if (v > 0) return "+" + body;
  if (v < 0) return "−" + body;
  return body;
}

function openWindow(id, title, html, { width } = {}) {
  let el = document.getElementById("win-" + id);
  if (el) {
    el.style.zIndex = String(++zTop);
    return el;
  }
  el = document.createElement("div");
  el.className = "win glass-panel";
  el.id = "win-" + id;
  winOffset = (winOffset + 28) % 180;
  el.style.left = 70 + winOffset + "px";
  el.style.top = 70 + winOffset + "px";
  el.style.width = (width || 460) + "px";
  el.style.zIndex = String(++zTop);
  el.innerHTML = `
    <div class="glass-filter"></div>
    <div class="glass-overlay"></div>
    <div class="glass-specular"></div>
    <div class="glass-content">
      <div class="win-title">
        <span>${title}</span>
        <button type="button" aria-label="Close">×</button>
      </div>
      <div class="win-body">${html}</div>
    </div>`;
  $("#windows").appendChild(el);
  el.querySelector(".win-title button").onclick = () => el.remove();
  makeDraggable(el);
  el.addEventListener("mousedown", () => { el.style.zIndex = String(++zTop); });
  return el;
}

function makeDraggable(win) {
  const bar = win.querySelector(".win-title");
  let dragging = false, sx = 0, sy = 0, ox = 0, oy = 0;
  bar.addEventListener("mousedown", (e) => {
    if (e.target.tagName === "BUTTON") return;
    dragging = true;
    sx = e.clientX; sy = e.clientY;
    ox = win.offsetLeft; oy = win.offsetTop;
  });
  window.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    win.style.left = Math.max(8, ox + e.clientX - sx) + "px";
    win.style.top = Math.max(8, oy + e.clientY - sy) + "px";
  });
  window.addEventListener("mouseup", () => { dragging = false; });
}

function setMsg(root, text, ok) {
  let n = root.querySelector(".msg");
  if (!n) {
    n = document.createElement("div");
    n.className = "msg";
    root.appendChild(n);
  }
  n.className = "msg " + (ok ? "ok" : "err");
  n.textContent = text || "";
}

function closeWindow(id) {
  const el = document.getElementById("win-" + id);
  if (el) el.remove();
}

async function refreshHud() {
  const seq = ++hudSeq;
  let s;
  try {
    s = await api("/api/state");
  } catch (err) {
    freezeMapClock();
    throw err;
  }
  if (seq !== hudSeq) return;
  lastState = s;
  const a = s.airline;
  $("#hud-airline").textContent = a
    ? `${a.name} (${a.callsign}) · ${a.home_hub_iata}`
    : "No airline — open Airline to found one";
  if (s.clock) syncMapClock(s.clock);
  paintHudTime();
  const cashEl = $("#hud-cash");
  const cashAmt = $("#hud-cash-amt");
  if (cashAmt) cashAmt.textContent = a ? money(a.cash) : "";
  const f = s.finance;
  if (cashEl) cashEl.classList.toggle("is-empty", !(a && f));
  if (a && f) {
    $("#hud-rev").textContent = money(f.revenue);
    const cf = $("#hud-cf");
    cf.textContent = signedMoney(f.cash_flow);
    cf.classList.toggle("neg", Number(f.cash_flow) < 0);
    $("#hud-today").textContent = money(f.today_revenue);
    const todayLabel = $("#hud-today-lbl");
    if (todayLabel) todayLabel.textContent = `Today revenue (${f.day || ""})`;
    const debtEl = $("#hud-debt");
    if (debtEl) debtEl.textContent = money(f.debt);
    const loanEl = $("#hud-loan");
    if (loanEl) loanEl.textContent = money(f.weekly_loan);
    const fuelEl = $("#hud-fuel");
    if (fuelEl) {
      fuelEl.textContent = (f.fuel_spot_bbl != null && Number.isFinite(Number(f.fuel_spot_bbl)))
        ? `$${Number(f.fuel_spot_bbl).toFixed(2)}/bbl`
        : "—";
    }
  }
  markHudSpeed((s.clock && s.clock.speed_multiplier) || 0);
  $("#ticker-text").textContent = (s.news && s.news.length) ? s.news.slice().reverse()[0] : "News: —";
  const notes = s.notifications || [];
  if (notes.length) {
    const newest = notes[0];
    if (newest && newest.body) {
      $("#ticker-text").textContent = `Notice: ${newest.body}`;
    }
    const unseen = notes.filter((n) => n && n.notification_id && !seenNoticeIds.has(n.notification_id));
    unseen.forEach((n) => seenNoticeIds.add(n.notification_id));
    if (unseen[0] && unseen[0].body) toast(unseen[0].body);
  }
  drainWeekSummaries().catch(() => {});
  if (!a && !document.getElementById("win-airline")) openAirline();
}

function closeAllWindows() {
  const box = $("#windows");
  if (box) box.innerHTML = "";
}

function resetClientWorld() {
  Object.keys(flights).forEach((id) => {
    try {
      if (mapInst) {
        mapInst.removeLayer(flights[id].marker);
        mapInst.removeLayer(flights[id].line);
      }
    } catch (_) {}
    delete flights[id];
  });
  didFitFlights = false;
}

function openAirline() {
  const existing = lastState && lastState.airline;
  const el = openWindow("airline", "Airline", existing
    ? `<p><b>${escapeHtml(existing.name)}</b> (${escapeHtml(existing.callsign)})</p>
       <p>Hub ${escapeHtml(existing.home_hub_iata)}</p>
       <p>Cash ${money(existing.cash)}</p>
       <p>Reputation <b>${Number(existing.reputation_score).toFixed(0)}</b> / 100
         · brand <b>${Number(existing.brand_power || 1).toFixed(2)}×</b></p>
       <p class="muted">Debt ${money(existing.total_debt)} · credit ${existing.credit_score}</p>
       <p class="muted">Reset keeps this name, callsign, and hub. Week, cash, fleet, and routes go back to a new-game start. Delete removes the airline so you can found another.</p>
       <button type="button" id="al-reset">Reset airline</button>
       <button type="button" id="al-delete" class="danger">Delete airline</button>`
    : `<form>
        <label>Airline name</label><input name="name" required placeholder="Smoke Air" />
        <div class="row2">
          <div><label>Callsign (3 letters)</label><input name="callsign" maxlength="3" required placeholder="SMK" /></div>
          <div><label>Home hub (code, city or name)</label><input name="home_hub_iata" required placeholder="TPA" /></div>
        </div>
        <p class="muted">Big hubs (SFO, ORD, JFK) need gate auctions before you can fly from them. TPA does not.</p>
        <button type="submit">Create airline</button>
      </form>`);
  const resetBtn = $("#al-reset", el);
  if (resetBtn) {
    resetBtn.onclick = async () => {
      const hub = existing.home_hub_iata;
      if (!window.confirm(`Reset ${existing.name} to a new-game start at ${hub}? Fleet, routes, cash, and the clock go back to week 1. Name, callsign, and hub stay.`)) return;
      try {
        await api("/api/airline/reset", { method: "POST", body: JSON.stringify({}) });
        toast("Airline reset to start");
        closeAllWindows();
        resetClientWorld();
        await refreshHud();
        loadMap().catch(() => {});
      } catch (err) { setMsg(el.querySelector(".win-body"), err.message, false); }
    };
  }
  const deleteBtn = $("#al-delete", el);
  if (deleteBtn) {
    deleteBtn.onclick = async () => {
      if (!window.confirm(`Delete ${existing.name}? You will need to found a new airline.`)) return;
      try {
        await api("/api/airline/delete", { method: "POST", body: JSON.stringify({}) });
        toast("Airline deleted");
        lastState = { airline: null };
        closeAllWindows();
        resetClientWorld();
        await refreshHud();
        loadMap().catch(() => {});
      } catch (err) { setMsg(el.querySelector(".win-body"), err.message, false); }
    };
  }
  const form = el.querySelector("form");
  if (!form) return;
  form.onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    try {
      const created = await api("/api/airline", {
        method: "POST",
        body: JSON.stringify({
          name: fd.get("name"),
          callsign: fd.get("callsign"),
          home_hub_iata: await resolveAirportCode(fd.get("home_hub_iata")),
        }),
      });
      hudSeq += 1;
      lastState = { ...(lastState || {}), airline: created.airline || {
        name: fd.get("name"),
        callsign: String(fd.get("callsign") || "").toUpperCase(),
        home_hub_iata: await resolveAirportCode(fd.get("home_hub_iata")),
      } };
      const hubIata = String(
        (lastState.airline && lastState.airline.home_hub_iata) || ""
      ).toUpperCase();
      toast("Airline created");
      closeWindow("airline");
      resetClientWorld();
      // Skip the first world-wide flight fit so the hub zoom isn't overridden.
      didFitFlights = true;
      await refreshHud();
      await loadMap();
      await focusMapOnHub(hubIata);
    } catch (err) { setMsg(form, err.message, false); }
  };
}

function openCatalog() {
  const el = openWindow("catalog", "Buy / lease aircraft", `
    <div class="row2">
      <div><label>Category</label>
        <select id="cat">
          <option value="NARROW">Narrowbody</option>
          <option value="WIDE">Widebody</option>
          <option value="REGIONAL_JET">Regional jet</option>
          <option value="TURBOPROP">Turboprop</option>
        </select>
      </div>
      <div><label>Lease weeks</label><input id="lease-weeks" type="number" value="52" min="1" max="520" step="1" /></div>
    </div>
    <div id="cat-list" class="muted">Loading…</div>`);
  const load = async () => {
    const box = $("#cat-list", el);
    try {
      const data = await api("/api/catalog?category=" + encodeURIComponent($("#cat", el).value));
      box.innerHTML = `<table class="grid"><thead><tr><th>Type</th><th>Range</th><th>Buy</th><th></th></tr></thead><tbody>${
        (data.aircraft || []).map((a) => `<tr>
          <td>${a.type_id}<div class="muted">${a.display_name}</div></td>
          <td>${Number(a.range_nm).toLocaleString()} nm</td>
          <td>${money(a.purchase_price)}</td>
          <td>
            <button data-buy="${a.type_id}">Buy</button>
            <button data-lease="${a.type_id}">Lease</button>
          </td>
        </tr>`).join("")
      }</tbody></table>`;
    } catch (err) { box.innerHTML = `<div class="err">${err.message}</div>`; }
  };
  $("#cat", el).onchange = load;
  el.addEventListener("click", (e) => {
    const t = e.target;
    if (!(t instanceof HTMLElement)) return;
    const typeId = t.dataset.buy || t.dataset.lease;
    if (!typeId) return;
    openCabinOrder(typeId, t.dataset.lease ? "lease" : "buy", readLeaseWeeksInput($("#lease-weeks", el), 52));
  });
  load();
}

function cabinEecUsed(y, w, j, f, costs) {
  return Number(y) * Number(costs.economy || 1)
    + Number(w) * Number(costs.premium_economy || 1.5)
    + Number(j) * Number(costs.business || 2)
    + Number(f) * Number(costs.first || 4);
}

function openCabinOrder(typeId, mode, weeks) {
  const existing = document.getElementById("win-cabin");
  if (existing) existing.remove();
  const leaseWeeks = mode === "lease" ? readLeaseWeeksInput(null, weeks) : 0;
  const el = openWindow("cabin", "Cabin layout", `<div class="muted">Loading ${escapeHtml(typeId)}…</div>`);
  api("/api/catalog/cabin?type_id=" + encodeURIComponent(typeId)).then((spec) => {
    const costs = spec.costs || {};
    const action = mode === "lease" ? "Lease" : "Buy";
    const unitPrice = mode === "lease" ? Number(spec.weekly_lease_cost || 0) : Number(spec.purchase_price || 0);
    const priceLine = mode === "lease"
      ? `${money(spec.weekly_lease_cost)} / week`
      : money(spec.purchase_price);
    el.querySelector(".win-body").innerHTML = `
      <p><b>${escapeHtml(spec.type_id)}</b> · ${escapeHtml(spec.display_name || "")}</p>
      <p class="muted">${action} ${priceLine}. Same cabin is applied to every tail in this order.</p>
      <form id="cb-form">
        <label>How many aircraft</label>
        <input name="qty" type="number" min="1" max="50" step="1" value="1" />
        ${mode === "lease"
          ? `<label>Lease term (weeks)</label>
             <input id="cb-lease-weeks" name="lease_weeks" type="number" min="1" max="520" step="1" value="${leaseWeeks}" />
             <p class="muted">First week is charged now; remaining weeks count down at each settlement.</p>`
          : ""}
        <div class="row2">
          <div><label>Economy / Y (${Number(costs.economy || 1)} EEC)</label><input name="y" type="number" min="0" step="1" value="${spec.seats_economy}" /></div>
          <div><label>Premium economy / W (${Number(costs.premium_economy || 1.5)} EEC)</label><input name="w" type="number" min="0" step="1" value="${spec.seats_premium_economy}" /></div>
          <div><label>Business / J (${Number(costs.business || 2)} EEC)</label><input name="j" type="number" min="0" step="1" value="${spec.seats_business}" /></div>
          <div><label>First / F (${Number(costs.first || 4)} EEC)</label><input name="f" type="number" min="0" step="1" value="${spec.seats_first}" /></div>
        </div>
        <p id="cb-eec" class="muted"></p>
        <button type="submit" id="cb-go">${action} with this cabin</button>
        <button type="button" id="cb-cancel">Cancel</button>
      </form>`;
    const form = $("#cb-form", el);
    const status = $("#cb-eec", el);
    const goBtn = $("#cb-go", el);
    const readSeats = () => {
      const fd = new FormData(form);
      return {
        y: Number(fd.get("y") || 0),
        w: Number(fd.get("w") || 0),
        j: Number(fd.get("j") || 0),
        f: Number(fd.get("f") || 0),
      };
    };
    const readQty = () => {
      const n = Math.floor(Number(form.qty && form.qty.value));
      if (!Number.isFinite(n) || n < 1) return 1;
      return Math.min(50, n);
    };
    const readOrderWeeks = () => (mode === "lease"
      ? readLeaseWeeksInput($("#cb-lease-weeks", el), leaseWeeks)
      : 0);
    const refreshEec = () => {
      const s = readSeats();
      const qty = readQty();
      const orderWeeks = readOrderWeeks();
      const used = cabinEecUsed(s.y, s.w, s.j, s.f, costs);
      const cap = Number(spec.eec_limit || 0);
      const over = used - cap;
      if (goBtn) {
        const term = mode === "lease" ? ` · ${orderWeeks} wk` : "";
        goBtn.textContent = qty > 1 ? `${action} ${qty} with this cabin${term}` : `${action} with this cabin${term}`;
      }
      if (s.y + s.w + s.j + s.f <= 0) {
        status.className = "err";
        status.textContent = "Need at least one seat.";
        return false;
      }
      if (over > 0.0001) {
        status.className = "err";
        status.textContent = `EEC ${used.toFixed(1)} / ${cap} — over by ${over.toFixed(1)}. Reduce seats to fit.`;
        return false;
      }
      const pct = cap > 0 ? (used / cap) * 100 : 0;
      const total = unitPrice * qty;
      const totLine = mode === "lease"
        ? ` · ${qty}× ${money(unitPrice)}/wk now · term ${orderWeeks} wk`
        : ` · ${qty}× ${money(unitPrice)} = ${money(total)}`;
      status.className = "ok";
      status.textContent = `EEC ${used.toFixed(1)} / ${cap} · ${pct.toFixed(0)}% used · ${(cap - used).toFixed(1)} remaining${totLine}`;
      return true;
    };
    form.addEventListener("input", refreshEec);
    refreshEec();
    $("#cb-cancel", el).onclick = () => el.remove();
    form.onsubmit = async (e) => {
      e.preventDefault();
      if (!refreshEec()) return;
      const s = readSeats();
      const qty = readQty();
      try {
        const orderWeeks = readOrderWeeks();
        const out = await api("/api/fleet", {
          method: "POST",
          body: JSON.stringify({
            type_id: spec.type_id,
            mode,
            weeks: orderWeeks,
            quantity: qty,
            seats_economy: s.y,
            seats_premium_economy: s.w,
            seats_business: s.j,
            seats_first: s.f,
          }),
        });
        const tails = (out.fleet || []).map((a) => a.tail_number).filter(Boolean);
        if (mode === "lease") {
          const rem = out.aircraft && out.aircraft.lease_weeks_remaining;
          const term = rem != null ? ` · ${rem} wk remaining` : "";
          if (tails.length > 1) toast(`Leased ${tails.length}× ${spec.type_id}: ${tails.join(", ")}${term}`);
          else toast(`Leased ${(out.aircraft && out.aircraft.tail_number) || spec.type_id}${term}`);
        } else if (tails.length > 1) toast(`Added ${tails.length}× ${spec.type_id}: ${tails.join(", ")}`);
        else toast((out.aircraft && out.aircraft.tail_number) || "Aircraft added");
        el.remove();
        await refreshHud();
      } catch (err) { toast(err.message); }
    };
  }).catch((err) => { el.querySelector(".win-body").innerHTML = `<div class="err">${err.message}</div>`; });
}

function openFleet() {
  const el = openWindow("fleet", "Fleet", `<div class="muted">Loading…</div>`);
  api("/api/fleet").then((data) => {
    const rows = data.fleet || [];
    if (!rows.length) {
      el.querySelector(".win-body").innerHTML = `<p class="muted">No aircraft. Open Buy aircraft.</p>`;
      return;
    }
    el.querySelector(".win-body").innerHTML = `<p class="muted">Click a tail to see its weekly schedule.</p>
      <table class="grid"><thead><tr><th>Tail</th><th>Type</th><th>Own</th><th>Lease left</th><th>Status</th><th>At</th></tr></thead><tbody>${
      rows.map((r) => `<tr>
        <td><span class="tail-link" tabindex="0" role="button" data-tail="${escapeHtml(r.tail_number)}">${escapeHtml(r.tail_number)}</span></td>
        <td>${escapeHtml(r.type_id)}</td>
        <td>${escapeHtml(fleetOwnershipLabel(r))}</td>
        <td>${escapeHtml(fleetLeaseWeeksLabel(r))}</td>
        <td>${escapeHtml(r.status)}</td>
        <td>${escapeHtml(r.current_airport_iata || "")}</td>
      </tr>`).join("")
    }</tbody></table>`;
    el.querySelectorAll(".tail-link").forEach((n) => {
      const go = () => openTailGrid(n.dataset.tail);
      n.onclick = go;
      n.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } };
    });
  }).catch((err) => { el.querySelector(".win-body").innerHTML = `<div class="err">${err.message}</div>`; });
}

function routePreviewCard(p) {
  const d = p.demand || {};
  const labels = d.labels || {};
  const ends = p.endpoints || [];
  const legs = (p.opens || []);
  const legLines = legs.length
    ? legs.map((l) => `<div>${escapeHtml(l.origin)}\u2192${escapeHtml(l.dest)} — ${l.charge_acquisition ? "charged" : "free"}</div>`).join("")
    : `<div class="muted">no new legs</div>`;
  const endLines = ends.map((e) => {
    const flags = [
      e.gate_auctioned ? "gate auction" : null,
      e.slot_controlled ? "slot controlled" : null,
    ].filter(Boolean).join(" · ");
    return `<div><b>${escapeHtml(e.iata)}</b> ${escapeHtml(e.city || "")} · ${escapeHtml(e.category || "")}
      <br><span class="muted">runway ${Number(e.runway_length_ft || 0).toLocaleString()} ft ·
      landing $${Number(e.landing_fee_per_1000 || 0).toFixed(2)}/1000lb · gate $${Number(e.gate_fee || 0).toFixed(0)}
      ${flags ? "· " + escapeHtml(flags) : ""}</span></div>`;
  }).join("");
  const market = Number(d.weekly_market_total != null ? d.weekly_market_total : d.total_pax || 0);
  const src = d.demand_source_badge || "Demand";
  const floorNote = d.market_floor_applied ? " · min market" : "";
  return `
    <div class="tip-grid">
      <div><span class="muted">Acquisition</span><br><b>${money(p.total_new_cost)}</b>
        ${Number(p.cost_reference || 0) !== Number(p.total_new_cost || 0)
          ? `<br><span class="muted">one leg would be ${money(p.cost_reference)}</span>` : ""}</div>
      <div><span class="muted">Distance</span><br><b>${Number(p.distance_nm || 0).toLocaleString()} nm</b></div>
      <div><span class="muted">This week's market</span><br><b>${market.toLocaleString()} pax</b>
        <br><span class="muted">${escapeHtml(src)}${escapeHtml(floorNote)} ·
        ${Number(d.business_pax || 0).toLocaleString()} business ·
        ${Number(d.leisure_pax || 0).toLocaleString()} leisure</span>
        <br><span class="muted">Cabins Y${Number(d.economy_pax || 0)} / W${Number(d.premium_economy_pax || 0)} / J${Number(d.business_cabin_pax || 0)} / F${Number(d.first_pax || 0)}
        (split — not the only number)</span>
        <br><span class="muted">Template ${Number(d.base_demand_business || 0)}B / ${Number(d.base_demand_leisure || 0)}L (internal)</span></div>
      <div><span class="muted">Default fares</span><br><b>$${Number(d.price_leisure_default || 0).toFixed(0)}</b> leisure
        <br><span class="muted">$${Number(d.price_business_default || 0).toFixed(0)} business</span></div>
    </div>
    ${labels.hero ? `<div class="tip-sec muted">${escapeHtml(labels.source || "")}</div>` : ""}
    <div class="tip-sec"><span class="muted">Why this price</span><br>${escapeHtml(p.cost_reason || "")}</div>
    <div class="tip-sec"><span class="muted">Legs opened</span>${legLines}</div>
    <div class="tip-sec"><span class="muted">Airports</span>${endLines}</div>`;
}

let airportPickerSeq = 0;

// Type a name, a city, or a code. Uses a native <datalist>, so the browser handles the
// dropdown, filtering and keyboard selection; picking an option puts the IATA in the field.
function attachAirportPicker(input) {
  if (!input || input.dataset.apPicker === "1") return;
  input.dataset.apPicker = "1";
  const id = "ap-list-" + (++airportPickerSeq);
  const list = document.createElement("datalist");
  list.id = id;
  input.setAttribute("list", id);
  input.setAttribute("autocomplete", "off");
  // A code is 3 chars but a name is not; the old maxlength blocked typing "Los Angeles".
  input.removeAttribute("maxlength");
  if (!input.placeholder || input.placeholder.length <= 4) {
    input.placeholder = input.placeholder ? `${input.placeholder} or "Los Angeles"` : 'code, city or name';
  }
  input.parentNode.appendChild(list);
  let timer = null;
  const fill = async () => {
    const q = input.value.trim();
    if (q.length < 2) { list.innerHTML = ""; return; }
    try {
      const r = await api("/api/airports?q=" + encodeURIComponent(q));
      list.innerHTML = (r.airports || []).slice(0, 25).map((a) => {
        const label = [a.name, a.city, a.country].filter(Boolean).join(" — ");
        return `<option value="${escapeHtml(a.iata)}">${escapeHtml(label)}</option>`;
      }).join("");
    } catch (_) { /* typeahead is best-effort */ }
  };
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(fill, 180);
  });
  input.addEventListener("focus", fill);
}

// Turn whatever the player typed into an IATA code. Accepts a code directly, otherwise
// resolves the best search match so "Los Angeles" works as well as "LAX".
async function resolveAirportCode(value) {
  const raw = String(value || "").trim();
  if (!raw) throw new Error("Enter an airport code, city or name.");
  const up = raw.toUpperCase();
  if (/^[A-Z]{3}$/.test(up)) return up;
  const r = await api("/api/airports?q=" + encodeURIComponent(raw));
  const list = r.airports || [];
  if (!list.length) throw new Error(`No airport matches "${raw}".`);
  const exact = list.find((a) => String(a.city || "").toUpperCase() === up
                              || String(a.name || "").toUpperCase() === up);
  return String((exact || list[0]).iata).toUpperCase();
}

function routeSuggestionStatus(row) {
  if (row.network_status === "open") return '<span class="rt-st rt-st-open">In network</span>';
  if (row.network_status === "partial") return '<span class="rt-st rt-st-partial">Partial</span>';
  return "";
}

function renderRouteSuggestionsTable(origin, rows) {
  if (!rows || !rows.length) {
    return `<p class="muted">No strong markets found within range from ${escapeHtml(origin)}.</p>`;
  }
  const hdr = (label) => `<th>${escapeHtml(label)}</th>`;
  const body = rows.map((r) => {
    const label = [r.other_city, r.other_iata].filter(Boolean).join(" · ");
    const out = Number((r.outbound && r.outbound.weekly_demand) || 0);
    const inn = Number((r.inbound && r.inbound.weekly_demand) || 0);
    const cost = Number(r.open_cost || 0);
    const costTxt = r.network_status === "open" ? "—" : money(cost);
    return `<tr class="rt-sug-row" data-dest="${escapeHtml(r.other_iata)}" tabindex="0">
      <td><b>${escapeHtml(label)}</b>${routeSuggestionStatus(r)}</td>
      <td>${out.toLocaleString()}</td>
      <td>${inn.toLocaleString()}</td>
      <td>${Number(r.distance_nm || 0).toLocaleString()} nm</td>
      <td>${escapeHtml(formatBlockHours(r.flight_hours))}</td>
      <td>${escapeHtml(costTxt)}</td>
    </tr>`;
  }).join("");
  return `<table class="grid rt-sug-grid">
    <thead><tr>
      ${hdr("Destination")}
      ${hdr(`${origin} →`)}
      ${hdr(`→ ${origin}`)}
      ${hdr("Distance")}
      ${hdr("Est. time")}
      ${hdr("Open cost")}
    </tr></thead>
    <tbody>${body}</tbody>
  </table>`;
}

function openRoutes() {
  const hubDefault = (lastState && lastState.airline && lastState.airline.home_hub_iata) || "";
  const el = openWindow("routes", "Open route", `
    <div class="row2">
      <div><label>Origin</label><input id="rt-o" placeholder="TPA" maxlength="4" value="${escapeHtml(hubDefault)}" /></div>
      <div id="rt-manual-dest"><label>Destination</label><input id="rt-d" placeholder="MCO" maxlength="4" /></div>
    </div>
    <div class="win-tabs" id="rt-tabs">
      <button type="button" class="active" data-rt-tab="popular">Popular destinations</button>
      <button type="button" data-rt-tab="manual">Manual search</button>
    </div>
    <div id="rt-popular-panel">
      <p class="muted" id="rt-pop-hint">Enter an origin to see popular markets.</p>
      <div id="rt-suggestions"></div>
    </div>
    <div id="rt-manual-panel" hidden>
      <button type="button" id="rt-preview">Preview cost</button>
      <button type="button" id="rt-open">Open route</button>
      <div id="rt-prev" class="muted"></div>
    </div>
    <div id="rt-list"></div>`);

  const originInput = $("#rt-o", el);
  const destWrap = $("#rt-manual-dest", el);
  const popularPanel = $("#rt-popular-panel", el);
  const manualPanel = $("#rt-manual-panel", el);
  const sugBox = $("#rt-suggestions", el);
  const popHint = $("#rt-pop-hint", el);

  attachAirportPicker(originInput);
  attachAirportPicker($("#rt-d", el));

  let sugTimer = null;
  let sugSeq = 0;
  let lastSugOrigin = "";

  const setTab = (name) => {
    const popular = name === "popular";
    popularPanel.hidden = !popular;
    manualPanel.hidden = popular;
    destWrap.hidden = popular;
    el.querySelectorAll("#rt-tabs button").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.rtTab === name);
    });
  };

  el.querySelectorAll("#rt-tabs button").forEach((btn) => {
    btn.onclick = () => setTab(btn.dataset.rtTab || "popular");
  });

  const previewRoute = async () => {
    try {
      const o = await resolveAirportCode(originInput.value);
      const d = await resolveAirportCode($("#rt-d", el).value);
      originInput.value = o;
      $("#rt-d", el).value = d;
      const p = await api(`/api/routes/preview?origin=${encodeURIComponent(o)}&dest=${encodeURIComponent(d)}`);
      const note = p.already_operated ? " · you already operate this" : "";
      const dem = p.demand || {};
      const market = Number(dem.weekly_market_total != null ? dem.weekly_market_total : dem.total_pax || 0);
      const src = dem.demand_source_badge || "Demand";
      const floorNote = dem.market_floor_applied ? " · min market" : "";
      $("#rt-prev", el).innerHTML =
        `<span class="tip">Cost ~ <b>${money(p.total_new_cost)}</b> · ${Number(p.distance_nm || 0).toLocaleString()} nm`
        + ` · <b>${market.toLocaleString()}</b> pax/wk market`
        + (src ? ` · ${escapeHtml(src)}${escapeHtml(floorNote)}` : "")
        + `${escapeHtml(note)}`
        + ` <span class="tip-mark">details</span>`
        + `<span class="tip-body">${routePreviewCard(p)}</span></span>`;
    } catch (err) { $("#rt-prev", el).innerHTML = `<span class="err">${err.message}</span>`; }
  };

  const wireSuggestionRows = () => {
    sugBox.querySelectorAll(".rt-sug-row").forEach((row) => {
      const pick = async () => {
        const dest = row.dataset.dest;
        if (!dest) return;
        $("#rt-d", el).value = dest;
        setTab("manual");
        await previewRoute();
      };
      row.onclick = pick;
      row.onkeydown = (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          pick();
        }
      };
    });
  };

  const loadSuggestions = async () => {
    const raw = originInput.value.trim();
    if (!raw) {
      popHint.textContent = "Enter an origin to see popular markets.";
      sugBox.innerHTML = "";
      lastSugOrigin = "";
      return;
    }
    const seq = ++sugSeq;
    popHint.textContent = "Loading markets…";
    sugBox.innerHTML = "";
    let origin;
    try {
      origin = await resolveAirportCode(raw);
    } catch (err) {
      if (seq !== sugSeq) return;
      popHint.textContent = err.message || "Unknown origin.";
      return;
    }
    if (seq !== sugSeq) return;
    if (origin !== raw.toUpperCase()) originInput.value = origin;
    if (origin === lastSugOrigin && sugBox.dataset.loaded === "1") {
      popHint.textContent = `Markets from ${origin}`;
      return;
    }
    try {
      const r = await api(`/api/routes/suggestions?origin=${encodeURIComponent(origin)}&limit=25`);
      if (seq !== sugSeq) return;
      lastSugOrigin = origin;
      sugBox.dataset.loaded = "1";
      const rows = r.suggestions || [];
      popHint.textContent = rows.length
        ? `Top markets from ${origin} (weekly pax, both directions)`
        : `No strong markets found from ${origin}.`;
      sugBox.innerHTML = renderRouteSuggestionsTable(origin, rows);
      wireSuggestionRows();
    } catch (err) {
      if (seq !== sugSeq) return;
      popHint.textContent = "";
      sugBox.innerHTML = `<span class="err">${escapeHtml(err.message)}</span>`;
    }
  };

  const scheduleSuggestions = () => {
    sugBox.dataset.loaded = "0";
    clearTimeout(sugTimer);
    sugTimer = setTimeout(() => { loadSuggestions().catch(() => {}); }, 350);
  };

  originInput.addEventListener("input", scheduleSuggestions);
  originInput.addEventListener("change", () => { loadSuggestions().catch(() => {}); });

  const showList = async () => {
    const list = await api("/api/routes");
    $("#rt-list", el).innerHTML = `<p class="muted">Your routes</p><table class="grid"><tbody>${
      (list.routes || []).map((r) => `<tr><td>${escapeHtml(r.route_id)}</td><td>${Number(r.distance_nm || 0).toFixed(0)} nm</td><td><button type="button" data-route="${escapeHtml(r.route_id)}">Detail</button></td></tr>`).join("")
    }</tbody></table>`;
    el.querySelectorAll("[data-route]").forEach((btn) => {
      btn.onclick = () => openRouteDetail(btn.dataset.route);
    });
  };
  $("#rt-preview", el).onclick = () => { previewRoute().catch(() => {}); };
  $("#rt-open", el).onclick = async () => {
    try {
      await api("/api/routes", {
        method: "POST",
        body: JSON.stringify({
          origin: await resolveAirportCode(originInput.value),
          dest: await resolveAirportCode($("#rt-d", el).value),
        }),
      });
      toast("Route opened");
      await refreshHud();
      await showList();
      sugBox.dataset.loaded = "0";
      lastSugOrigin = "";
      await loadSuggestions();
    } catch (err) { toast(err.message); }
  };
  setTab("popular");
  showList().catch((err) => toast(err.message));
  if (hubDefault) loadSuggestions().catch(() => {});
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function scheduleTables(view) {
  if (!view) return `<p class="muted">No schedule.</p>`;
  const planned = view.planned || [];
  const days = view.days || ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"];
  const list = planned.length
    ? `<table class="grid"><thead><tr><th>Day</th><th>Dep</th><th>Arr</th><th>Flight</th><th>Route</th><th>Status</th></tr></thead><tbody>${
        planned.map((r) => `<tr><td>${escapeHtml(r.day)}</td><td>${escapeHtml(r.dep)}</td><td>${escapeHtml(r.arr)}</td><td>${escapeHtml(r.flight)}</td><td>${escapeHtml(r.route)}</td><td>${escapeHtml(r.status)}</td></tr>`).join("")
      }</tbody></table>`
    : `<p class="muted">No flights scheduled for this tail in week ${view.game_week}.</p>`;
  const rows = (view.hours || []).map((h) => {
    const tds = (h.cells || []).map((cell) => {
      if (!cell || !cell.length) return `<td class="empty">·</td>`;
      const html = cell.map((ln) => `<div class="${ln.overlap ? "overlap" : ""}">${ln.overlap ? "⚠ " : ""}${escapeHtml(ln.text)}</div>`).join("");
      return `<td>${html}</td>`;
    }).join("");
    return `<tr><td class="time">${escapeHtml(h.hour)}</td>${tds}</tr>`;
  }).join("");
  const grid = `<p class="muted">Week ${view.game_week} · ${view.tail_number} · rows = local hour of departure</p>
    <table class="sched-grid"><thead><tr><th>Time</th>${days.map((d) => `<th>${d}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table>`;
  return `<p class="muted">Planned schedule (departure order)</p>${list}${grid}`;
}

function openScheduleResult(title, data) {
  const u = data.utilization || {};
  openWindow("sched-result", title, `
    <p><b>${escapeHtml(data.tail_number || "")}</b> · ${escapeHtml(data.chain || "")}</p>
    ${data.cleared ? "<p>Schedule cleared.</p>" : `<p>Flights this action: ${data.flights_created != null ? data.flights_created : "—"}</p>`}
    ${u.airborne_hours != null ? `<p class="muted">Week airborne ${Number(u.airborne_hours).toFixed(1)} / ${Number(u.cap_hours).toFixed(0)} h</p>` : ""}
    ${scheduleTables(data.schedule)}
  `, { width: 1080 });
}

function legEditorRows(preview) {
  const legs = preview.legs || (preview.route_ids || []).map((rid, i) => ({
    route_id: rid, flight_number: "", turn_minutes: preview.min_turn_minutes || 30,
  }));
  if (!legs.length) return "";
  const minTurn = Number(preview.min_turn_minutes || 30);
  const rows = legs.map((leg, i) => `
    <tr>
      <td class="muted">${i + 1}</td>
      <td><b>${escapeHtml(leg.route_id || "")}</b></td>
      <td><input class="leg-fn" data-i="${i}" type="text" value="${escapeHtml(leg.flight_number || "")}"
                 placeholder="auto" size="10" /></td>
      <td><input class="leg-turn" data-i="${i}" type="number" value="${Number(leg.turn_minutes || minTurn)}"
                 min="${minTurn}" max="1440" step="5" size="5" /> <span class="muted">min</span></td>
    </tr>`).join("");
  return `
    <p><b>Legs — flight number and turnaround</b></p>
    <p class="muted">Turnaround is ground time after that leg lands, before the next departs.
       Minimum is ${minTurn} min (global MTT floor). Turnaround is gate hold time for that leg
       and spacing before the next departure. Flight numbers are sticky per route (random 4-digit);
       same number may repeat the same day only if times do not overlap. Clear a field to re-auto.</p>
    <table class="leg-editor">
      <thead><tr><th>#</th><th>Leg</th><th>Flight no.</th><th>Turnaround</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function collectLegs(el, preview) {
  const ids = (preview.legs || []).map((l) => l.route_id) ;
  const routeIds = ids.length ? ids : (preview.route_ids || []);
  return routeIds.map((rid, i) => {
    const fnEl = el.querySelector(`.leg-fn[data-i="${i}"]`);
    const tEl = el.querySelector(`.leg-turn[data-i="${i}"]`);
    return {
      route_id: rid,
      flight_number: fnEl ? fnEl.value.trim() : "",
      turn_minutes: tEl && tEl.value !== "" ? Number(tEl.value) : null,
    };
  });
}

function openScheduleOptions(preview, chainRaw) {
  const existing = document.getElementById("win-sched-opts");
  if (existing) existing.remove();
  const u = preview.utilization || {};
  const el = openWindow("sched-opts", "Assign rotation", `
    <p><b>${escapeHtml(preview.tail_number)}</b> · plan <b>${escapeHtml(preview.chain)}</b></p>
    <p class="muted">Airborne ${Number(u.airborne_hours || 0).toFixed(1)} / ${Number(u.cap_hours || 0).toFixed(0)} h this week</p>
    ${legEditorRows(preview)}
    ${scheduleTables(preview.schedule)}
    <p>How do you want to schedule this chain?</p>
    <button type="button" id="so-quick">1. Quick schedule (from now)</button>
    <button type="button" id="so-clear">3. Clear this tail's week</button>
    <div id="so-detail-form">
      <p><b>2. Detailed — operating days and first departure</b></p>
      <p class="muted">Schedules repeat every sim week (Mon–Sun), not a calendar date. Check the days this rotation flies, then set the first-leg local time. Later legs follow after flight time plus the turnaround you set above.</p>
      <div class="day-presets">
        <button type="button" data-preset="DAILY">Daily</button>
        <button type="button" data-preset="WEEKDAYS">Weekdays</button>
        <button type="button" data-preset="WEEKENDS">Weekends</button>
      </div>
      <div class="day-picks" id="so-days">
        ${["MON","TUE","WED","THU","FRI","SAT","SUN"].map((d) =>
          `<label><input type="checkbox" value="${d}" checked /> ${d}</label>`
        ).join("")}
      </div>
      <label>First departure (local HH:MM)</label>
      <input id="so-time" type="time" value="08:00" step="60" />
      <button type="button" id="so-detail-go">Save detailed schedule</button>
    </div>
    <div class="msg"></div>
  `, { width: 1080 });
  const body = el.querySelector(".win-body");
  const payloadBase = { tail_number: preview.tail_number, chain: chainRaw };
  const dayBoxes = () => [...el.querySelectorAll("#so-days input[type=checkbox]")];
  const selectedDays = () => dayBoxes().filter((b) => b.checked).map((b) => b.value);
  const setDays = (days) => {
    const set = new Set(days);
    dayBoxes().forEach((b) => { b.checked = set.has(b.value); });
  };
  el.querySelectorAll("[data-preset]").forEach((btn) => {
    btn.onclick = () => {
      const p = btn.dataset.preset;
      if (p === "WEEKDAYS") setDays(["MON", "TUE", "WED", "THU", "FRI"]);
      else if (p === "WEEKENDS") setDays(["SAT", "SUN"]);
      else setDays(["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]);
    };
  });
  $("#so-quick", el).onclick = async () => {
    try {
      const out = await api("/api/schedule", { method: "POST", body: JSON.stringify({ ...payloadBase, mode: "quick", legs: collectLegs(el, preview) }) });
      openScheduleResult("Rotation assigned", out);
    } catch (err) { setMsg(body, err.message, false); }
  };
  $("#so-detail-go", el).onclick = async () => {
    const days = selectedDays();
    if (!days.length) {
      setMsg(body, "Pick at least one operating day.", false);
      return;
    }
    try {
      const out = await api("/api/schedule", {
        method: "POST",
        body: JSON.stringify({
          ...payloadBase,
          mode: "detailed",
          days,
          departure_time: $("#so-time", el).value,
          legs: collectLegs(el, preview),
        }),
      });
      openScheduleResult("Detailed rotation saved", out);
    } catch (err) { setMsg(body, err.message, false); }
  };
  $("#so-clear", el).onclick = async () => {
    try {
      const out = await api("/api/schedule", { method: "POST", body: JSON.stringify({ tail_number: preview.tail_number, mode: "clear" }) });
      openScheduleResult("Schedule cleared", out);
    } catch (err) { setMsg(body, err.message, false); }
  };
}

function formatTailUtil(u) {
  if (!u || u.airborne_hours == null) return "";
  return `${Number(u.airborne_hours).toFixed(1)} / ${Number(u.cap_hours).toFixed(0)} h (${Number(u.utilization_pct).toFixed(0)}%)`;
}

function fleetOwnershipLabel(r) {
  const own = String(r.ownership || "").toUpperCase();
  return own === "LEASED" ? "Leased" : "Owned";
}

function fleetLeaseWeeksLabel(r) {
  if (String(r.ownership || "").toUpperCase() !== "LEASED") return "—";
  const rem = Number(r.lease_weeks_remaining);
  if (!Number.isFinite(rem) || rem <= 0) return "—";
  return `${Math.floor(rem)} wk`;
}

function readLeaseWeeksInput(el, fallback = 52) {
  const raw = el && el.value != null ? String(el.value).trim() : "";
  const n = Math.floor(Number(raw));
  if (!Number.isFinite(n) || n < 1) return Math.max(1, Math.floor(Number(fallback) || 52));
  return Math.min(520, n);
}

function fillTailSelect(sel, fleet, { includeUtil } = {}) {
  sel.innerHTML = "";
  (fleet || []).forEach((r) => {
    const o = document.createElement("option");
    o.value = r.tail_number;
    const loc = r.current_airport_iata ? ` @ ${r.current_airport_iata}` : "";
    const util = includeUtil ? formatTailUtil(r.utilization) : "";
    const own = fleetOwnershipLabel(r);
    const lease = fleetLeaseWeeksLabel(r);
    o.textContent = [r.tail_number, r.type_id, own, lease !== "—" ? lease : null, r.status, util]
      .filter(Boolean).join(" · ") + loc;
    o.dataset.util = util;
    o.dataset.over = r.utilization && r.utilization.at_or_over_limit ? "1" : "";
    sel.appendChild(o);
  });
}

function openTailGrid(preselectTail) {
  const existing = document.getElementById("win-tailgrid");
  if (existing) existing.remove();
  const days = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"];
  const el = openWindow("tailgrid", "Aircraft detail", `
    <form id="tg-form">
      <label>Tail</label>
      <select id="tg-tail"></select>
      <button type="submit">Load</button>
    </form>
    <div class="win-tabs">
      <button type="button" data-tgtab="schedule" class="active">Schedule</button>
      <button type="button" data-tgtab="reposition">Reposition</button>
    </div>
    <div id="tg-panel-schedule">
      <div id="tg-out" class="muted">Pick a tail.</div>
    </div>
    <div id="tg-panel-reposition" hidden>
      <div id="tg-repos-out" class="muted">Pick a tail.</div>
    </div>
  `, { width: 1080 });
  let currentTail = "";
  let activeTab = "schedule";

  const showTab = (id) => {
    activeTab = id;
    el.querySelectorAll("[data-tgtab]").forEach((b) => b.classList.toggle("active", b.dataset.tgtab === id));
    $("#tg-panel-schedule", el).hidden = id !== "schedule";
    $("#tg-panel-reposition", el).hidden = id !== "reposition";
    if (id === "reposition" && currentTail) {
      renderReposition(currentTail).catch((err) => setMsg($("#tg-repos-out", el), err.message, false));
    }
  };
  el.querySelectorAll("[data-tgtab]").forEach((b) => {
    b.onclick = () => showTab(b.dataset.tgtab);
  });

  const showGrid = async (tail) => {
    const out = await api("/api/schedule?tail=" + encodeURIComponent(tail));
    const ac = out.aircraft || {};
    const ownLine = ac.ownership
      ? `<p class="muted">${escapeHtml(fleetOwnershipLabel(ac))}${ac.ownership === "LEASED" && ac.lease_weeks_remaining != null
        ? ` · ${fleetLeaseWeeksLabel(ac)} remaining` : ""}${ac.lease_weekly_cost ? ` · ${money(ac.lease_weekly_cost)}/wk` : ""}</p>`
      : "";
    const hasRows = (out.schedule && out.schedule.planned && out.schedule.planned.length);
    $("#tg-out", el).innerHTML = `${ownLine}${scheduleTables(out.schedule)}${
      hasRows ? `<button type="button" id="tg-clear">Clear this tail's week</button>` : ""
    }`;
    const clearBtn = $("#tg-clear", el);
    if (clearBtn) {
      clearBtn.onclick = async () => {
        if (!window.confirm(`Clear ${tail}'s weekly schedule? Landed flights this week leave the grid; cash already earned stays.`)) return;
        try {
          const cleared = await api("/api/schedule", {
            method: "POST",
            body: JSON.stringify({ tail_number: tail, mode: "clear" }),
          });
          toast("Schedule cleared");
          $("#tg-out", el).innerHTML = scheduleTables(cleared.schedule);
        } catch (err) { setMsg($("#tg-out", el), err.message, false); }
      };
    }
    return out;
  };

  const renderReposition = async (tail) => {
    const out = await api("/api/schedule?tail=" + encodeURIComponent(tail));
    const ac = out.aircraft || {};
    const loc = ac.current_airport_iata || "—";
    const earliest = ac.earliest_dep_day && ac.earliest_dep
      ? `${ac.earliest_dep_day} ${ac.earliest_dep}` : "—";
    const rangeNote = ac.range_nm != null ? ` · range ${Number(ac.range_nm).toLocaleString()} nm` : "";
    if (String(ac.status || "").toUpperCase() === "AOG") {
      $("#tg-repos-out", el).innerHTML = `<p class="err">${escapeHtml(tail)} is AOG — repair before repositioning.</p>`;
      return;
    }
    $("#tg-repos-out", el).innerHTML = `
      <p><b>${escapeHtml(tail)}</b> · ${escapeHtml(ac.type_id || "")} · ${escapeHtml(ac.status || "")}${escapeHtml(rangeNote)}</p>
      <p>Currently at <b>${escapeHtml(loc)}</b>. Earliest departure: <b>${escapeHtml(earliest)}</b> (after turnaround).</p>
      <p class="muted">Week ${Number(ac.game_week || 1)} · Reposition schedules a ferry with no passengers and <b>clears the rest of this tail's planned week</b> (airborne legs finish). Fuel and airport fees still apply.</p>
      <form id="tg-repos-form">
        <div class="row2">
          <div><label>Destination airport</label><input id="tg-repos-dest" placeholder="code, city or name" /></div>
          <div><label>Day (this game week)</label>
            <select id="tg-repos-day">${days.map((d) => `<option value="${d}">${d}</option>`).join("")}</select>
          </div>
        </div>
        <div class="row2">
          <div><label>Departure time</label><input id="tg-repos-time" type="time" value="08:00" /></div>
          <div></div>
        </div>
        <button type="submit">Schedule reposition</button>
      </form>
      <div id="tg-repos-msg"></div>`;
    if (ac.earliest_dep_day) $("#tg-repos-day", el).value = ac.earliest_dep_day;
    if (ac.earliest_dep) $("#tg-repos-time", el).value = ac.earliest_dep;
    attachAirportPicker($("#tg-repos-dest", el));
    $("#tg-repos-form", el).onsubmit = async (e) => {
      e.preventDefault();
      try {
        const dest = await resolveAirportCode($("#tg-repos-dest", el).value);
        const day = $("#tg-repos-day", el).value;
        const time = $("#tg-repos-time", el).value.slice(0, 5);
        const res = await api("/api/fleet/reposition", {
          method: "POST",
          body: JSON.stringify({
            tail_number: tail,
            dest_iata: dest,
            day,
            departure_time: time,
          }),
        });
        toast(res.message || "Reposition scheduled");
        showTab("schedule");
        await showGrid(tail);
      } catch (err) { setMsg($("#tg-repos-msg", el), err.message, false); }
    };
  };

  const loadTail = async (tail) => {
    currentTail = tail;
    if (activeTab === "reposition") {
      $("#tg-repos-out", el).textContent = "Loading…";
      await renderReposition(tail);
    } else {
      $("#tg-out", el).textContent = "Loading…";
      await showGrid(tail);
    }
  };

  api("/api/fleet").then((data) => {
    const sel = $("#tg-tail", el);
    fillTailSelect(sel, data.fleet || [], { includeUtil: true });
    if (preselectTail) {
      sel.value = preselectTail;
      loadTail(preselectTail).catch((err) => setMsg(el.querySelector(".win-body"), err.message, false));
    }
  }).catch((err) => setMsg(el.querySelector(".win-body"), err.message, false));
  $("#tg-form", el).onsubmit = async (e) => {
    e.preventDefault();
    const tail = $("#tg-tail", el).value;
    try {
      await loadTail(tail);
    } catch (err) { setMsg(el.querySelector(".win-body"), err.message, false); }
  };
}

function money2(n) {
  return Number(n || 0).toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 });
}

function formatBlockHours(h) {
  const n = Number(h);
  if (!(n > 0)) return "—";
  const hh = Math.floor(n);
  const mm = Math.round((n - hh) * 60) % 60;
  return `${hh}h ${String(mm).padStart(2, "0")}m`;
}

function openRouteDetail(preselect) {
  const existing = document.getElementById("win-routedetail");
  if (existing) existing.remove();
  const el = openWindow("routedetail", "Route detail", `
    <p class="muted" id="rd-week">Loading your routes…</p>
    <div id="rd-table"></div>
    <div id="rd-panel" class="rd-panel" hidden></div>
  `, { width: 820 });

  let overview = [];

  const showFares = async (routeId) => {
    const panel = $("#rd-panel", el);
    panel.hidden = false;
    panel.innerHTML = `<p class="muted">Loading ${escapeHtml(routeId)}…</p>`;
    try {
      const d = await api("/api/routes/detail?route_id=" + encodeURIComponent(routeId));
      const r = d.route || {};
      const p = d.performance;
      const ops = d.ops;
      const rc = (ops && ops.remaining_cabin) || {};
      const origin = String(r.origin_iata || "").toUpperCase();
      const dest = String(r.dest_iata || "").toUpperCase();
      const reverseId = origin && dest ? `${dest}-${origin}` : "";
      const reverse = reverseId
        ? overview.find((row) => String(row.route_id || "").toUpperCase() === reverseId)
        : null;
      const reverseBtn = reverse
        ? `<button type="button" id="rd-apply-reverse">Apply to ${escapeHtml(reverse.route_id)}</button>`
        : "";
      panel.innerHTML = `
        <div class="rd-panel-head">
          <div>
            <b>${escapeHtml(r.route_id)}</b>
            · ${escapeHtml(r.origin_city || r.origin_iata)} → ${escapeHtml(r.dest_city || r.dest_iata)}
            · ${Number(r.distance_nm || 0).toLocaleString()} nm
          </div>
          <button type="button" id="rd-close-panel">Close</button>
        </div>
        <p class="muted">This week: ${escapeHtml(d.schedule || "no flights scheduled")}</p>
        ${p ? `<p><b>This week's market: ${(p.weekly_market_total != null ? p.weekly_market_total : (Number(p.business_pax||0)+Number(p.leisure_pax||0))).toLocaleString()} pax</b>
          · ${escapeHtml(p.demand_source_badge || "Demand")}${p.market_floor_applied ? " · min market" : ""}</p>
          <p class="muted">${Number(p.business_pax||0).toLocaleString()} business · ${Number(p.leisure_pax||0).toLocaleString()} leisure
          · cabin split Y${p.economy_pax} / W${p.premium_economy_pax} / J${p.business_cabin_pax} / F${p.first_pax}</p>
          <p class="muted">Template base ${Number(r.base_demand_business||0)}B / ${Number(r.base_demand_leisure||0)}L (internal)</p>
          <p>One-aircraft projection: ${p.aircraft_fill_pax != null ? p.aircraft_fill_pax : p.total_pax} pax · LF ${(Number(p.load_factor) * 100).toFixed(0)}% · gross ${money2(p.gross_revenue)}</p>` : ""}
        ${ops ? `<p class="muted">Week ${ops.game_week} — market pool ${ops.weekly_business}/${ops.weekly_leisure} · carried ${ops.carried_business}/${ops.carried_leisure}
          · remaining cabin F${rc.F || 0} / J${rc.J || 0} / W${rc.W || 0} / Y${rc.Y || 0}</p>` : ""}
        <form id="rd-fares">
          <div class="row2">
            <div><label>Leisure / Y base</label><input name="price_leisure" type="number" min="0.01" step="0.01" value="${r.price_leisure}" /></div>
            <div><label>Premium economy / W</label><input name="price_premium_economy" type="number" min="0.01" step="0.01" value="${r.price_premium_economy}" /></div>
            <div><label>Business / J base</label><input name="price_business" type="number" min="0.01" step="0.01" value="${r.price_business}" /></div>
            <div><label>First / F</label><input name="price_first" type="number" min="0.01" step="0.01" value="${r.price_first}" /></div>
          </div>
          <p class="muted">Y and J bases feed demand vs distance reference fares. W and F are list tickets.</p>
          <div class="row2" style="align-items:center;gap:8px">
            <button type="submit">Save fares</button>
            ${reverseBtn}
          </div>
          ${reverse ? `<p class="muted">Copies the fares above onto ${escapeHtml(reverse.route_id)} (already opened).</p>` : ""}
        </form>`;
      $("#rd-close-panel", el).onclick = () => { panel.hidden = true; panel.innerHTML = ""; };
      const fareBody = (targetRouteId) => {
        const fd = new FormData($("#rd-fares", el));
        return {
          route_id: targetRouteId,
          price_leisure: Number(fd.get("price_leisure")),
          price_premium_economy: Number(fd.get("price_premium_economy")),
          price_business: Number(fd.get("price_business")),
          price_first: Number(fd.get("price_first")),
        };
      };
      $("#rd-fares", el).onsubmit = async (e) => {
        e.preventDefault();
        try {
          await api("/api/routes/prices", {
            method: "POST",
            body: JSON.stringify(fareBody(r.route_id)),
          });
          toast("Fares updated");
          await showFares(r.route_id);
          await loadTable();
        } catch (err) { toast(err.message); }
      };
      const applyRev = $("#rd-apply-reverse", el);
      if (applyRev && reverse) {
        applyRev.onclick = async () => {
          try {
            await api("/api/routes/prices", {
              method: "POST",
              body: JSON.stringify(fareBody(reverse.route_id)),
            });
            toast(`Fares applied to ${reverse.route_id}`);
          } catch (err) { toast(err.message); }
        };
      }
    } catch (err) {
      panel.innerHTML = `<div class="err">${escapeHtml(err.message)}</div>`;
    }
  };

  const renderTable = (rows) => {
    if (!rows.length) {
      $("#rd-table", el).innerHTML = `<p class="muted">Open a route first (Open route).</p>`;
      return;
    }
    $("#rd-table", el).innerHTML = `
      <table class="grid rd-grid">
        <thead>
          <tr>
            <th>Route</th>
            <th>Cities</th>
            <th>Distance</th>
            <th>Flight time</th>
            <th>Remaining demand</th>
            <th>Source</th>
            <th>Ops</th>
            <th>Flights this week</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((r) => {
            const est = r.flight_hours_estimated ? " · est." : "";
            const rc = r.remaining_cabin || null;
            const dem = rc
              ? `F${Number(rc.F || 0).toLocaleString()} / J${Number(rc.J || 0).toLocaleString()} / W${Number(rc.W || 0).toLocaleString()} / Y${Number(rc.Y || 0).toLocaleString()}`
              : "—";
            const demTitle = "Remaining cabin market this week (after carried + scheduled absorption)";
            const src = `${escapeHtml(r.demand_source_badge || "Demand")}${r.market_floor_applied ? " · min" : ""}`;
            return `<tr data-rid="${escapeHtml(r.route_id)}">
              <td><b>${escapeHtml(r.route_id)}</b></td>
              <td>${escapeHtml(r.origin_city || r.origin_iata || "")} → ${escapeHtml(r.dest_city || r.dest_iata || "")}</td>
              <td>${Number(r.distance_nm || 0).toLocaleString()} nm</td>
              <td>${escapeHtml(formatBlockHours(r.flight_hours))}${escapeHtml(est)}</td>
              <td title="${escapeHtml(demTitle)}">${escapeHtml(dem)}</td>
              <td>${src}</td>
              <td>${Number(r.weekly_ops || 0)}</td>
              <td class="rd-flights">${escapeHtml(r.flights_label || "—")}</td>
              <td><button type="button" data-fares="${escapeHtml(r.route_id)}">Fares</button></td>
            </tr>`;
          }).join("")}
        </tbody>
      </table>`;
    el.querySelectorAll("[data-fares]").forEach((btn) => {
      btn.onclick = () => showFares(btn.dataset.fares);
    });
  };

  const loadTable = async () => {
    const data = await api("/api/routes/overview");
    overview = data.routes || [];
    $("#rd-week", el).textContent = `Week ${data.week || "—"} · ${overview.length} opened route${overview.length === 1 ? "" : "s"}`;
    renderTable(overview);
    if (preselect && overview.some((r) => r.route_id === preselect)) {
      await showFares(preselect);
    }
  };

  loadTable().catch((err) => setMsg(el.querySelector(".win-body"), err.message, false));
}

function openSchedule() {
  const existing = document.getElementById("win-schedule");
  if (existing) existing.remove();
  const el = openWindow("schedule", "Assign rotation", `
    <form id="sch-form">
      <label>Tail</label>
      <select id="sch-tail" required></select>
      <p id="sch-util" class="muted">Select a tail to see this week’s airborne utilization.</p>
      <label>Airport chain</label><input id="sch-chain" name="chain" placeholder="TPA-MCO-TPA" required />
      <p class="muted">Press Enter or Continue after the chain. Example: TPA-MCO-TPA. Each leg must already be an open route.</p>
      <button type="submit">Continue</button>
    </form>`);
  const sel = $("#sch-tail", el);
  const utilLine = $("#sch-util", el);
  const showUtil = () => {
    const opt = sel.selectedOptions[0];
    if (!opt || !opt.value) {
      utilLine.className = "muted";
      utilLine.textContent = "No aircraft in fleet. Buy or lease one first.";
      return;
    }
    const over = opt.dataset.over === "1";
    utilLine.className = over ? "err" : "muted";
    utilLine.textContent = over
      ? `Week airborne ${opt.dataset.util} — at or over the weekly cap.`
      : `Week airborne utilization: ${opt.dataset.util}`;
  };
  api("/api/fleet").then((data) => {
    const rows = data.fleet || [];
    if (!rows.length) {
      sel.innerHTML = `<option value="">No aircraft</option>`;
      showUtil();
      return;
    }
    fillTailSelect(sel, rows, { includeUtil: true });
    showUtil();
  }).catch((err) => setMsg(el.querySelector(".win-body"), err.message, false));
  sel.onchange = showUtil;
  $("#sch-form", el).onsubmit = async (e) => {
    e.preventDefault();
    const body = el.querySelector(".win-body");
    try {
      const chain = $("#sch-chain", el).value;
      const preview = await api("/api/schedule/preview", {
        method: "POST",
        body: JSON.stringify({ tail_number: sel.value, chain }),
      });
      openScheduleOptions(preview, chain);
    } catch (err) { setMsg(body, err.message, false); }
  };
}

function openGates() {
  const el = openWindow("gates", "Gate auctions", `<div class="muted">Loading…</div>`, { width: 560 });
  const render = async () => {
    const [data, bids, gates, notes] = await Promise.all([
      api("/api/gates/auctions"),
      api("/api/gates/bids").catch(() => ({ bids: [], recent: [] })),
      api("/api/gates").catch(() => ({ gates: [] })),
      api("/api/notifications").catch(() => ({ notifications: [] })),
    ]);
    const gateNotes = (notes.notifications || []).filter((n) =>
      String(n.type || "").includes("GATE") || /gate auction/i.test(String(n.body || ""))
    );
    const noticeHtml = gateNotes.length
      ? `<p>Gate results</p><ul class="muted">${gateNotes.slice(0, 8).map((n) =>
          `<li>Wk ${n.game_week} · ${escapeHtml(n.body || "")}</li>`
        ).join("")}</ul>`
      : "";
    const held = (gates.gates || []).map((g) =>
      `<tr><td>${escapeHtml(g.airport_iata || "")}</td><td>${g.gate_units}</td><td>${g.effective_week}</td></tr>`
    ).join("") || `<tr><td colspan="3" class="muted">None yet</td></tr>`;
    const myBids = (bids.bids || []).map((b) =>
      `<tr><td>${escapeHtml(b.airport_iata || "")}</td><td>${b.units_requested}</td><td>${money(b.price_per_unit)}</td><td>wk ${b.closes_week}</td></tr>`
    ).join("") || `<tr><td colspan="4" class="muted">None</td></tr>`;
    const recent = (bids.recent || []).slice(0, 8).map((b) =>
      `<tr><td>${escapeHtml(b.airport_iata || "")}</td><td>${b.units_requested}</td><td>${money(b.price_per_unit)}</td><td>${escapeHtml(b.status || "")}</td></tr>`
    ).join("");
    el.querySelector(".win-body").innerHTML = `
      <p class="muted">Week ${data.week} · auctions close end of this week. Won stands appear under Held and become usable next week.</p>
      ${noticeHtml}
      <div class="row2">
        <div><label>Airport</label><input id="g-iata" placeholder="DFW" /></div>
        <div><label>Units</label><input id="g-units" type="number" value="1" min="1" /></div>
      </div>
      <label>$ / unit</label><input id="g-price" type="number" value="8000" min="1" />
      <button type="button" id="g-bid">Place bid</button>
      <table class="grid"><thead><tr><th>Airport</th><th>Avail</th><th>$ now</th><th>Closes wk</th></tr></thead><tbody>${
        (data.auctions || []).map((a) => `<tr data-iata="${a.airport_iata}" style="cursor:pointer">
          <td>${a.airport_iata}</td><td>${a.units_available}</td>
          <td>${money(a.current_price_per_unit)}</td><td>${a.closes_week}</td></tr>`).join("")
      }</tbody></table>
      <p>Your open bids</p>
      <table class="grid"><thead><tr><th>Airport</th><th>Units</th><th>$/unit</th><th>Closes</th></tr></thead><tbody>${myBids}</tbody></table>
      ${recent ? `<p>Recent closed bids</p><table class="grid"><thead><tr><th>Airport</th><th>Units</th><th>$/unit</th><th>Status</th></tr></thead><tbody>${recent}</tbody></table>` : ""}
      <p>Held stands</p>
      <table class="grid"><thead><tr><th>Airport</th><th>Units</th><th>From wk</th></tr></thead><tbody>${held}</tbody></table>`;
    attachAirportPicker($("#g-iata", el));
    el.querySelectorAll("tbody tr[data-iata]").forEach((tr) => {
      tr.onclick = () => {
        $("#g-iata", el).value = tr.dataset.iata;
        const priceCell = tr.children[2].textContent.replace(/[^0-9.]/g, "");
        if (priceCell) $("#g-price", el).value = priceCell;
      };
    });
    $("#g-bid", el).onclick = async () => {
      try {
        await api("/api/gates/bid", {
          method: "POST",
          body: JSON.stringify({
            airport_iata: await resolveAirportCode($("#g-iata", el).value),
            units: Number($("#g-units", el).value),
            price_per_unit: Number($("#g-price", el).value),
          }),
        });
        toast("Bid recorded — resolves at end of this week");
        await render();
      } catch (err) { toast(err.message); }
    };
  };
  render().catch((err) => { el.querySelector(".win-body").innerHTML = `<div class="err">${err.message}</div>`; });
}

function openBids() {
  const el = openWindow("bids", "My bids & stands", `<div class="muted">Loading…</div>`, { width: 620 });
  Promise.all([
    api("/api/gates/bids"),
    api("/api/gates"),
    api("/api/slots/bids").catch(() => ({ bids: [] })),
    api("/api/notifications").catch(() => ({ notifications: [] })),
  ]).then(([bids, gates, slots, notes]) => {
    const notices = notes.notifications || [];
    const noticeHtml = notices.length
      ? `<p>Unread notices</p>
         <ul class="muted">${notices.map((n) => `<li>Wk ${n.game_week} · ${escapeHtml(n.body || n.type || "")}</li>`).join("")}</ul>
         <button type="button" id="n-ack">Mark notices read</button>`
      : "";
    const utilRows = (gates.gates || []).map((g) => {
      const gapLines = (g.gaps || []).map((x) => `G${x.gate}: ${(x.windows || []).join(", ")}`).join("<br>");
      return `<tr>
        <td>${escapeHtml(g.airport_iata || "")}</td>
        <td>${g.gate_units}</td>
        <td>${g.touches ?? "—"}</td>
        <td>${g.util_pct != null ? `${g.util_pct}%` : "—"}</td>
        <td>${g.effective_week}</td>
      </tr>${gapLines ? `<tr><td colspan="5" class="muted">${gapLines}</td></tr>` : ""}`;
    }).join("") || `<tr><td colspan="5" class="muted">None yet</td></tr>`;
    el.querySelector(".win-body").innerHTML = `
      ${noticeHtml}
      <p>Open gate bids</p>
      <table class="grid"><thead><tr><th>Airport</th><th>Units</th><th>$/unit</th><th>Closes</th></tr></thead><tbody>${
        (bids.bids || []).map((b) => `<tr><td>${b.airport_iata}</td><td>${b.units_requested}</td><td>${money(b.price_per_unit)}</td><td>${b.closes_week}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">None</td></tr>`
      }</tbody></table>
      ${(bids.recent || []).length ? `<p>Recent closed gate bids</p>
      <table class="grid"><thead><tr><th>Airport</th><th>Units</th><th>$/unit</th><th>Status</th></tr></thead><tbody>${
        (bids.recent || []).slice(0, 12).map((b) => `<tr><td>${b.airport_iata}</td><td>${b.units_requested}</td><td>${money(b.price_per_unit)}</td><td>${escapeHtml(b.status || "")}</td></tr>`).join("")
      }</tbody></table>` : ""}
      <p>Open slot bids</p>
      <table class="grid"><thead><tr><th>Airport</th><th>Units</th><th>$/unit</th></tr></thead><tbody>${
        (slots.bids || []).map((b) => `<tr><td>${b.airport_iata}</td><td>${b.units_requested}</td><td>${money(b.price_per_unit)}</td></tr>`).join("") || `<tr><td colspan="3" class="muted">None</td></tr>`
      }</tbody></table>
      <p>Held stands</p>
      <table class="grid"><thead><tr><th>Airport</th><th>Units</th><th>Touches</th><th>Util</th><th>From wk</th></tr></thead><tbody>${utilRows}</tbody></table>`;
    const ack = $("#n-ack", el);
    if (ack) {
      ack.onclick = async () => {
        try {
          await api("/api/notifications/ack", {
            method: "POST",
            body: JSON.stringify({ ids: notices.map((n) => n.notification_id).filter(Boolean) }),
          });
          toast("Notices marked read");
          openBids();
        } catch (err) { toast(err.message); }
      };
    }
  }).catch((err) => { el.querySelector(".win-body").innerHTML = `<div class="err">${err.message}</div>`; });
}

function openSlots() {
  const el = openWindow("slots", "Runway slots", `<div class="muted">Loading…</div>`, { width: 560 });
  Promise.all([api("/api/slots"), api("/api/slots/bids").catch(() => ({ bids: [] }))]).then(([data, bids]) => {
    const floor = Number(data.min_price_per_unit || 8000);
    el.querySelector(".win-body").innerHTML = `<p class="muted">Week ${data.week} · hourly cap still binds everyone. Weekly quota is extra. Awards start next week.</p>
      <div class="row2">
        <div><label>Airport</label><input id="s-iata" placeholder="JFK" /></div>
        <div><label>Units</label><input id="s-units" type="number" value="2" min="1" /></div>
      </div>
      <label>$ / unit</label><input id="s-price" type="number" value="${floor}" min="1" />
      <button type="button" id="s-bid">Place slot bid</button>
      <p>Open auctions</p>
      <table class="grid"><thead><tr><th>Airport</th><th>Avail</th><th>$ now</th><th>Closes wk</th></tr></thead><tbody>${
        (data.auctions || []).map((a) => `<tr data-iata="${a.airport_iata}" data-price="${a.current_price_per_unit}" style="cursor:pointer">
          <td>${a.airport_iata}</td><td>${a.units_available}</td>
          <td>${money(a.current_price_per_unit)}</td><td>${a.closes_week}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">None this week</td></tr>`
      }</tbody></table>
      <p>Your quota</p>
      <table class="grid"><thead><tr><th>Airport</th><th>Cap/hr</th><th>Held</th><th>Used</th><th>Peak</th></tr></thead><tbody>${
        (data.airports || []).map((a) => `<tr><td>${a.iata}${a.full ? " FULL" : ""}</td><td>${a.cap}</td><td>${a.held ?? 0}</td><td>${a.used}</td><td>${a.peak_movements}/${a.cap} ${a.peak_label || ""}</td></tr>`).join("")
      }</tbody></table>
      <p>Your open bids</p>
      <table class="grid"><thead><tr><th>Airport</th><th>Units</th><th>$/unit</th></tr></thead><tbody>${
        (bids.bids || []).map((b) => `<tr><td>${b.airport_iata}</td><td>${b.units_requested}</td><td>${money(b.price_per_unit)}</td></tr>`).join("") || `<tr><td colspan="3" class="muted">None</td></tr>`
      }</tbody></table>`;
    attachAirportPicker($("#s-iata", el));
    el.querySelectorAll("tbody tr[data-iata]").forEach((tr) => {
      tr.onclick = () => {
        $("#s-iata", el).value = tr.dataset.iata;
        if (tr.dataset.price) $("#s-price", el).value = tr.dataset.price;
      };
    });
    $("#s-bid", el).onclick = async () => {
      try {
        await api("/api/slots/bid", {
          method: "POST",
          body: JSON.stringify({
            airport_iata: await resolveAirportCode($("#s-iata", el).value),
            units: Number($("#s-units", el).value),
            price_per_unit: Number($("#s-price", el).value),
          }),
        });
        toast("Slot bid recorded");
        openSlots();
      } catch (err) { toast(err.message); }
    };
  }).catch((err) => { el.querySelector(".win-body").innerHTML = `<div class="err">${err.message}</div>`; });
}

function openBank() {
  const el = openWindow("bank", "Bank", `<div class="muted">Loading…</div>`, { width: 620 });
  const render = (amount) => {
    const q = (amount != null && amount !== "") ? `?amount=${encodeURIComponent(amount)}` : "";
    api("/api/bank" + q).then((data) => {
      const preview = data.preview_amount != null ? data.preview_amount : 100000;
      const offerErr = data.offer_error
        ? `<p class="err">${escapeHtml(data.offer_error)}</p>`
        : "";
      const offers = data.offers || [];
      const active = (data.loans || []).filter((l) => l.status === "ACTIVE");
      el.querySelector(".win-body").innerHTML = `
        <p>Credit <b>${data.credit_score}</b> · ${escapeHtml(data.bracket_label || "")}</p>
        <p>Debt ${money(data.debt)} · room ${money(data.headroom)} · weekly ${money(data.weekly_service)}</p>
        <p class="muted">Cap ${money(data.borrowing_cap)} (5× weekly revenue, $500k floor). Min ticket $100k.</p>
        <div class="row2">
          <div><label>Amount</label><input id="bk-amt" type="number" min="100000" step="100000" value="${Number(preview)}" /></div>
          <div><label>&nbsp;</label><button type="button" id="bk-offers">Show offers</button></div>
        </div>
        ${offerErr}
        <p>Term choices</p>
        <table class="grid"><thead><tr><th>Weeks</th><th>APR</th><th>Weekly</th><th>Total</th><th></th></tr></thead><tbody>${
          offers.map((o) => `<tr>
            <td>${o.weeks}</td>
            <td>${(Number(o.annual_rate) * 100).toFixed(2)}%</td>
            <td>${money(o.weekly_payment)}</td>
            <td>${money(o.total_paid)}</td>
            <td><button type="button" data-originate="${o.weeks}">Take</button></td>
          </tr>`).join("") || `<tr><td colspan="5" class="muted">Enter an amount within your room.</td></tr>`
        }</tbody></table>
        <p>Active loans</p>
        <table class="grid"><thead><tr><th>Id</th><th>Remaining</th><th>Weekly</th><th>Weeks</th><th></th></tr></thead><tbody>${
          active.map((l) => `<tr>
            <td>${escapeHtml(String(l.loan_id || "").slice(0, 8))}</td>
            <td>${money(l.principal_remaining)}</td>
            <td>${money(l.weekly_payment)}</td>
            <td>${l.weeks_remaining}</td>
            <td><button type="button" data-payoff="${escapeHtml(l.loan_id)}">Pay off</button></td>
          </tr>`).join("") || `<tr><td colspan="5" class="muted">None</td></tr>`
        }</tbody></table>`;
      $("#bk-offers", el).onclick = () => render($("#bk-amt", el).value);
      el.querySelectorAll("[data-originate]").forEach((btn) => {
        btn.onclick = async () => {
          try {
            await api("/api/bank/originate", {
              method: "POST",
              body: JSON.stringify({
                amount: Number($("#bk-amt", el).value),
                weeks: Number(btn.dataset.originate),
              }),
            });
            toast("Loan funded");
            refreshHud();
            render($("#bk-amt", el).value);
          } catch (err) { toast(err.message); }
        };
      });
      el.querySelectorAll("[data-payoff]").forEach((btn) => {
        btn.onclick = async () => {
          try {
            await api("/api/bank/payoff", {
              method: "POST",
              body: JSON.stringify({ loan_id: btn.dataset.payoff }),
            });
            toast("Loan paid off");
            refreshHud();
            render($("#bk-amt", el).value);
          } catch (err) { toast(err.message); }
        };
      });
    }).catch((err) => { el.querySelector(".win-body").innerHTML = `<div class="err">${err.message}</div>`; });
  };
  render();
}

async function drainWeekSummaries() {
  const data = await api("/api/books/pending");
  const rows = data.summaries || [];
  rows.forEach((s) => {
    const wk = Number(s.game_week || 0);
    const net = Number(s.net_income || 0);
    toast(`Week ${wk} net ${signedMoney(net)} · open Books`);
  });
}

function booksMoneyRow(label, value, opts = {}) {
  const v = Number(value || 0);
  const bold = opts.bold ? "font-weight:700" : "";
  const neg = opts.signed && v < 0 ? " neg" : "";
  const shown = opts.signed ? signedMoney(v) : money(v);
  return `<div class="hud-tip-row" style="${bold}"><span>${escapeHtml(label)}</span><b class="${neg}">${shown}</b></div>`;
}

function openBooks() {
  const el = openWindow("books", "Books", `<div class="muted">Loading…</div>`, { width: 560 });
  const paint = () => {
    api("/api/books").then((data) => {
      const w = data.week || {};
      const f = data.fuel || {};
      const st = data.settlement || {};
      const rep = data.reputation || {};
      if (w.skipped) {
        el.querySelector(".win-body").innerHTML = `<p class="err">${escapeHtml(w.reason || "No airline")}</p>`;
        return;
      }
      const caught = st.catchup_settled || [];
      if (caught.length) {
        toast(caught.length === 1
          ? `Settled backlog: week ${caught[0]}`
          : `Settled backlog: weeks ${caught[0]}–${caught[caught.length - 1]}`);
        refreshHud();
      }
      const missing = st.missing_weeks || [];
      const settleLine = missing.length
        ? `<p class="err">Settlement backlog: weeks ${missing.join(", ")}</p>`
        : (st.last_settled_week
          ? `<p class="muted">Last settled week <b>${Number(st.last_settled_week)}</b> · calendar week ${Number(st.calendar_week || 0)}</p>`
          : `<p class="muted">No weeks settled yet · calendar week ${Number(st.calendar_week || 0)}</p>`);
      const otpPct = (Number(rep.on_time_rate || 0) * 100).toFixed(0);
      const dProj = Number(rep.projected_delta || 0);
      const repBlock = `
        <p><b>Reputation</b> ${Number(rep.reputation_score || 0).toFixed(0)} / 100
          · brand ${Number(rep.brand_power || 1).toFixed(2)}×</p>
        <p class="muted">This week OTP ${otpPct}% (${Number(rep.flights_counted || 0)} flights)
          · projected Δ ${dProj >= 0 ? "+" : ""}${dProj.toFixed(1)}
          ${Number(rep.aog_events || 0) ? ` · AOG ${Number(rep.aog_events)}` : ""}</p>`;
      const src = w.summary_source === "ledger" ? "settled" : "live / in-progress";
      const feesDetail = `
        <details class="books-fees">
          <summary class="muted">Fee breakdown</summary>
          <table class="grid"><tbody>
            <tr><td>Excise</td><td>${money(w.excise_tax)}</td></tr>
            <tr><td>Segment</td><td>${money(w.segment_fees)}</td></tr>
            <tr><td>Security</td><td>${money(w.security_fees)}</td></tr>
            <tr><td>PFC</td><td>${money(w.pfc_fees)}</td></tr>
            <tr><td>Landing</td><td>${money(w.landing_fees)}</td></tr>
            <tr><td>Gate</td><td>${money(w.gate_fees)}</td></tr>
          </tbody></table>
        </details>`;
      const prev = w.prev_week;
      const prevLine = prev
        ? `<p class="muted">Prior week — net ${money(prev.net_income)} · cash EOW ${money(prev.cash_end_of_week)} · rev ${money(prev.revenue_gross)}</p>`
        : "";
      const hedge = (f.hedged_bbl != null && f.hedge_weeks_remaining)
        ? `$${Number(f.hedged_bbl).toFixed(2)}/bbl · ${f.hedge_weeks_remaining} wk left`
        : "none";
      const shock = f.shock_pending
        ? `<p class="err">Fuel shock: ${escapeHtml(f.shock_message || "pending")} <button type="button" id="bk-ack">Ack</button></p>`
        : "";
      const netCls = Number(w.net_income || 0) < 0 ? "neg" : "";
      el.querySelector(".win-body").innerHTML = `
        ${settleLine}
        ${repBlock}
        <hr class="books-rule" />
        <p><b>Week ${Number(w.game_week || 0)}</b> · ${escapeHtml(src)}
          ${w.flights_count != null ? ` · ${Number(w.flights_count)} flights` : ""}</p>
        <div class="books-stack">
          ${booksMoneyRow("Revenue", w.revenue_gross)}
          ${booksMoneyRow("Taxes & fees", w.total_taxes_and_fees)}
          ${feesDetail}
          ${booksMoneyRow("Fuel", w.fuel_cost)}
          ${booksMoneyRow("Leases", w.lease_costs)}
          ${booksMoneyRow("Loans", w.loan_payments)}
          ${booksMoneyRow("Corp tax", w.corporate_tax)}
          <div class="hud-tip-row" style="font-weight:700;margin-top:4px;border-top:1px solid rgba(0,0,0,.08);padding-top:6px">
            <span>Net</span><b class="${netCls}">${signedMoney(w.net_income)}</b>
          </div>
          ${booksMoneyRow(w.cash_row_label || "Cash", w.cash_end_of_week)}
        </div>
        ${prevLine}
        <hr class="books-rule" />
        <p><b>Fuel</b> · spot <b>$${Number(f.spot_bbl || 0).toFixed(2)}/bbl</b>
          · all-in ~$${Number(f.spot_gal || 0).toFixed(3)}/gal</p>
        <p class="muted">Spark ${escapeHtml(f.sparkline || "—")} · est burn ${Number(f.est_weekly_burn_gal || 0).toLocaleString()} gal/wk</p>
        <p>Hedge: ${escapeHtml(hedge)}</p>
        <p>Reserve: ${Number(f.reserve_gal || 0).toLocaleString()} gal
          ${f.reserve_avg_price != null ? `· avg $${Number(f.reserve_avg_price).toFixed(3)}/gal` : ""}</p>
        ${shock}
        <p class="muted">Hedge premiums ~ 2wk ${money(f.premium_2wk)} · 4wk ${money(f.premium_4wk)} · 8wk ${money(f.premium_8wk)}</p>
        <div class="books-actions">
          <button type="button" data-hedge="2">Hedge 2</button>
          <button type="button" data-hedge="4">Hedge 4</button>
          <button type="button" data-hedge="8">Hedge 8</button>
          <button type="button" id="bk-cancel-hedge">Cancel hedge</button>
        </div>
        <div class="row2" style="margin-top:8px">
          <div><label>Buy reserve (gal)</label><input id="bk-res-gal" type="number" min="1" step="1000" value="10000" /></div>
          <div><label>&nbsp;</label><button type="button" id="bk-buy-res">Buy reserve</button></div>
        </div>
        <div class="row2">
          <div><label>Dip alert ($/bbl)</label><input id="bk-dip" type="number" min="1" step="1"
            value="${f.dip_alert_bbl != null ? Number(f.dip_alert_bbl) : ""}" placeholder="off" /></div>
          <div><label>&nbsp;</label>
            <button type="button" id="bk-dip-set">Set</button>
            <button type="button" id="bk-dip-clear">Clear</button>
          </div>
        </div>
        <p class="muted" style="margin-top:10px">Debt desk is under <b>Bank</b>.</p>`;

      const runFuel = async (body, okMsg) => {
        try {
          await api("/api/fuel", { method: "POST", body: JSON.stringify(body) });
          if (okMsg) toast(okMsg);
          refreshHud();
          paint();
        } catch (err) { toast(err.message); }
      };
      el.querySelectorAll("[data-hedge]").forEach((btn) => {
        btn.onclick = () => runFuel({ action: "hedge", weeks: Number(btn.dataset.hedge) }, `Hedge ${btn.dataset.hedge}wk`);
      });
      const cancelBtn = $("#bk-cancel-hedge", el);
      if (cancelBtn) cancelBtn.onclick = () => runFuel({ action: "cancel_hedge" }, "Hedge cancelled");
      const buyRes = $("#bk-buy-res", el);
      if (buyRes) {
        buyRes.onclick = () => runFuel(
          { action: "buy_reserve", gallons: Number($("#bk-res-gal", el).value) },
          "Reserve updated"
        );
      }
      const dipSet = $("#bk-dip-set", el);
      if (dipSet) {
        dipSet.onclick = () => runFuel(
          { action: "set_dip", price: Number($("#bk-dip", el).value) },
          "Dip alert set"
        );
      }
      const dipClear = $("#bk-dip-clear", el);
      if (dipClear) dipClear.onclick = () => runFuel({ action: "clear_dip" }, "Dip alert cleared");
      const ack = $("#bk-ack", el);
      if (ack) ack.onclick = () => runFuel({ action: "ack_shock" }, "Shock acknowledged");
    }).catch((err) => {
      el.querySelector(".win-body").innerHTML = `<div class="err">${escapeHtml(err.message)}</div>`;
    });
  };
  paint();
}

function openCompetitors() {
  const existing = document.getElementById("win-competitors");
  if (existing) existing.remove();
  const el = openWindow("competitors", "Competitors", `
    <div class="win-tabs">
      <button type="button" data-ctab="airlines" class="active">Airlines</button>
      <button type="button" data-ctab="routes">Routes</button>
      <button type="button" data-ctab="contested">Contested</button>
    </div>
    <div id="co-out" class="muted">Loading…</div>
  `, { width: 780 });
  let airlines = [];
  let filterId = "";
  const out = $("#co-out", el);
  const tabs = el.querySelectorAll("[data-ctab]");
  const setTab = (id) => {
    tabs.forEach((b) => b.classList.toggle("active", b.dataset.ctab === id));
  };
  const showAirlines = () => {
    setTab("airlines");
    if (!airlines.length) {
      out.innerHTML = `<p class="muted">No competitors seeded yet.</p>`;
      return;
    }
    out.innerHTML = `<p class="muted">Click an airline to see its routes. Revenue is a weekly estimate from their network.</p>
      <table class="grid"><thead><tr>
        <th>Airline</th><th>Hub</th><th>Routes</th><th>Est. weekly rev</th><th>Route profit</th><th>Fleet</th><th>Rep</th>
      </tr></thead><tbody>${
        airlines.map((c) => `<tr data-cid="${escapeHtml(c.competitor_id)}" style="cursor:pointer">
          <td>${escapeHtml(c.name)}<div class="muted">${escapeHtml(c.competitor_id)}</div></td>
          <td>${escapeHtml(c.home_hub_iata || "")}</td>
          <td>${c.routes_count}</td>
          <td>${money(c.weekly_revenue)}</td>
          <td>${money(c.route_profit_est)}</td>
          <td>${c.fleet_size}</td>
          <td>${Number(c.reputation || 0).toFixed(0)}</td>
        </tr>`).join("")
      }</tbody></table>`;
    out.querySelectorAll("tr[data-cid]").forEach((tr) => {
      tr.onclick = () => {
        filterId = tr.dataset.cid;
        showRoutes();
      };
    });
  };
  const showRoutes = async () => {
    setTab("routes");
    out.innerHTML = `<p class="muted">Loading routes…</p>`;
    try {
      const q = filterId ? "?id=" + encodeURIComponent(filterId) : "";
      const data = await api("/api/competitors/routes" + q);
      const rows = data.routes || [];
      const title = filterId
        ? `Routes for ${escapeHtml(filterId)}`
        : "All competitor routes";
      out.innerHTML = `<p>${title} ${filterId ? `<button type="button" id="co-all">Show all</button>` : ""}</p>
        ${!rows.length ? `<p class="muted">None.</p>` : `<table class="grid"><thead><tr>
          <th>Airline</th><th>Pair</th><th>J / Y</th><th>Freq</th><th>Rev avg</th><th>Profit est</th><th>Share</th>
        </tr></thead><tbody>${
          rows.map((r) => `<tr>
            <td>${escapeHtml(r.name || r.competitor_id)}</td>
            <td>${escapeHtml(r.route_pair_id || "")}${r.contested ? ` <span class="muted">vs you</span>` : ""}</td>
            <td>${money(r.fare_business)} / ${money(r.fare_leisure)}</td>
            <td>${r.frequency_per_week}/wk</td>
            <td>${money(r.actual_weekly_revenue_avg)}</td>
            <td>${money(r.estimated_weekly_profit)}</td>
            <td>${(Number(r.market_share || 0) * 100).toFixed(0)}%</td>
          </tr>`).join("")
        }</tbody></table>`}`;
      const allBtn = $("#co-all", el);
      if (allBtn) allBtn.onclick = () => { filterId = ""; showRoutes(); };
    } catch (err) { out.innerHTML = `<div class="err">${err.message}</div>`; }
  };
  const showContested = async () => {
    setTab("contested");
    out.innerHTML = `<p class="muted">Loading contested markets…</p>`;
    try {
      const data = await api("/api/competitors/contested");
      const rows = data.markets || [];
      if (!rows.length) {
        out.innerHTML = `<p class="muted">No overlap yet between your network and AI routes.</p>`;
        return;
      }
      out.innerHTML = `<p class="muted">Week ${data.week} · your fares vs AI on shared city pairs.</p>
        <table class="grid"><thead><tr>
          <th>Route</th><th>Your J / Y</th><th>Your share</th><th>AI</th>
        </tr></thead><tbody>${
          rows.map((m) => `<tr>
            <td>${escapeHtml(m.route_id)}</td>
            <td>${money(m.player_fare_business)} / ${money(m.player_fare_leisure)}</td>
            <td>${(Number(m.player_share_business) * 100).toFixed(0)}% J · ${(Number(m.player_share_leisure) * 100).toFixed(0)}% Y</td>
            <td>${(m.competitors || []).map((c) =>
              `${escapeHtml(c.competitor_id)} ${money(c.fare_business)}/${money(c.fare_leisure)} (${(Number(c.share_business) * 100).toFixed(0)}%/${(Number(c.share_leisure) * 100).toFixed(0)}%)`
            ).join("<br>") || "—"}</td>
          </tr>`).join("")
        }</tbody></table>`;
    } catch (err) { out.innerHTML = `<div class="err">${err.message}</div>`; }
  };
  el.querySelector("[data-ctab=airlines]").onclick = showAirlines;
  el.querySelector("[data-ctab=routes]").onclick = () => showRoutes();
  el.querySelector("[data-ctab=contested]").onclick = showContested;
  api("/api/competitors").then((data) => {
    airlines = data.competitors || [];
    showAirlines();
  }).catch((err) => { out.innerHTML = `<div class="err">${err.message}</div>`; });
}

function boardStatusClass(status) {
  const s = String(status || "").toUpperCase();
  if (s === "IN_AIR") return "st-inair";
  if (s === "LANDED") return "st-landed";
  if (s === "DELAYED" || s === "HOLDING") return "st-delay";
  if (s === "CANCELLED") return "st-cancel";
  return "st-sched";
}

function boardRowHtml(r) {
  const mine = String(r.operator_id || "").toUpperCase() === "PLAYER";
  return `<tr class="${mine ? "board-mine" : ""}">
    <td>${escapeHtml(r.time_label || "")}</td>
    <td>${escapeHtml(r.direction || "")}</td>
    <td>${escapeHtml(r.flight_number || "")}</td>
    <td>${escapeHtml(r.operator_label || r.operator_id || "")}</td>
    <td>${escapeHtml(r.route || "")}</td>
    <td>${escapeHtml(r.tail_number || "—")}</td>
    <td><span class="board-st ${boardStatusClass(r.status)}">${escapeHtml(r.status || "")}</span></td>
    <td>${escapeHtml(r.pax_label || "—")}</td>
  </tr>`;
}

function boardTableHtml(rows, emptyMsg) {
  if (!rows || !rows.length) {
    return `<p class="muted">${escapeHtml(emptyMsg || "No flights.")}</p>`;
  }
  return `<table class="grid board-grid"><thead><tr>
    <th>Time</th><th></th><th>Flight</th><th>Airline</th><th>Route</th><th>Tail</th><th>Status</th><th>Pax</th>
  </tr></thead><tbody>${rows.map(boardRowHtml).join("")}</tbody></table>`;
}

function openBoard() {
  const existing = document.getElementById("win-board");
  if (existing) existing.remove();
  const el = openWindow("board", "Airport flight board", `<div class="muted">Loading…</div>`, { width: 780 });
  let airportTab = "all";
  let mainTab = "airport";
  let currentAirport = "";

  const renderAirportPanel = (data) => {
    const ap = data.airport_iata || currentAirport;
    const title = [data.airport_name, data.airport_city].filter(Boolean).join(" · ");
    let rows = data.flights || [];
    if (airportTab === "dep") rows = data.departures || [];
    if (airportTab === "arr") rows = data.arrivals || [];
    const empty = airportTab === "dep"
      ? "No departures this week."
      : airportTab === "arr"
        ? "No arrivals this week."
        : "No flights through this airport this week.";
    return `
      <p><b>${escapeHtml(ap)}</b>${title ? ` · ${escapeHtml(title)}` : ""}</p>
      <p class="muted">Week ${Number(data.week || 1)} · player + AI · times are local clock within the week</p>
      <div class="win-tabs board-subtabs">
        <button type="button" data-abtab="all" class="${airportTab === "all" ? "active" : ""}">All</button>
        <button type="button" data-abtab="dep" class="${airportTab === "dep" ? "active" : ""}">Departures</button>
        <button type="button" data-abtab="arr" class="${airportTab === "arr" ? "active" : ""}">Arrivals</button>
      </div>
      <div id="board-ap-out">${boardTableHtml(rows, empty)}</div>`;
  };

  const wireAirportSubtabs = () => {
    el.querySelectorAll("[data-abtab]").forEach((b) => {
      b.onclick = () => {
        airportTab = b.dataset.abtab;
        loadAirport(currentAirport, false);
      };
    });
  };

  const loadAirport = async (iata, showLoading = true) => {
    currentAirport = String(iata || "").trim().toUpperCase();
    if (!currentAirport) return;
    const panel = $("#board-airport", el);
    if (showLoading && panel) panel.innerHTML = `<p class="muted">Loading ${escapeHtml(currentAirport)}…</p>`;
    try {
      const code = await resolveAirportCode(currentAirport);
      currentAirport = code;
      const data = await api("/api/board/airport?iata=" + encodeURIComponent(code));
      if (panel) {
        panel.innerHTML = renderAirportPanel(data);
        wireAirportSubtabs();
      }
      const inp = $("#board-ap", el);
      if (inp) inp.value = code;
    } catch (err) {
      if (panel) panel.innerHTML = `<div class="err">${escapeHtml(err.message)}</div>`;
    }
  };

  const loadNetwork = async () => {
    const panel = $("#board-network", el);
    if (!panel) return;
    panel.innerHTML = `<p class="muted">Loading…</p>`;
    try {
      const data = await api("/api/board");
      panel.innerHTML = `
        <p class="muted">Week ${data.week} · your active legs (sample)</p>
        <table class="grid"><thead><tr><th>Flt</th><th>Tail</th><th>Route</th><th>Status</th></tr></thead><tbody>${
          (data.player || []).map((f) => `<tr class="board-mine"><td>${escapeHtml(f.flight_number || "")}</td><td>${escapeHtml(f.tail_number || "")}</td><td>${escapeHtml(f.origin_iata)}-${escapeHtml(f.dest_iata)}</td><td>${escapeHtml(f.status)}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">None</td></tr>`
        }</tbody></table>
        <p class="muted">AI (sample)</p>
        <table class="grid"><tbody>${
          (data.ai || []).slice(0, 25).map((f) => `<tr><td>${escapeHtml(f.flight_number || "")}</td><td>${escapeHtml(f.competitor_id)}</td><td>${escapeHtml(f.origin_iata)}-${escapeHtml(f.dest_iata)}</td><td>${escapeHtml(f.status)}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">None</td></tr>`
        }</tbody></table>`;
    } catch (err) {
      panel.innerHTML = `<div class="err">${escapeHtml(err.message)}</div>`;
    }
  };

  const showMainTab = (id) => {
    mainTab = id;
    el.querySelectorAll("[data-btab]").forEach((b) => b.classList.toggle("active", b.dataset.btab === id));
    const apPanel = $("#board-airport-wrap", el);
    const netPanel = $("#board-network-wrap", el);
    if (apPanel) apPanel.hidden = id !== "airport";
    if (netPanel) netPanel.hidden = id !== "network";
    if (id === "network") loadNetwork();
    else if (currentAirport) loadAirport(currentAirport, false);
  };

  const mount = (hub, picks) => {
    const pickBtns = [...picks].slice(0, 8).map((c) =>
      `<button type="button" class="board-pick" data-ap="${escapeHtml(c)}">${escapeHtml(c)}</button>`
    ).join("");
    el.querySelector(".win-body").innerHTML = `
      <div class="win-tabs">
        <button type="button" data-btab="airport" class="active">By airport</button>
        <button type="button" data-btab="network">Your network</button>
      </div>
      <div id="board-airport-wrap">
        <form id="board-ap-form" class="board-ap-form">
          <label>Airport</label>
          <div class="row2">
            <input id="board-ap" placeholder="ATL or Atlanta" value="${escapeHtml(hub || "")}" />
            <button type="submit">Show board</button>
          </div>
        </form>
        ${pickBtns ? `<p class="board-picks">${pickBtns}</p>` : ""}
        <div id="board-airport" class="muted">${hub ? `Loading ${escapeHtml(hub)}…` : "Pick an airport."}</div>
      </div>
      <div id="board-network-wrap" hidden><div id="board-network"></div></div>`;
    attachAirportPicker($("#board-ap", el));
    el.querySelectorAll("[data-btab]").forEach((b) => {
      b.onclick = () => showMainTab(b.dataset.btab);
    });
    el.querySelectorAll(".board-pick").forEach((b) => {
      b.onclick = () => loadAirport(b.dataset.ap);
    });
    $("#board-ap-form", el).onsubmit = (e) => {
      e.preventDefault();
      loadAirport($("#board-ap", el).value);
    };
    if (hub) loadAirport(hub);
  };

  Promise.all([
    api("/api/state").catch(() => ({})),
    api("/api/routes").catch(() => ({ routes: [] })),
  ]).then(([state, routes]) => {
    const hub = state.airline && state.airline.home_hub_iata;
    const picks = new Set();
    if (hub) picks.add(String(hub).toUpperCase());
    (routes.routes || []).forEach((r) => {
      if (r.origin_iata) picks.add(String(r.origin_iata).toUpperCase());
      if (r.dest_iata) picks.add(String(r.dest_iata).toUpperCase());
    });
    mount(hub, picks);
  }).catch((err) => {
    mount("", new Set());
    setMsg(el.querySelector(".win-body"), err.message, false);
  });
}

const openers = {
  airline: openAirline,
  catalog: openCatalog,
  fleet: openFleet,
  routes: openRoutes,
  routedetail: openRouteDetail,
  schedule: openSchedule,
  tailgrid: openTailGrid,
  gates: openGates,
  bids: openBids,
  slots: openSlots,
  bank: openBank,
  books: openBooks,
  competitors: openCompetitors,
  board: openBoard,
};

function tailColor(t) {
  let h = 0;
  for (let i = 0; i < t.length; i++) h = ((h << 5) - h) + t.charCodeAt(i) | 0;
  return "hsl(" + (Math.abs(h) % 300) + " 65% 42%)";
}

function interpolateGreatCircle(lat1, lon1, lat2, lon2, numPoints = 80) {
  const toRad = (d) => d * Math.PI / 180;
  const toDeg = (r) => r * 180 / Math.PI;
  lat1 = toRad(lat1); lon1 = toRad(lon1);
  lat2 = toRad(lat2); lon2 = toRad(lon2);
  const d = 2 * Math.asin(Math.sqrt(
    Math.sin((lat2 - lat1) / 2) ** 2 +
    Math.cos(lat1) * Math.cos(lat2) * Math.sin((lon2 - lon1) / 2) ** 2
  ));
  const points = [];
  if (!isFinite(d) || d === 0) return [[toDeg(lat1), toDeg(lon1)], [toDeg(lat2), toDeg(lon2)]];
  for (let i = 0; i <= numPoints; i++) {
    const f = i / numPoints;
    const A = Math.sin((1 - f) * d) / Math.sin(d);
    const B = Math.sin(f * d) / Math.sin(d);
    const x = A * Math.cos(lat1) * Math.cos(lon1) + B * Math.cos(lat2) * Math.cos(lon2);
    const y = A * Math.cos(lat1) * Math.sin(lon1) + B * Math.cos(lat2) * Math.sin(lon2);
    const z = A * Math.sin(lat1) + B * Math.sin(lat2);
    points.push([toDeg(Math.atan2(z, Math.sqrt(x * x + y * y))), toDeg(Math.atan2(y, x))]);
  }
  return unwrapLngPath(points);
}

/** Keep successive longitudes continuous so Pacific arcs don't jump ±360° mid-path. */
function unwrapLngPath(points) {
  if (!points || points.length < 2) return points || [];
  const out = [[points[0][0], points[0][1]]];
  for (let i = 1; i < points.length; i++) {
    let lat = points[i][0];
    let lon = points[i][1];
    const prev = out[i - 1][1];
    while (lon - prev > 180) lon -= 360;
    while (lon - prev < -180) lon += 360;
    out.push([lat, lon]);
  }
  return out;
}

function flightDotLabelHtml(seg, p) {
  const lines = [];
  const flightNo = String(seg.flight_number || "").trim();
  if (flightNo) lines.push("<b>" + escapeHtml(flightNo) + "</b>");
  const tail = String(seg.tail_number || "").trim();
  if (tail) lines.push(escapeHtml(tail));
  const origin = String(seg.origin_iata || "").trim();
  const dest = String(seg.dest_iata || "").trim();
  const route = origin && dest ? origin + "-" + dest : (origin || dest);
  if (route) lines.push(escapeHtml(route) + (seg.is_ferry ? " (ferry)" : ""));
  const typeId = String(seg.type_id || seg.aircraft_type || "").trim();
  if (typeId) lines.push(escapeHtml(typeId));
  const status = String(seg.status || "").trim();
  if (status) lines.push(escapeHtml(status));
  const flying = p > 0.002 && p < 0.998;
  lines.push(escapeHtml(flying ? ((p * 100).toFixed(0) + "% en route") : (p >= 0.998 ? "arrived" : "at origin")));
  return lines.join("<br>");
}

function airportPopupHtml(ap) {
  const iata = String(ap.iata || "").trim().toUpperCase();
  const icao = String(ap.icao || "").trim().toUpperCase();
  const name = String(ap.name || "").trim();
  const city = String(ap.city || "").trim();
  const country = String(ap.country || "").trim();
  const tz = String(ap.timezone || "").trim();
  const cat = String(ap.category || "").replace(/_/g, " ");
  const runway = ap.runway_length_ft != null ? Number(ap.runway_length_ft).toLocaleString() + " ft" : "—";
  const gates = ap.gate_count != null ? Number(ap.gate_count).toLocaleString() : "—";
  const score = ap.score != null ? Number(ap.score).toLocaleString() : "—";
  const place = [city, country].filter(Boolean).join(", ");
  return `
    <div class="ap-popup">
      <div class="ap-popup-title">${escapeHtml(name || iata)}</div>
      <div class="ap-popup-codes">
        <span>${escapeHtml(iata || "—")}</span>
        ${icao ? `<span class="ap-popup-icao">${escapeHtml(icao)}</span>` : ""}
      </div>
      ${place ? `<div class="ap-popup-place">${escapeHtml(place)}</div>` : ""}
      <table class="ap-popup-table">
        <tr><th>Runway</th><td>${escapeHtml(runway)}</td></tr>
        <tr><th>Gates</th><td>${escapeHtml(gates)}</td></tr>
        <tr><th>Timezone</th><td>${escapeHtml(tz || "—")}</td></tr>
        <tr><th>Category</th><td>${escapeHtml(cat || "—")}</td></tr>
        <tr><th>Score</th><td>${escapeHtml(score)}</td></tr>
      </table>
      <button type="button" class="ap-new-route-btn" data-origin="${escapeHtml(iata)}">New route</button>
    </div>
  `;
}

function airportLatLon(iata) {
  const code = String(iata || "").toUpperCase();
  const pin = airportPins[code];
  if (pin && pin.ap && pin.ap.lat != null && pin.ap.lon != null) {
    return { lat: Number(pin.ap.lat), lon: Number(pin.ap.lon) };
  }
  const ap = (airportCatalog || []).find((a) => a.iata === code);
  if (ap) return { lat: Number(ap.lat), lon: Number(ap.lon) };
  return null;
}

/** Animate the map onto the player's home hub after founding (or on demand). */
async function focusMapOnHub(iata, opts = {}) {
  const code = String(iata || "").toUpperCase();
  if (!code) return;
  try { initMap(); } catch (_) { return; }
  if (!mapInst) return;
  try {
    await ensureAirportCatalog();
  } catch (_) { /* still try pin / catalog below */ }
  const ll = airportLatLon(code);
  if (!ll || !Number.isFinite(ll.lat) || !Number.isFinite(ll.lon)) return;
  // Keep the hub pin on-screen at this zoom, then fly in.
  refreshAirportPins();
  const zoom = opts.zoom != null ? Number(opts.zoom) : 6;
  const duration = opts.duration != null ? Number(opts.duration) : 1.35;
  didFitFlights = true;
  mapInst.flyTo([ll.lat, ll.lon], zoom, {
    animate: true,
    duration: Math.max(0.4, duration),
    easeLinearity: 0.25,
  });
}

function clearRouteDraft() {
  routeDraft = null;
  if (airportDraftLayer) airportDraftLayer.clearLayers();
  if (mapInst && mapInst.getContainer()) {
    mapInst.getContainer().classList.remove("ap-picking-dest");
  }
}

function clearAirportRouteSelection() {
  selectedAirportIata = "";
  if (airportRouteLayer) airportRouteLayer.clearLayers();
  Object.values(flights).forEach((f) => {
    if (!f || !f.line) return;
    const flying = liveProgress(f) > 0.002 && liveProgress(f) < 0.998;
    f.line.setStyle({ opacity: flying ? (f.player ? 0.95 : 0.55) : (f.player ? 0.45 : 0.2) });
  });
}

function startNewRouteDraft(originIata) {
  const origin = String(originIata || "").toUpperCase();
  if (!origin || !airportLatLon(origin)) {
    toast("Airport location unavailable");
    return;
  }
  clearAirportRouteSelection();
  clearRouteDraft();
  routeDraft = { origin, dest: null, phase: "pick_dest" };
  if (mapInst) {
    mapInst.closePopup();
    if (mapInst.getContainer()) mapInst.getContainer().classList.add("ap-picking-dest");
  }
  toast("Click a destination airport");
}

/** Compact demand block — same numbers as Routes → Preview. */
function routeDraftStatsInner(preview) {
  if (preview == null) {
    return `<div class="muted">Loading demand…</div>`;
  }
  if (preview.__error) {
    return `<div class="err">${escapeHtml(preview.__error)}</div>`;
  }
  const dem = preview.demand || {};
  const market = Number(dem.weekly_market_total != null ? dem.weekly_market_total : dem.total_pax || 0);
  const src = dem.demand_source_badge || "Demand";
  const floorNote = dem.market_floor_applied ? " · min market" : "";
  const already = preview.already_operated
    ? `<div class="ap-open-route-note">You already operate this</div>`
    : "";
  return `
    <div class="ap-open-route-demand"><b>${market.toLocaleString()}</b> pax/wk
      <span class="muted">· ${escapeHtml(src)}${escapeHtml(floorNote)}</span></div>
    <div class="muted">${Number(dem.business_pax || 0).toLocaleString()} business ·
      ${Number(dem.leisure_pax || 0).toLocaleString()} leisure</div>
    <div class="muted">Y${Number(dem.economy_pax || 0)} / W${Number(dem.premium_economy_pax || 0)} /
      J${Number(dem.business_cabin_pax || 0)} / F${Number(dem.first_pax || 0)}</div>
    <div class="muted">${Number(preview.distance_nm || 0).toLocaleString()} nm ·
      Cost ~ <b>${money(preview.total_new_cost)}</b></div>
    ${already}`;
}

function openRouteBoxHtml(origin, dest, preview) {
  return `
    <div class="ap-open-route-box">
      <div class="ap-open-route-pair">${escapeHtml(origin)} → ${escapeHtml(dest)}</div>
      <div class="ap-open-route-stats">${routeDraftStatsInner(preview)}</div>
      <button type="button" class="ap-open-route-btn">Open route</button>
      <button type="button" class="ap-open-route-cancel" aria-label="Cancel">×</button>
    </div>`;
}

function drawRouteDraftPreview() {
  if (!airportDraftLayer || !mapInst || !routeDraft || !routeDraft.dest) return;
  airportDraftLayer.clearLayers();
  const o = airportLatLon(routeDraft.origin);
  const d = airportLatLon(routeDraft.dest);
  if (!o || !d) return;

  const endpoints = [[o.lat, o.lon], [d.lat, d.lon]];
  const pts = interpolateGreatCircle(o.lat, o.lon, d.lat, d.lon, 64);
  // Soft underlay first so the pulse stroke sits on top.
  const underOpts = {
    color: "#0ea5e9",
    weight: 8,
    opacity: 0.22,
    pane: "routes",
    steps: 5,
    wrap: false,
    interactive: false,
    className: "ap-draft-line-glow",
  };
  if (typeof L.Geodesic === "function") {
    new L.Geodesic(endpoints, underOpts).addTo(airportDraftLayer);
  } else {
    L.polyline(pts, underOpts).addTo(airportDraftLayer);
  }
  const lineOpts = {
    color: "#38bdf8",
    weight: 3.5,
    opacity: 0.95,
    pane: "routes",
    steps: 5,
    wrap: false,
    interactive: false,
    className: "ap-draft-line",
  };
  if (typeof L.Geodesic === "function") {
    new L.Geodesic(endpoints, lineOpts).addTo(airportDraftLayer);
  } else {
    L.polyline(pts, lineOpts).addTo(airportDraftLayer);
  }

  const origin = routeDraft.origin;
  const dest = routeDraft.dest;
  const marker = L.marker([d.lat, d.lon], {
    icon: L.divIcon({
      className: "ap-open-route-anchor",
      html: openRouteBoxHtml(origin, dest, null),
      iconSize: [196, 150],
      iconAnchor: [98, 158],
    }),
    interactive: true,
    keyboard: false,
    zIndexOffset: 800,
  }).addTo(airportDraftLayer);

  const wireOpenRouteBox = () => {
    const el = marker.getElement();
    if (!el || el.dataset.wired === "1") return;
    el.dataset.wired = "1";
    L.DomEvent.disableClickPropagation(el);
    L.DomEvent.disableScrollPropagation(el);
    const openBtn = el.querySelector(".ap-open-route-btn");
    const cancelBtn = el.querySelector(".ap-open-route-cancel");
    if (openBtn) {
      openBtn.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        confirmOpenDraftRoute();
      });
    }
    if (cancelBtn) {
      cancelBtn.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        clearRouteDraft();
      });
    }
  };
  marker.on("add", wireOpenRouteBox);
  requestAnimationFrame(wireOpenRouteBox);

  const seq = ++routeDraftPreviewSeq;
  routeDraft.previewSeq = seq;
  api(`/api/routes/preview?origin=${encodeURIComponent(origin)}&dest=${encodeURIComponent(dest)}`)
    .then((p) => {
      if (!routeDraft || routeDraft.previewSeq !== seq) return;
      const el = marker.getElement();
      const slot = el && el.querySelector(".ap-open-route-stats");
      if (slot) slot.innerHTML = routeDraftStatsInner(p);
    })
    .catch((err) => {
      if (!routeDraft || routeDraft.previewSeq !== seq) return;
      const el = marker.getElement();
      const slot = el && el.querySelector(".ap-open-route-stats");
      if (slot) {
        slot.innerHTML = routeDraftStatsInner({ __error: err.message || "Demand unavailable" });
      }
    });
}

async function confirmOpenDraftRoute() {
  if (!routeDraft || !routeDraft.origin || !routeDraft.dest) return;
  const origin = routeDraft.origin;
  const dest = routeDraft.dest;
  try {
    await api("/api/routes", {
      method: "POST",
      body: JSON.stringify({ origin, dest }),
    });
    toast(`Opened ${origin}–${dest}`);
    clearRouteDraft();
    await refreshHud();
    // Refresh network coloring + show spokes from the new origin.
    try {
      const data = await api("/api/flight-map.json");
      updateAirports(data);
    } catch (_) { /* ignore */ }
    if (airportPins[origin]) {
      selectAirportRoutes(origin, airportPins[origin].marker);
    }
  } catch (err) {
    toast(err.message || "Could not open route");
  }
}

function pickDraftDestination(destIata) {
  if (!routeDraft || routeDraft.phase !== "pick_dest") return false;
  const dest = String(destIata || "").toUpperCase();
  if (!dest || dest === routeDraft.origin) {
    toast("Pick a different airport");
    return true;
  }
  if (!airportLatLon(dest)) {
    toast("Destination location unavailable");
    return true;
  }
  routeDraft.dest = dest;
  routeDraft.phase = "confirm";
  if (mapInst && mapInst.getContainer()) {
    mapInst.getContainer().classList.remove("ap-picking-dest");
  }
  if (mapInst) mapInst.closePopup();
  drawRouteDraftPreview();
  return true;
}

function airportSpokeStyle(hasPlayer, hasAi, suspendedOnly) {
  if (hasPlayer) {
    return {
      color: "#2563eb",
      weight: hasAi ? 3.5 : 3,
      opacity: 0.95,
      dashArray: null,
    };
  }
  if (suspendedOnly) {
    return { color: "#94a3b8", weight: 1.5, opacity: 0.55, dashArray: "4 6" };
  }
  return { color: "#f59e0b", weight: 2.25, opacity: 0.85, dashArray: null };
}

function drawAirportRouteSpokes(hubIata, routes) {
  if (!airportRouteLayer || !mapInst) return;
  airportRouteLayer.clearLayers();
  const hub = String(hubIata || "").toUpperCase();
  const byOther = {};
  (routes || []).forEach((r) => {
    const other = String(r.other_iata || "").toUpperCase();
    if (!other || other === hub) return;
    if (!byOther[other]) {
      byOther[other] = {
        other,
        hasPlayer: false,
        hasAi: false,
        suspendedOnly: true,
        lat: null,
        lon: null,
        labels: [],
      };
    }
    const bucket = byOther[other];
    const op = String(r.operator || "");
    if (op === "PLAYER") bucket.hasPlayer = true;
    else bucket.hasAi = true;
    if (String(r.status || "").toUpperCase() !== "SUSPENDED") bucket.suspendedOnly = false;
    // Endpoint coords: the non-hub end of this directed leg.
    if (String(r.origin_iata || "").toUpperCase() === hub) {
      bucket.lat = Number(r.dest_lat);
      bucket.lon = Number(r.dest_lon);
    } else {
      bucket.lat = Number(r.origin_lat);
      bucket.lon = Number(r.origin_lon);
    }
    const label = String(r.operator_label || op);
    if (label && bucket.labels.indexOf(label) < 0) bucket.labels.push(label);
  });

  const hubPin = airportPins[hub];
  const hubLat = hubPin && hubPin.ap ? Number(hubPin.ap.lat) : null;
  const hubLon = hubPin && hubPin.ap ? Number(hubPin.ap.lon) : null;
  if (hubLat == null || hubLon == null) return;

  // Soften background flight lines while a network is selected.
  Object.values(flights).forEach((f) => {
    if (f && f.line) f.line.setStyle({ opacity: f.player ? 0.18 : 0.08 });
  });

  Object.values(byOther).forEach((spoke) => {
    if (spoke.lat == null || spoke.lon == null || !Number.isFinite(spoke.lat) || !Number.isFinite(spoke.lon)) {
      return;
    }
    const style = airportSpokeStyle(spoke.hasPlayer, spoke.hasAi, spoke.suspendedOnly && !spoke.hasPlayer);
    const endpoints = [[hubLat, hubLon], [spoke.lat, spoke.lon]];
    const lineOpts = {
      ...style,
      pane: "routes",
      steps: 5,
      wrap: false,
      className: "ap-route-spoke",
    };
    const pts = interpolateGreatCircle(hubLat, hubLon, spoke.lat, spoke.lon, 64);
    const line = (typeof L.Geodesic === "function")
      ? new L.Geodesic(endpoints, lineOpts).addTo(airportRouteLayer)
      : L.polyline(pts, lineOpts).addTo(airportRouteLayer);
    const tip = `${hub}–${spoke.other}`;
    line.bindTooltip(tip, { sticky: true, direction: "top", className: "ap-pin-tip" });
  });
}

async function selectAirportRoutes(iata, marker) {
  const code = String(iata || "").toUpperCase();
  if (!code || !marker) return;
  if (routeDraft && routeDraft.phase === "pick_dest") {
    pickDraftDestination(code);
    return;
  }
  if (routeDraft && routeDraft.phase === "confirm") {
    // Starting a fresh inspection cancels an unfinished draft.
    clearRouteDraft();
  }
  if (selectedAirportIata === code) {
    clearAirportRouteSelection();
    return;
  }
  selectedAirportIata = code;
  const seq = ++selectedAirportRoutesSeq;
  try {
    const data = await api("/api/airports/routes?iata=" + encodeURIComponent(code));
    if (seq !== selectedAirportRoutesSeq || selectedAirportIata !== code) return;
    drawAirportRouteSpokes(code, data.routes || []);
  } catch (err) {
    if (seq !== selectedAirportRoutesSeq || selectedAirportIata !== code) return;
    clearAirportRouteSelection();
    toast(err.message || "Failed to load routes");
  }
}

function ensureAirportCatalog() {
  if (airportCatalog) return Promise.resolve(airportCatalog);
  if (!airportCatalogPromise) {
    airportCatalogPromise = api("/api/airports-map.json")
      .then((data) => {
        const list = Array.isArray(data.airports) ? data.airports : [];
        airportCatalog = list
          .filter((ap) => ap && ap.iata != null && ap.lat != null && ap.lon != null)
          .map((ap) => ({
            iata: String(ap.iata).toUpperCase(),
            icao: ap.icao,
            name: ap.name,
            city: ap.city,
            country: ap.country,
            lat: Number(ap.lat),
            lon: Number(ap.lon),
            runway_length_ft: ap.runway_length_ft,
            gate_count: ap.gate_count,
            timezone: ap.timezone,
            score: Number(ap.score || 0),
            category: String(ap.category || ""),
          }))
          .sort((a, b) => b.score - a.score || a.iata.localeCompare(b.iata));
        return airportCatalog;
      })
      .catch((err) => {
        airportCatalogPromise = null;
        throw err;
      });
  }
  return airportCatalogPromise;
}

/** Zoom → size floor + geographic separation so metro clusters keep the biggest hub. */
function airportZoomPolicy(zoom) {
  const z = Number(zoom) || 0;
  if (z <= 2) return { minScore: 1450000, minSepKm: 900, cats: ["large_airport"] };
  if (z <= 3) return { minScore: 1200000, minSepKm: 520, cats: ["large_airport"] };
  if (z <= 4) return { minScore: 1000000, minSepKm: 300, cats: ["large_airport"] };
  if (z <= 5) return { minScore: 850000, minSepKm: 180, cats: ["large_airport"] };
  if (z <= 6) return { minScore: 600000, minSepKm: 110, cats: ["large_airport", "medium_airport"] };
  if (z <= 7) return { minScore: 350000, minSepKm: 70, cats: ["large_airport", "medium_airport"] };
  if (z <= 8) return { minScore: 150000, minSepKm: 28, cats: null };
  if (z <= 9) return { minScore: 50000, minSepKm: 16, cats: null };
  return { minScore: 0, minSepKm: 8, cats: null };
}

function airportPinStyle(iata) {
  const onNetwork = airportNetwork.has(iata);
  const isHub = airportHub && iata === airportHub;
  if (isHub) {
    return { radius: 7, color: "#5b21b6", weight: 2, fillColor: "#8b5cf6", fillOpacity: 0.95 };
  }
  if (onNetwork) {
    return { radius: 6, color: "#1e40af", weight: 1.5, fillColor: "#3b82f6", fillOpacity: 0.95 };
  }
  return { radius: 4.5, color: "#6b7280", weight: 1.1, fillColor: "#9ca3af", fillOpacity: 0.92 };
}

/** Approx great-circle distance in km (good enough for metro declutter). */
function airportSepKm(a, b) {
  const toRad = Math.PI / 180;
  const dLat = (b.lat - a.lat) * toRad;
  const dLon = (b.lon - a.lon) * toRad;
  const lat1 = a.lat * toRad;
  const lat2 = b.lat * toRad;
  const h = Math.sin(dLat / 2) ** 2
    + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
  return 12742 * Math.asin(Math.min(1, Math.sqrt(h)));
}

function selectAirportsForZoom(catalog, zoom) {
  const policy = airportZoomPolicy(zoom);
  // Fully zoomed in: show the whole catalog (canvas renderer handles the load).
  if (policy.minScore <= 0 && policy.minSepKm <= 8) {
    return catalog;
  }
  const accepted = [];

  function tooClose(ap) {
    for (let i = 0; i < accepted.length; i++) {
      if (airportSepKm(ap, accepted[i]) < policy.minSepKm) return true;
    }
    return false;
  }

  function tryAccept(ap, force) {
    if (!force && tooClose(ap)) return false;
    accepted.push(ap);
    return true;
  }

  // Network airports always win (even if clustered), then fill with largest others.
  const network = [];
  const others = [];
  for (let i = 0; i < catalog.length; i++) {
    const ap = catalog[i];
    if (airportNetwork.has(ap.iata)) network.push(ap);
    else others.push(ap);
  }
  network.sort((a, b) => b.score - a.score || a.iata.localeCompare(b.iata));
  network.forEach((ap) => tryAccept(ap, true));

  for (let i = 0; i < others.length; i++) {
    const ap = others[i];
    if (ap.score < policy.minScore) continue;
    if (policy.cats && policy.cats.indexOf(ap.category) < 0) continue;
    tryAccept(ap, false);
  }
  return accepted;
}

function syncAirportPin(ap) {
  const iata = ap.iata;
  const style = airportPinStyle(iata);
  if (!airportPins[iata]) {
    const marker = L.circleMarker([ap.lat, ap.lon], {
      ...style,
      pane: "airports",
      renderer: airportRenderer || undefined,
      className: "ap-pin",
    }).addTo(airportLayer);
    marker.bindTooltip(iata, {
      permanent: false,
      direction: "top",
      offset: [0, -6],
      className: "ap-pin-tip",
    });
    marker.bindPopup(() => airportPopupHtml(airportPins[iata].ap), {
      maxWidth: 280,
      className: "ap-pin-popup",
      autoPan: true,
    });
    marker.on("popupopen", () => {
      if (routeDraft && routeDraft.phase === "pick_dest") {
        marker.closePopup();
        return;
      }
      const root = marker.getPopup() && marker.getPopup().getElement();
      const btn = root && root.querySelector(".ap-new-route-btn");
      if (!btn) return;
      btn.onclick = (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        startNewRouteDraft(btn.getAttribute("data-origin") || iata);
      };
    });
    marker.on("click", (ev) => {
      if (ev && ev.originalEvent) L.DomEvent.stopPropagation(ev.originalEvent);
      if (routeDraft && routeDraft.phase === "pick_dest") {
        pickDraftDestination(iata);
        marker.closePopup();
        return;
      }
      selectAirportRoutes(iata, marker);
    });
    marker.on("popupclose", () => {
      if (routeDraft) return;
      if (selectedAirportIata === iata) clearAirportRouteSelection();
    });
    airportPins[iata] = { marker, ap };
  } else {
    airportPins[iata].ap = ap;
    airportPins[iata].marker.setLatLng([ap.lat, ap.lon]);
    airportPins[iata].marker.setStyle(style);
    if (!airportLayer.hasLayer(airportPins[iata].marker)) {
      airportLayer.addLayer(airportPins[iata].marker);
    }
  }
}

function refreshAirportPins() {
  if (!airportLayer || !mapInst || !airportCatalog) return;
  const visible = selectAirportsForZoom(airportCatalog, mapInst.getZoom());
  const seen = {};
  visible.forEach((ap) => {
    seen[ap.iata] = true;
    syncAirportPin(ap);
  });
  Object.keys(airportPins).forEach((iata) => {
    if (!seen[iata]) {
      airportLayer.removeLayer(airportPins[iata].marker);
      delete airportPins[iata];
    }
  });
}

function scheduleAirportPinRefresh() {
  if (airportPinRefreshTimer) clearTimeout(airportPinRefreshTimer);
  airportPinRefreshTimer = setTimeout(() => {
    airportPinRefreshTimer = null;
    refreshAirportPins();
  }, 80);
}

function updateAirports(data) {
  airportHub = String((data.airline && data.airline.home_hub_iata) || "").toUpperCase();
  const net = new Set();
  if (airportHub) net.add(airportHub);
  (data.network_iatas || []).forEach((code) => {
    const c = String(code || "").toUpperCase();
    if (c) net.add(c);
  });
  airportNetwork = net;
  ensureAirportCatalog()
    .then(() => refreshAirportPins())
    .catch((err) => toast(err.message || "Airport map failed to load"));
}

function updateFlights(data) {
  const seen = {};
  const bounds = [];
  (data.segments || []).forEach((seg) => {
    if (seg.origin_lat == null || seg.dest_lat == null) return;
    const id = String(seg.segment_id || (seg.tail_number + "_" + seg.route_id));
    seen[id] = true;
    const dep = Number(seg.scheduled_dep_game_hour);
    const arr = Number(seg.scheduled_arr_game_hour);
    if (!flights[id]) {
      const route = interpolateGreatCircle(seg.origin_lat, seg.origin_lon, seg.dest_lat, seg.dest_lon);
      const color = tailColor(String(seg.tail_number || id));
      const player = seg.operator === "PLAYER";
      const lineOpts = {
        color,
        weight: player ? 3.5 : 1.5,
        opacity: player ? 0.9 : 0.35,
        pane: "routes",
        steps: 5,
        // false = one continuous Pacific arc (shifted lng). true splits at ±180 into two segments.
        wrap: false,
      };
      const endpoints = [
        [Number(seg.origin_lat), Number(seg.origin_lon)],
        [Number(seg.dest_lat), Number(seg.dest_lon)],
      ];
      const line = (typeof L.Geodesic === "function")
        ? new L.Geodesic(endpoints, lineOpts).addTo(lineLayer)
        : L.polyline(route, lineOpts).addTo(lineLayer);
      const marker = L.circleMarker(route[0], {
        radius: player ? 8 : 5,
        color: "#111", weight: 1.5, fillColor: color, fillOpacity: 1, pane: "aircraft",
      }).addTo(acLayer);
      flights[id] = { route, marker, line, dep, arr, player, seg };
      const popupHtml = () => {
        const p = liveProgress(flights[id]);
        return flightDotLabelHtml(flights[id].seg, p);
      };
      marker.bindTooltip(popupHtml(), { sticky: true, direction: "top" });
      marker.on("mouseover", () => { marker.setTooltipContent(popupHtml()); });
      marker.bindPopup(popupHtml);
      line.bindPopup(popupHtml);
    } else {
      flights[id].dep = dep;
      flights[id].arr = arr;
      flights[id].seg = seg;
    }
    try {
      const lb = flights[id].line.getBounds && flights[id].line.getBounds();
      if (lb && lb.isValid && lb.isValid()) {
        bounds.push(lb.getSouthWest(), lb.getNorthEast());
      } else {
        flights[id].route.forEach((pt) => bounds.push(pt));
      }
    } catch (_) {
      flights[id].route.forEach((pt) => bounds.push(pt));
    }
  });
  Object.keys(flights).forEach((id) => {
    if (!seen[id]) {
      mapInst.removeLayer(flights[id].marker);
      mapInst.removeLayer(flights[id].line);
      delete flights[id];
    }
  });
  if (!didFitFlights && bounds.length) {
    mapInst.fitBounds(bounds, { padding: [80, 80], maxZoom: 6 });
    didFitFlights = true;
  }
}

function liveProgress(f) {
  if (!f) return 0;
  const d = Number(f.dep);
  let a = Number(f.arr);
  if (!(a > d + 1e-6)) a = d + 1;
  return Math.max(0, Math.min(1, (liveGameHour() - d) / (a - d)));
}

function pointAlong(route, t) {
  const max = route.length - 1;
  if (max <= 0) return route[0];
  const x = Math.max(0, Math.min(1, t)) * max;
  const i = Math.min(Math.floor(x), max - 1);
  const frac = x - i;
  const a = route[i];
  const b = route[i + 1];
  return [a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac];
}

function animate() {
  Object.values(flights).forEach((f) => {
    const p = liveProgress(f);
    const pos = pointAlong(f.route, p);
    if (pos) f.marker.setLatLng(pos);
    const flying = p > 0.002 && p < 0.998;
    f.marker.setStyle({ fillOpacity: flying || f.player ? 1 : 0.45, radius: f.player ? (flying ? 9 : 7) : 5 });
    if (selectedAirportIata) {
      f.line.setStyle({ opacity: f.player ? 0.18 : 0.08 });
    } else {
      f.line.setStyle({ opacity: flying ? (f.player ? 0.95 : 0.55) : (f.player ? 0.45 : 0.2) });
    }
  });
  requestAnimationFrame(animate);
}

function initMap() {
  if (mapInst) return;
  if (typeof L === "undefined") {
    toast("Map library failed to load.");
    return;
  }
  if (typeof L.Geodesic === "undefined") {
    toast("Geodesic plugin failed to load — Pacific routes may draw the long way.");
  }
  mapInst = L.map("map", { worldCopyJump: true, zoomControl: false });
  mapInst.createPane("airports");
  mapInst.getPane("airports").style.zIndex = 405;
  mapInst.createPane("routes");
  mapInst.getPane("routes").style.zIndex = 410;
  mapInst.createPane("aircraft");
  mapInst.getPane("aircraft").style.zIndex = 640;
  L.control.zoom({ position: "bottomright" }).addTo(mapInst);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    noWrap: false,
    attribution: "&copy; OpenStreetMap",
  }).addTo(mapInst);
  airportRenderer = L.canvas({ pane: "airports" });
  airportLayer = L.layerGroup().addTo(mapInst);
  lineLayer = L.layerGroup().addTo(mapInst);
  airportRouteLayer = L.layerGroup().addTo(mapInst);
  airportDraftLayer = L.layerGroup().addTo(mapInst);
  acLayer = L.layerGroup().addTo(mapInst);
  mapInst.setView([20, 10], 3);
  mapInst.on("zoomend", scheduleAirportPinRefresh);
  mapInst.on("click", () => {
    if (routeDraft && routeDraft.phase === "pick_dest") {
      clearRouteDraft();
      toast("New route cancelled");
      return;
    }
    if (selectedAirportIata) clearAirportRouteSelection();
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && routeDraft) {
      clearRouteDraft();
      toast("New route cancelled");
    }
  });
  requestAnimationFrame(() => mapInst.invalidateSize());
  animate();
}

async function loadMap() {
  try { initMap(); } catch (err) { toast(err.message); return; }
  if (!mapInst) return;
  const seq = ++mapLoadSeq;
  try {
    const data = await api("/api/flight-map.json");
    if (seq !== mapLoadSeq) return;
    syncMapClock(data);
    updateAirports(data);
    updateFlights(data);
  } catch (err) {
    // Don't let the HUD keep racing while the sim/API is stuck.
    freezeMapClock();
    throw err;
  }
}

function mapPollIntervalMs() {
  const sp = mapClock.speed || 0;
  if (sp >= 60) return 250;
  if (sp >= 20) return 400;
  if (sp >= 4) return 800;
  return 1500;
}

function scheduleMapPoll() {
  if (mapPollTimer) clearInterval(mapPollTimer);
  mapPollTimer = setInterval(() => { loadMap().catch(() => {}); }, mapPollIntervalMs());
}

$("#dock").addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const fn = openers[btn.dataset.win];
  if (fn) fn();
});

function markHudSpeed(speed) {
  const n = Number(speed);
  $("#hud-speeds").querySelectorAll("button").forEach((b) => {
    b.classList.toggle("active", Number(b.dataset.speed) === n);
  });
}

$("#hud-speeds").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const speed = Number(btn.dataset.speed);
  hudSeq += 1;
  applyClockSpeed(speed);
  markHudSpeed(speed);
  paintHudTime();
  try {
    const out = await api("/api/clock", { method: "POST", body: JSON.stringify({ speed }) });
    if (out.clock) {
      lastState = lastState || {};
      lastState.clock = out.clock;
      $("#hud-time").textContent = out.clock.time_display || $("#hud-time").textContent;
      markHudSpeed(out.clock.speed_multiplier);
      syncMapClock(out.clock);
      paintHudTime();
    }
    await refreshHud();
    loadMap().catch(() => {});
  } catch (err) {
    toast(err.message);
    refreshHud().catch(() => {});
  }
});

try { initMap(); } catch (err) { toast(err.message); }
refreshHud().catch((err) => toast(err.message));
loadMap().catch((err) => toast(err.message));
scheduleMapPoll();
setInterval(() => { refreshHud().catch(() => {}); }, 4000);
setInterval(paintHudTime, 250);
