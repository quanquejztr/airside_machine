# DA/DS Workstreams: Demand Modeling & AI Competition

Detailed scope for **Airside Machine** — role split, how the systems work today, exact tasks, files, metrics, and acceptance criteria.

**Repo:** `airline_sim game`  
**Game:** single-player airline tycoon; weekly demand → scheduling → settlement → AI competitors.

---

## Table of contents

1. [Role definitions: DA vs DS](#1-role-definitions-da-vs-ds)
2. [How demand works today (read this first)](#2-how-demand-works-today-read-this-first)
3. [How AI competition works today](#3-how-ai-competition-works-today)
4. [Known gaps, bugs, and regional holes](#4-known-gaps-bugs-and-regional-holes)
5. [DA work — specific tasks](#5-da-work--specific-tasks)
6. [DS work — specific tasks](#6-ds-work--specific-tasks)
7. [Regional expansion: VN, CN, Middle East](#7-regional-expansion-vn-cn-middle-east)
8. [SQL & scripts cheat sheet](#8-sql--scripts-cheat-sheet)
9. [Deliverables checklist](#9-deliverables-checklist)
10. [90-day plan](#10-90-day-plan)
11. [File index](#11-file-index)

---

## 1. Role definitions: DA vs DS

### Data Analyst (DA)

**Primary question:** *Is the current model right, where is it wrong, and what should we change?*

| Does | Does not (usually) |
|------|---------------------|
| Run benchmarks and backtests | Refit gravity coefficients in production without review |
| Build reports, dashboards, coverage maps | Rewrite `engine/demand.py` end-to-end |
| Write SQL against `airline_sim.db` | Own game UI / scheduling code |
| Recommend constant changes with evidence | Add new AI competitors without game-design sign-off |
| Define acceptance criteria for model changes | |

**Typical outputs:** CSV/Excel reports, Jupyter notebooks, weekly regression summary, prioritized fix list, SQL views.

**Success metric example:** “We can say with data whether MNL-SGN demand is credible and whether AI should fly it.”

---

### Data Scientist (DS)

**Primary question:** *How do we build or improve the models that drive demand and AI decisions?*

| Does | Does not (usually) |
|------|---------------------|
| Refit gravity, elasticity, seasonality | Only run ad-hoc SQL with no model artifact |
| Build ETL for new anchor sources | Replace entire game loop |
| Tune AI scoring weights / propose new formulas | Design gate auction UI |
| Ship PRs to `engine/route_demand.py`, `engine/ai.py` | |

**Typical outputs:** Updated `data/gravity_bands.json`, `data/bts_demand_anchors.csv`, fitted parameters in `financial_constants.csv`, new tests in `tests/test_bts_calibrate.py`.

**Success metric example:** “Anchored routes median predicted/real ratio stays in 0.85–1.15 after refit; `pytest tests/test_bts_calibrate.py` passes.”

---

### Combined hire (DA/DS)

One person often does both. Use this doc as the full backlog; tag each task **DA**, **DS**, or **Both**.

| Activity | DA | DS |
|----------|:--:|:--:|
| Expand `check_routes.py` benchmark set | ✓ | |
| Compute MAPE by region | ✓ | |
| Refit `fit_gravity_bands.py` | | ✓ |
| Estimate price elasticities | | ✓ |
| Audit AI route distribution by country | ✓ | |
| Retune `ai_score_candidate` weights | spec | ✓ |
| Add VN/CN anchor ETL | | ✓ |
| Recommend new `competitors.json` hubs | ✓ | |
| 100-week simulation balance report | ✓ | ✓ |

---

## 2. How demand works today (read this first)

### 2.1 Pipeline (per route, per week)

```
compute_base_demand()          engine/route_demand.py
    │
    ├─ 1. BTS anchor lookup     db.bts_demand_anchors (50k+ rows)
    │      → if found: Mode B scale to game pool
    ├─ 2. GRAVITY               gravity_weekly() + quantile mapping + cap
    ├─ 3. LEGACY                category × distance buckets (fallback)
    │
    ▼
compute_demand()               engine/demand.py
    ├─ seasonality (data/seasonality.csv)
    ├─ price_factor()            ε_biz=0.3, ε_lei=1.2 (hand-set)
    ├─ brand / reputation
    ├─ passenger_demand_multiplier (= 9.0)
    ├─ demand_business_segment_multiplier (= 2.0)
    ├─ demand_leisure_segment_multiplier (= 3.5)
    ├─ weekly noise              uniform 0.9–1.1, seeded per route/week
    └─ compute_logit_shares_for_route()  if AI/player on same route
```

### 2.2 Base demand lookup order

Documented in `engine/route_demand.py` header:

1. **BTS** — directional weekly market from `bts_demand_anchors`
2. **GRAVITY** — `k × (score_o × score_d)^α / distance_decay × regional_prior × quantile_map`
3. **LEGACY** — old category × distance buckets

**Mode B calibration:**  
`base_total = anchor_weekly × (bts_target_market_share / effective_multiplier)`  
Default `bts_target_market_share = 0.90` → game pool ≈ 90% of published market.

### 2.3 Key hand-tuned constants (`data/financial_constants.csv`)

| Key | Default | What it does | Who should tune |
|-----|---------|--------------|-----------------|
| `passenger_demand_multiplier` | 9.0 | Scales all pax after base demand | DS + DA validate |
| `demand_business_segment_multiplier` | 2.0 | Business pax scale | DS |
| `demand_leisure_segment_multiplier` | 3.5 | Leisure pax scale | DS |
| `bts_target_market_share` | 0.90 | Anchor → game pool | DS |
| `bts_min_weekly_pool` | 750 | Floor for thin BTS/GRAVITY routes | DS |
| `bts_gravity_k` | 7.15e-6 | Gravity scale | DS (via fit) |
| `bts_gravity_alpha` | 0.8134 | Score exponent | DS |
| `bts_gravity_beta_short/medium/long` | 0.56 / 0.73 / 0.89 | Distance decay by band | DS |
| `bts_gravity_quantile_blend` | (in JSON) | How much quantile correction | DS |
| `gate_auction_score_threshold` | 1080000 | Airports requiring gate bids | Game design |

### 2.4 Price elasticity (hand-set, not fitted)

`engine/demand.py` → `price_factor()`:

- Business: `(base_fare / fare) ^ 0.3`
- Leisure: `(base_fare / fare) ^ 1.2`
- Clamped 0.05–2.0; extra penalty above 4× reference fare

Logit competition (`_logit_utility`): same ε values; utility = `−ε·log(fare) + 0.02·reputation`.

**DA task:** measure whether fare changes in-game match these elasticities.  
**DS task:** estimate ε from data or fare sweeps and propose replacements.

### 2.5 Anchor data sources (merged into `data/bts_demand_anchors.csv`)

| Source | Folder | Precedence |
|--------|--------|------------|
| US domestic BTS | `US_Route/` | Highest |
| US intl DOT T-100 | `Intl_Route/` | High |
| Korea | `Korea_Route/fetch_korea.py` | |
| Japan | `Japan_Route/fetch_japan.py` | |
| Europe Eurostat | `Europe_Route/fetch_eurostat.py` | |
| Australia BITRE | `Australia_Route/fetch_australia.py` | Lower |

Merge logic: `Intl_Route/merge_anchors.py`.

### 2.6 Validation tools that exist today

| Tool | Command | What it checks |
|------|---------|----------------|
| Route spot check | `python3 Intl_Route/check_routes.py` | 17 trunk routes vs published weekly pax |
| Intl analysis | `python3 Intl_Route/intl_analyze.py` | Score bias, distance decay, plausibility caps |
| Unit tests | `pytest tests/test_bts_calibrate.py` | Anchors, gravity continuity, quantile mapping |
| Popular destinations API | `GET /api/routes/suggestions?origin=MNL` | Uses `preview_weekly_demand_before_open` |

### 2.7 Known player-facing demand bugs (from `GAME_REVIEW.md`)

| Issue | Location | DA/DS action |
|-------|----------|--------------|
| Route panel uses even split per leg; flights use fill-up accounting | `scheduling.py` vs `analytics.py` | DA quantify gap; eng fix |
| `random.seed()` is global — affects AI bids | `demand.py:203, 370` | DS spec fix: use `random.Random(seed)` |
| `hash(route_id)` in noise seed — non-deterministic across restarts | `demand.py` | DS spec: use `zlib.crc32` |
| Reading route panel writes `competitor_share_this_week` to DB | `demand.py:577` | DA note side effects in analysis |

---

## 3. How AI competition works today

### 3.1 Weekly loop

```
ai_weekly_turn(game_week)                    engine/ai.py
    For each competitor:
        ai_light_pass()     every week
            → fare adjustments, suspend losers, open queued routes
        ai_full_evaluation()  every N weeks (ai_evaluation_interval_weeks=2)
            → ai_generate_candidates()
            → ai_score_candidate() per pair
            → gate bids, queue opens
            → _open_queued_candidates()
        spawn_ai_segments_for_week()         engine/ai_flights.py
```

### 3.2 Candidate generation (`ai_generate_candidates`)

Inputs: competitor `home_hub_iata`, `strategy`.

| Strategy | How pairs are built | Radius |
|----------|---------------------|--------|
| HUBSPOKE, PREMIUM | Hub → every allowed airport | 2500 nm (PREMIUM: 4200 nm) |
| BUDGET | Cart product of allowed airports (max 60 by score) | min(radius, 1500 nm); pair distance < 1500 nm |
| POINTTOPOINT | Same as BUDGET | |

Filters:

- PREMIUM: destination `score >= 600_000` and `category = large_airport`
- BUDGET: keep top 60 airports by score within radius
- Exclude pairs already in `competitor_routes` (ACTIVE/SUSPENDED)
- Prefer mix: ~2/3 gated (high-score foreign airports), ~1/3 ungated (`gate_score_threshold`)

**Important:** Candidates are ranked by **airport score sum**, not by demand preview. Demand enters only at scoring stage.

### 3.3 Route scoring (`ai_score_candidate`)

| Component | Formula / rule | Weight |
|-----------|----------------|--------|
| Demand | `min(1, (base_b + base_l) × 2 / 1600)` from `_live_route_bases` | **0.30** |
| Profit | sigmoid of `(est_profit - ai_min_profit_to_open)`; default floor **$8,000/wk** | **0.35** |
| Network fit | hub bonus, strategy match, connection bonus | **0.20** |
| Competition | penalize other AI; soften if player has 1–3 frequencies | **0.10** |
| Feasibility | cash, fleet headroom, block hours | **0.05** |

Decision threshold: `score >= 0.50 + (1 - risk_tolerance) × 0.20`.

P&L estimate: `weekly_pair_pnl()` in `engine/ai_economics.py` — uses same demand bases + logit share + fees + lease.

### 3.4 Key AI constants (`financial_constants.csv`)

| Key | Default | Meaning |
|-----|---------|---------|
| `ai_min_profit_to_open` | 8000 | Weekly profit floor ($) |
| `ai_hub_radius_nm` | 2500 | Candidate search radius |
| `ai_candidate_pool_size` | 15 | Max pairs evaluated per full eval |
| `ai_evaluation_interval_weeks` | 2 | Weeks between full evals |
| `ai_exit_loss_threshold` | 4 | Consecutive loss weeks before exit |
| `ai_price_undercut_pct` | 0.08 | Fare cut when competing |
| `ai_expansion_rate` | per competitor | Max new routes per eval (1–12 in JSON) |

Per-airline overrides: `data/competitors.json` (`expansion_rate`, `starter_spokes`, `strategy`, `growth`).

### 3.5 Competitor roster (Asia-relevant)

| ID | Name | Hub | Strategy | Notes |
|----|------|-----|----------|-------|
| AI_STRATOS | Stratos Airlines | KUL | BUDGET | Starter: SIN, BKK, **MNL**, DPS… |
| AI_APEX | Apex Pacific | ICN | POINTTOPOINT | Heavy China (PEK, PVG, CAN); no CN hub |
| AI_RIDGEWAY | Ridgeway Airlines | HND | PREMIUM | China + US starters |
| AI_ZENITH | Zenith Air | SIN | PREMIUM | Long-haul |
| AI_CRESTLINE | Crestline Airways | DXB | PREMIUM | EU + China |
| AI_HALCYON | Halcyon Airlines | IST | HUBSPOKE | EU + ME |
| AI_TEMPEST | Tempest Air | DEL | BUDGET | India + DXB |

**Gaps:** No competitor with hub in **China (CN)** or **Vietnam (VN)**.

---

## 4. Known gaps, bugs, and regional holes

### 4.1 Anchor coverage (approximate, from `bts_demand_anchors.csv`)

| Region | Airports in game | Anchor rows touching region |
|--------|------------------|----------------------------|
| China | 175 | ~890 |
| Middle East | 8 | ~1148 |
| Vietnam | 22 | ~115 |
| Japan | 84 | (Japan_Route + intl) |
| Philippines | — | sparse; MNL hub relies on gravity for many pairs |

### 4.2 AI route penetration (example save, week 4)

| Region | AI route pairs touching region | Problem |
|--------|----------------------------------|---------|
| Vietnam | **2** (KUL-SGN, HAN-SZX) | Almost no VN competition |
| China | ~35 | Mostly via ICN/DXB/HND — no CN-home carrier |
| Middle East | ~38 | Strong; weak into SEA/MNL |

### 4.3 Why AI under-expands to VN/CN (for DA to validate)

1. **No VN/CN hub airline** in `competitors.json`
2. **Candidate pool ranks by airport score**, not demand — SGN/HAN score below trophy cities
3. **Gate auctions** at PEK/PVG/SGN delay foreign entry
4. **`ai_min_profit_to_open`** — thin gravity-only routes may fail profit floor
5. **BUDGET radius 1500 nm** — KUL can reach VN but competes with 60 higher-score airports
6. **`expansion_rate` 1–4** for most AIs — slow weekly growth

---

## 5. DA work — specific tasks

### DA-1: Demand backtest report (Week 1–2)

**Goal:** Baseline model accuracy before any changes.

**Steps:**

1. Copy benchmark pattern from `Intl_Route/check_routes.py`
2. Build `DA_DS/scripts/demand_backtest.py` (or notebook) that:
   - Loads published weekly pax for **≥100 routes** (stratified table below)
   - Calls `compute_base_demand()` and full `preview_weekly_demand_before_open()`
   - Outputs: `route`, `source` (BTS/GRAVITY/LEGACY), `predicted_pool`, `published`, `ratio`, `abs_error`

**Stratification (minimum counts):**

| Stratum | N routes | Examples |
|---------|----------|----------|
| US domestic trunk | 15 | ATL-LAX, ORD-LAX, JFK-MIA |
| US short haul | 10 | BOS-DCA, LAX-SFO |
| Japan domestic | 10 | HND-FUK, HND-CTS, ITM-FUK |
| Korea | 8 | ICN-NRT, ICN-BKK |
| China | 10 | PEK-PVG, CAN-PEK, PEK-CTU |
| SEA | 10 | **MNL-SGN**, **MNL-HAN**, SIN-BKK, KUL-SIN |
| Europe | 10 | LHR-CDG, FRA-LHR |
| Middle East | 8 | DXB-LHR, IST-FRA |
| Long-haul | 10 | LHR-SIN, LAX-NRT |
| Thin / gravity-only | 15 | DLI-HPH, HAN-BOM |

**Acceptance criteria:**

- Report runs in `<5 min` on laptop
- CSV output: `DA_DS/reports/demand_backtest_YYYY-MM-DD.csv`
- Summary table: median ratio and MAPE **per stratum** and **per demand_source**

---

### DA-2: Coverage map (Week 2)

**Goal:** Know which city pairs are data-backed vs guessed.

**SQL / script logic:**

```sql
-- Pairs player could open: all airports within 4200nm of MNL
-- Tag each MNL-X pair: anchor exists? gravity only?
```

**Deliverable:** `DA_DS/reports/anchor_coverage_from_MNL.csv` with columns:

- `dest_iata`, `country`, `distance_nm`, `has_anchor`, `anchor_weekly`, `gravity_weekly`, `demand_source`, `rank_demand`

**Use:** `engine/route_suggestions.py` → `popular_destinations_from_origin('MNL', limit=50)`

**Acceptance:** List top 25 MNL destinations; flag any where `demand_source=GRAVITY` and `rank_demand > 500`.

---

### DA-3: Player vs model consistency audit (Week 2–3)

**Compare three numbers for 20 operated routes:**

| Source | Function / table |
|--------|------------------|
| Route open preview | `preview_weekly_demand_before_open()` |
| Live route demand | `compute_demand(route_id, game_week)` |
| Analytics panel | `engine/analytics.py` route card |
| Actual flown pax | `flight_segments` pax columns after week settles |

**Deliverable:** Table of `% difference` per route; flag systematic optimism (see `GAME_REVIEW.md` #15).

**Acceptance:** Document max/median gap; file eng ticket if panel > flown by >15% consistently.

---

### DA-4: AI route distribution report (Week 3)

**Goal:** Who flies where; is it balanced?

**Query template:**

```sql
SELECT c.name, c.home_hub_iata, c.strategy,
       COUNT(*) AS active_routes
FROM competitor_routes cr
JOIN competitors c ON c.competitor_id = cr.competitor_id
WHERE cr.status IN ('ACTIVE','SUSPENDED')
GROUP BY c.competitor_id;
```

**Extend:** Join `airports` on route endpoints → count routes **by country** and **by region**.

**Deliverable:** `DA_DS/reports/ai_routes_by_region.csv`

**Acceptance criteria (proposed game-health targets):**

| Region | Min AI route pairs (target) | Current (example save) |
|--------|----------------------------|-------------------------|
| Vietnam | ≥ 8 | 2 |
| China (touching CN airport) | ≥ 40 | ~35 |
| Philippines (touching PH) | ≥ 5 | measure |
| Middle East | ≥ 30 | ~38 |

---

### DA-5: AI rationality check (Week 3–4)

For each `competitor_routes` row:

| Field | Compare |
|-------|---------|
| `simulated_revenue`, `simulated_net`, `simulated_load_factor` | vs recomputed `weekly_pair_pnl()` |
| `estimated_weekly_profit` in `ai_route_candidates` | vs actual after 4+ weeks |

**Deliverable:** Scatter plot or table: predicted profit vs realized (if `actual_weekly_revenue_avg` populated).

**Acceptance:** Median absolute error < 30% on trunk routes; document failures on thin routes.

---

### DA-6: Weekly regression dashboard (ongoing)

After any model or constant change, re-run:

```bash
python3 Intl_Route/check_routes.py
python3 DA_DS/scripts/demand_backtest.py   # once built
pytest tests/test_bts_calibrate.py -q
```

**Deliverable:** One-page summary: pass/fail vs thresholds in §9.

---

### DA-7: Recommendations doc (ongoing)

Convert findings into **actionable tickets**:

| Finding type | Route to |
|--------------|----------|
| Wrong demand on MNL-SGN | DS: add VN anchors |
| AI never opens VN | Game design: add SGN hub competitor |
| Logit share wrong | DS: validate `compute_logit_shares_for_route` |
| Panel vs flown mismatch | Engineering |

Template: `DA_DS/reports/recommendations_YYYY-MM-DD.md`

---

## 6. DS work — specific tasks

### DS-1: Refit gravity bands (Week 3–5)

**Input:** `Intl_Route/*.csv`, `US_Route/*.csv`  
**Script:** `python3 Intl_Route/fit_gravity_bands.py`  
**Output:** `data/gravity_bands.json` → synced to `financial_constants.csv` via `db.sync_bts_gravity_constants_from_json`

**Tune:**

- `k`, `alpha`, `beta_short/medium/long`
- `quantile_blend` (spread correction)
- `region_priors` matrix in JSON

**Validation:**

```bash
pytest tests/test_bts_calibrate.py -q
python3 Intl_Route/check_routes.py
```

**Acceptance:**

- Anchored pairs: median `predicted/real` ∈ **[0.85, 1.15]**
- Modelled pairs: no trunk route > **2×** published benchmark without flag
- HND-FUK class trunks: ratio ∈ **[0.5, 2.0]** (was ~150× low before quantile mapping)

---

### DS-2: Elasticity estimation (Week 4–6)

**Current:** `engine/demand.py` lines 153–156, 252–259.

**Method options:**

1. **Simulation sweep:** For one route, vary fare ±30%, record `compute_demand` output → fit ε
2. **Cross-section:** If anchor data includes fare classes (usually not) — skip
3. **Literature priors:** Document why 0.3 / 1.2; adjust if backtest shows systematic bias

**Deliverable:** PR or spec updating `_elasticity_for_logit` and `price_factor` to use constants in `financial_constants.csv` (e.g. `demand_elasticity_business`, `demand_elasticity_leisure`).

**Acceptance:** Monotonicity preserved; no route exceeds 2× demand at −50% fare in tests.

---

### DS-3: Seasonality from data (Week 5–7)

**Current:** `data/seasonality.csv` — static monthly multipliers.

**Method:**

- Extract monthly variation from BTS monthly files (if available) or Eurostat
- Normalize to mean 1.0 per segment (business/leisure)
- Replace or blend with existing CSV

**Files:** `engine/demand.py` → `get_seasonality_multiplier()`

**Acceptance:** December vs July ratio on US trunk routes within ±20% of published seasonality (if benchmark exists).

---

### DS-4: Vietnam / China anchor ETL (Week 4–8)

**VN priority pairs for MNL player:**

- MNL-SGN, MNL-HAN, MNL-DAD, SGN-CAN, HAN-PEK, SGN-ICN

**CN priority:**

- Domestic trunk: PEK-PVG, PEK-CAN, SHA-PEK
- Intl: PEK-SIN, PVG-NRT, CAN-MNL

**Pipeline:**

1. Identify public data source (VN CAAC, China CAAC, OAG summaries, etc.)
2. Add fetch script under `Intl_Route/` or `Vietnam_Route/`
3. Run through `US_Route/bts_calibrate.py` pattern
4. Merge via `Intl_Route/merge_anchors.py`
5. Reload DB: seed or migration

**Acceptance:** `db.lookup_bts_anchor_weekly('MNL','SGN')` returns non-null; backtest ratio for MNL-SGN within [0.7, 1.4].

---

### DS-5: Airport score model (Week 6–10)

**Problem:** `Intl_Route/backfill_airports.py` uses peer heuristics; R² vs volume was ~0.23.

**Goal:** Predict `airports.score` from traffic proxies so gravity works on unanchored pairs.

**Features:** population, GDP proxy, hub flag, runway, gate_count, anchor degree.

**Deliverable:** Scoring script + updated `data/airports.csv` scores (with diff report).

**Acceptance:** Gravity-only pairs involving major Asian hubs rank above random small airports.

---

### DS-6: AI scoring model (Week 6–9)

**Current weights** (`engine/ai.py` ~line 1239):

```python
comp_score = demand*0.30 + profit*0.35 + network*0.20 + competition*0.10 + feas*0.05
```

**DS tasks:**

1. Log all `ai_route_candidates` for 50 game weeks
2. Label success = route still ACTIVE after 8 weeks AND `simulated_net > 0`
3. Fit weights (logistic regression or grid search) OR replace demand_score denominator (1600 is arbitrary)

**Also tune:**

- `ai_min_profit_to_open` — may be too high for VN thin routes
- Demand in candidate **generation** (not just scoring): boost pairs where `preview_weekly_demand > X`

**Acceptance:** After 50-week sim, no single AI > 40% trunk revenue; VN-touching routes ≥ 8 globally.

---

### DS-7: Recalibration pipeline (Week 8–10)

**One command:**

```bash
# Target interface (to be built)
./DA_DS/scripts/recalibrate_demand.sh
# → fetch_* → bts_calibrate → fit_gravity_bands → merge_anchors → pytest
```

**CI gate:** Fail if `check_routes.py` median ratio moves > 10% without approval.

---

### DS-8: Determinism fixes (spec for engineering)

From `GAME_REVIEW.md`:

| Bug | Fix |
|-----|-----|
| Global `random.seed()` | `rng = random.Random(seed)` local in `demand.py` |
| `hash(route_id)` in seeds | `zlib.crc32(route_id.encode())` |
| AI eval stagger `hash(cid)` | Same stable hash |

DS writes spec + test; engineering implements.

---

## 7. Regional expansion: VN, CN, Middle East

### 7.1 Is more AI needed there?

**Yes** — especially for a **Manila (MNL) player**:

| Region | Demand data | AI presence | Player impact |
|--------|-------------|-------------|---------------|
| Vietnam | Moderate anchors | **Very low** | No competition on MNL-SGN/HAN |
| China | Good anchors | Via foreign hubs only | Apex/ DXB fly CAN/PEK; no CN carrier persona |
| Middle East | Good | Moderate | DXB/IST strong; few MNL links |

### 7.2 DA recommendations (no code)

1. Measure demand on **MNL-SGN, MNL-HAN, MNL-CAN, MNL-DXB** — credible?
2. Report AI route count by country weekly
3. Propose `competitors.json` additions (game design)

### 7.3 DS + game design changes

**A. New competitors (game design + JSON)** — fastest win

| Proposed carrier | Hub | Strategy | Starter spokes |
|------------------|-----|----------|----------------|
| e.g. Mekong Air | SGN | HUBSPOKE | MNL, HAN, DAD, CAN, SIN, BKK |
| e.g. Yangtze Air | PVG | HUBSPOKE | PEK, CAN, CTU, MNL, NRT, ICN |

File: `data/competitors.json` — see `_generator_prompt` in file for schema.

**B. DS demand work** — so AI scoring sees profit on those routes

- Add VN intl anchors
- Validate CN domestic + CN-PH pairs
- Tune `bts_min_weekly_pool` if thin routes die

**C. DS AI logic** — optional code changes

- Region boost in `ai_generate_candidates`: +score if pair touches under-served country
- Lower `ai_min_profit_to_open` for ungated airports only
- Increase `expansion_rate` for `AI_STRATOS` (KUL) and add VN to `starter_spokes`

### 7.4 Division of labor

| Action | DA | DS | Game dev |
|--------|:--:|:--:|:--------:|
| Prove MNL-SGN demand too low/high | ✓ | | |
| Add VN anchor CSV | | ✓ | |
| Refit gravity for AS-AS pairs | | ✓ | |
| Add SGN-hub competitor | | | ✓ |
| Tune AI weights to open VN | spec | ✓ | review |

---

## 8. SQL & scripts cheat sheet

### 8.1 Useful tables

| Table | Purpose |
|-------|---------|
| `airports` | score, country, gate_count, category |
| `routes` | player network, prices, base_demand_* (legacy) |
| `flight_segments` | scheduled/landed legs, pax, times |
| `week_ledger` | weekly P&L |
| `competitors` | AI roster, cash, strategy, hub |
| `competitor_routes` | AI network, fares, simulated_* |
| `ai_route_candidates` | scored options, rejection_reason |
| `bts_demand_anchors` | anchor weekly pax (in DB after seed) |

### 8.2 Example queries

**AI routes touching Vietnam:**

```sql
SELECT c.name, cr.route_pair_id
FROM competitor_routes cr
JOIN competitors c ON c.competitor_id = cr.competitor_id
JOIN airports a ON a.iata IN (
  substr(cr.route_pair_id, 1, 3),
  substr(cr.route_pair_id, 5, 3)
)
WHERE a.country = 'VN' AND cr.status IN ('ACTIVE','SUSPENDED');
```

**Player MNL peak gates (external analysis):**

```python
from engine.gates import player_gate_peak_at_airport, _allocated_gates
gw = 4  # from game_state
print(player_gate_peak_at_airport('MNL', gw), _allocated_gates('MNL', 'PLAYER'))
```

**Demand preview for one pair:**

```python
from engine.airports import get_airport
from engine.demand import preview_weekly_demand_before_open
from engine.routes import haversine_distance
o, d = get_airport('MNL'), get_airport('SGN')
dist = haversine_distance(o['lat'], o['lon'], d['lat'], d['lon'])
print(preview_weekly_demand_before_open(o, d, dist))
```

### 8.3 Commands

```bash
# Demand benchmark (17 routes)
python3 Intl_Route/check_routes.py

# Intl gravity analysis
python3 Intl_Route/intl_analyze.py

# Refit gravity
python3 Intl_Route/fit_gravity_bands.py

# Merge anchors
python3 Intl_Route/merge_anchors.py

# Tests
pytest tests/test_bts_calibrate.py -q
pytest tests/test_smoke.py -k "AI" -q

# Popular destinations from MNL (in-game API or Python)
python3 -c "from engine.route_suggestions import popular_destinations_from_origin; import json; print(json.dumps(popular_destinations_from_origin('MNL', limit=10), indent=2))"
```

---

## 9. Deliverables checklist

### DA deliverables

| ID | Deliverable | Format | Done when |
|----|-------------|--------|-----------|
| DA-1 | Demand backtest ≥100 routes | CSV + 1-page summary | Stratum table complete |
| DA-2 | Anchor coverage from MNL | CSV | Top 50 dests tagged BTS/GRAVITY |
| DA-3 | Panel vs flown gap study | Markdown | Median gap documented |
| DA-4 | AI routes by region | CSV | Weekly refresh |
| DA-5 | AI profit prediction accuracy | Chart/table | Error bounds documented |
| DA-6 | Weekly regression run log | Markdown | Attached to each model PR |
| DA-7 | Recommendations | Markdown | Linked to tickets |

### DS deliverables

| ID | Deliverable | Format | Done when |
|----|-------------|--------|-----------|
| DS-1 | Refit gravity | `gravity_bands.json` + PR | Tests pass; backtest in band |
| DS-2 | Elasticity constants | PR to `demand.py` / CSV | Documented ε values |
| DS-3 | Seasonality update | `seasonality.csv` + PR | Sanity vs known season |
| DS-4 | VN/CN anchor ETL | Script + CSV + merge | MNL-SGN anchored |
| DS-5 | Airport score model | Script + airport diff | Major hubs rank correctly |
| DS-6 | AI scoring tune | PR to `ai.py` | 50-week sim meets balance targets |
| DS-7 | Recalibrate script | `DA_DS/scripts/` | One-command pipeline |
| DS-8 | Determinism spec | Markdown + test | Eng implements |

### Shared acceptance thresholds (proposed)

| Metric | Threshold |
|--------|-----------|
| Anchored route median predicted/real | 0.85 – 1.15 |
| Gravity-only thin route pool | ≥ `bts_min_weekly_pool` (750) after mult |
| AI routes touching Vietnam | ≥ 8 (after roster + tune) |
| Single AI share of global trunk revenue | < 40% |
| `test_bts_calibrate.py` | 100% pass |
| Player route panel vs flown pax | < 15% median overstatement |

---

## 10. 90-day plan

### Month 1 — Measure (mostly DA)

| Week | DA | DS |
|------|----|----|
| 1 | DA-1 benchmark design; run `check_routes.py` baseline | Read pipeline; run `intl_analyze.py` |
| 2 | DA-1 report; DA-2 coverage map | Document current constants |
| 3 | DA-3 panel audit; DA-4 AI distribution | DS-4 source research (VN/CN data) |
| 4 | DA-5 rationality; DA-7 recommendations | DS-1 gravity refit start |

### Month 2 — Model (mostly DS)

| Week | DA | DS |
|------|----|----|
| 5 | Weekly regression; validate DS-1 | DS-1 complete; DS-2 elasticity |
| 6 | DA-4 weekly; VN gap report | DS-4 VN anchors ingest |
| 7 | Player hub (MNL) focus report | DS-3 seasonality |
| 8 | AI balance pre/post demand change | DS-6 AI scoring experiments |

### Month 3 — Integrate (Both)

| Week | DA | DS |
|------|----|----|
| 9 | 50-week sim observation | DS-6 weights PR |
| 10 | Post-change backtest | DS-7 pipeline |
| 11 | Final balance report | DS-5 airport scores (if time) |
| 12 | Handoff doc + monitoring plan | DS-8 determinism with eng |

---

## 11. File index

```
# This folder
DA_DS/demand_and_ai_competition_work.md    ← this document
DA_DS/reports/                             ← DA outputs (create as needed)
DA_DS/scripts/                             ← shared analysis scripts (create as needed)

# Demand engine
engine/demand.py                           # compute_demand, logit, elasticity, noise
engine/route_demand.py                     # BTS, gravity, legacy, quantile map
engine/route_suggestions.py                # popular destinations API
engine/demand_display.py                   # UI helpers

# Calibration & ETL
US_Route/bts_calibrate.py
Intl_Route/fit_gravity_bands.py
Intl_Route/merge_anchors.py
Intl_Route/intl_analyze.py
Intl_Route/check_routes.py
Intl_Route/backfill_airports.py
Japan_Route/fetch_japan.py
Korea_Route/fetch_korea.py
Europe_Route/fetch_eurostat.py
Australia_Route/fetch_australia.py

# Data artifacts
data/bts_demand_anchors.csv
data/gravity_bands.json
data/financial_constants.csv
data/seasonality.csv
data/airports.csv
data/competitors.json

# AI
engine/ai.py                               # candidates, scoring, weekly turn
engine/ai_economics.py                     # P&L, logit shares
engine/ai_flights.py                       # spawn segments
engine/ai_gates.py                         # AI gate bidding

# Analytics & settlement
engine/analytics.py                        # route cards, KPIs
engine/settlement.py                       # week_ledger

# Tests & review
tests/test_bts_calibrate.py
tests/test_smoke.py
GAME_REVIEW.md                             # known bugs
docs/phase_implementation_detail.md        # phase roadmap
```

---

## Summary

| Question | Answer |
|----------|--------|
| Is the DA role only to improve the current model? | **Mostly yes for DA** — measure, validate, recommend. **DS** implements model changes. |
| Does it include building models? | **DS yes** (gravity, elasticity, AI scoring). **DA** usually evaluates models, not builds them. |
| Why is VN/CN AI weak? | Roster gap (no VN/CN hub) + scoring/radius + demand/gravity on thin pairs. |
| Fastest fix for more VN/CN AI? | New `competitors.json` hubs + VN anchors + lower profit floor for thin routes. |
| First week action? | Run `check_routes.py`, build 100-route backtest, SQL AI routes by country. |
