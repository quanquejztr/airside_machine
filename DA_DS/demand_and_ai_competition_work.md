# DA/DS Workstreams: Demand Modeling & AI Competition

Practical scope for a data analyst / data scientist working on **Airside Machine** — what already exists, what to build, and what “done” looks like.

---

## Current state (what they inherit)

| Area | Built | Hand-tuned / weak |
|------|--------|-------------------|
| **Demand** | BTS/gravity/anchor pipeline, logit competition, seasonality, noise | Elasticities (−0.3/−1.2), `passenger_demand_multiplier` (9.0), biz/lei multipliers, seasonality CSV |
| **Validation** | 17-route benchmark (`Intl_Route/check_routes.py`), ~450 lines of tests | No MAPE dashboard, no regional holdout, no automated recalibration CI |
| **AI competition** | Route scoring, logit share, P&L simulation, weekly turn | Score weights (demand/profit/competition/network) are heuristic; limited balance validation |

### Key files

```
# Demand
engine/demand.py
engine/route_demand.py
engine/route_suggestions.py
US_Route/bts_calibrate.py
Intl_Route/fit_gravity_bands.py
Intl_Route/merge_anchors.py
Intl_Route/intl_analyze.py
Intl_Route/check_routes.py
Intl_Route/backfill_airports.py
data/bts_demand_anchors.csv
data/gravity_bands.json
data/financial_constants.csv
data/seasonality.csv
tests/test_bts_calibrate.py

# AI competition
engine/ai.py
engine/ai_economics.py
engine/ai_flights.py
engine/ai_gates.py
```

---

## Workstream 1: Demand modeling

### Phase A — Measure & baseline (2–3 weeks)

**Goal:** Know where the model is wrong before changing it.

| Task | Deliverable |
|------|-------------|
| Expand benchmark set | 100+ routes stratified by region, distance band, anchor vs gravity-only |
| Build evaluation report | MAPE / median ratio by: US, Japan, Korea, Europe, SEA, thin vs trunk |
| Residual analysis | Where gravity under/over-shoots (score bias, distance band) — extend `Intl_Route/intl_analyze.py` |
| Player-facing validation | Compare `preview_weekly_demand_before_open` vs `compute_demand` vs route panel (known mismatch in `GAME_REVIEW.md`) |
| Coverage map | % of airport pairs with real anchors vs model-only; prioritize gaps (China, India, SEA) |

**Outputs:** notebook or script + weekly regression report + prioritized fix list.

---

### Phase B — Improve existing models (4–6 weeks)

**Goal:** Better fit without rewriting the game engine.

| Task | Model work? | Details |
|------|-------------|---------|
| **Refit gravity bands** | Yes | Re-run `Intl_Route/fit_gravity_bands.py`; tune α, β, quantile blend |
| **Anchor merge / precedence** | Partly | Validate `Intl_Route/merge_anchors.py` rules; add new sources |
| **Elasticity estimation** | Yes | Fit business/leisure ε from fare sensitivity or cross-route variation; replace hard-coded 0.3 / 1.2 in `engine/demand.py` |
| **Seasonality model** | Yes | Derive monthly multipliers from BTS/Eurostat/Japan instead of static `data/seasonality.csv` |
| **Airport score model** | Yes | Replace peer heuristics in `Intl_Route/backfill_airports.py` with traffic-based scores |
| **Thin-route floor tuning** | Partly | Calibrate `bts_min_weekly_pool`, floors so thin routes are flyable without inflating trunks |
| **New data pipelines** | ETL | Fetch + calibrate missing regions (China domestic, India DGCA, etc.) |

**Outputs:** updated `data/gravity_bands.json`, `data/bts_demand_anchors.csv`, new constants in `financial_constants.csv`, tests passing in `tests/test_bts_calibrate.py`.

---

### Phase C — Productionize (2–3 weeks)

| Task | Deliverable |
|------|-------------|
| Recalibration pipeline | `fetch → calibrate → fit → merge → test` as one command |
| Regression gates | CI fails if benchmark median ratio drifts > X% |
| Model documentation | What each constant does, how to re-fit, data lineage |
| Determinism fixes | Replace `hash()` with stable seeds (`GAME_REVIEW.md` #17) — DS should spec, eng implements |

---

## Workstream 2: AI competition

### Phase A — Validate AI behavior (2–3 weeks)

**Goal:** Check if AI decisions match the demand model and feel fair.

| Task | Deliverable |
|------|-------------|
| **AI vs player symmetry audit** | Does AI use same demand pool as player? (`ai_economics._live_route_bases` vs `compute_demand`) |
| **Route choice analysis** | Which routes AI opens; are they profitable in simulation vs actual `simulated_*` fields? |
| **Logit share validation** | When player enters a route, does share split match logit math? |
| **Competitor balance report** | Per-AI: routes flown, revenue, load factor, hub focus vs strategy (BUDGET/HUBSPOKE/PREMIUM) |
| **Player experience sim** | Monte Carlo: “If I open MNL-FUK, which AI responds and at what fare?” |

**Key code:** `engine/ai.py` (`ai_score_candidate`), `engine/ai_economics.py` (`weekly_pair_pnl`, `segment_shares`), `competitor_routes` table.

---

### Phase B — Improve AI decision models (4–6 weeks)

**Goal:** Smarter, more balanced AI — not just random route spam.

| Task | Model work? | Details |
|------|-------------|---------|
| **Route scoring model** | Yes | Today: weighted heuristics (demand/1600, profit sigmoid, competition penalties). Fit weights from simulated outcomes or expert labels |
| **Fare response model** | Yes | `_adjust_fares_light()` — when should AI undercut vs hold? Currently rule-based |
| **Entry/exit model** | Yes | When to open/suspend routes based on profit, capacity, player overlap |
| **Capacity allocation** | Partly | Fleet hours, gate costs, lease payback — optimize frequency per route |
| **Strategy personas** | Partly | Calibrate BUDGET vs PREMIUM vs HUBSPOKE so they behave distinctly |

**Outputs:** updated scoring function, tuned constants, balance report showing AI doesn’t dominate thin routes or ignore trunks.

---

### Phase C — Closed-loop with demand (ongoing)

| Task | Why |
|------|-----|
| Re-score AI candidates after demand model changes | AI uses `_live_route_bases` — demand refit changes which routes AI picks |
| Simulate full game weeks | 50–100 weeks: player opens hub, AI reacts, check market concentration |
| A/B parameter sweeps | e.g. logit reputation coefficient (0.02 today), competition penalty (0.25 per AI) |

---

## Overlap (both workstreams)

Demand changes **directly affect** AI. After any demand refit, AI must be re-validated.

```
Real traffic data
       ↓
  Demand model ──→ Player demand UI
       ↓
  AI route scoring
       ↓
  AI opens routes
       ↓
  Logit market share ──→ Revenue split (player vs AI)
       ↓
  Settlement P&L ──→ Validate AI was rational ──→ tune scoring
```

---

## Concrete deliverables (what “done” looks like)

| # | Deliverable | Owner |
|---|-------------|-------|
| 1 | Demand backtest dashboard (100+ routes, by region) | Demand DS |
| 2 | Refit gravity + updated anchor files | Demand DS |
| 3 | Estimated elasticities + seasonality | Demand DS |
| 4 | Recalibration runbook + CI gates | Demand DS + eng |
| 5 | AI behavior audit report | AI DS |
| 6 | Tuned route scoring / fare response | AI DS |
| 7 | 100-week simulation balance report | Both |
| 8 | PRs into `engine/demand.py`, `route_demand.py`, `ai.py`, `ai_economics.py` | DS specs, eng reviews |

---

## Engineering dependencies

The DA/DS needs:

- Ability to run scripts, read SQLite, run tests
- **Not** full game UI work — but API hooks help (e.g. export `competitor_routes` + `week_ledger` for analysis)
- Clear acceptance criteria, e.g.:
  - Anchored routes median ratio 0.85–1.15
  - No AI earns >40% of trunk revenue

---

## Rough timeline (one DA/DS)

| Month | Focus |
|-------|--------|
| **1** | Demand baseline + AI audit |
| **2** | Demand refit (gravity, elasticity, seasonality) |
| **3** | AI scoring/fare tuning + closed-loop sim |
| **4+** | New data sources, marketing models (Phase 13), ongoing monitoring |

---

## Model-building vs analytics-only

| Project | Model-building? |
|---------|-----------------|
| Gravity / anchor calibration | **Yes** |
| Elasticity & seasonality estimation | **Yes** |
| Airport score model | **Yes** |
| AI route scoring & fare response | **Yes** |
| Marketing lift (Phase 13, unbuilt) | **Yes** |
| Route revenue Monte Carlo | **Yes** |
| Backtesting dashboard | Mostly **no** — evaluates existing models |
| Profitability scorecard | Mostly **no** — joins existing outputs |
| SQL KPI reports | **No** — classic DA |

**Split:** ~50% modeling, ~30% measurement/validation, ~20% engineering glue (pipelines, tests, constants).

---

## Related game phases

| Phase | Relevance |
|-------|-----------|
| **2 — Demand & pricing** | Core econometric model |
| **4 — Settlement** | P&L validation for AI rationality |
| **6 — KPI / analytics** | In-game metrics; CLI-first today |
| **10 — AI & slots** | Logit share, competitor simulation |
| **13 — Marketing** (unbuilt) | Natural extension: campaign lift, attribution |

See `docs/phase_implementation_detail.md` for full phase list.
