# Phase implementation detail

What each build phase added in **Airside Machine**.

| Phase | Status | What it added |
|-------|--------|----------------|
| **0 — Foundation** | Built | Project layout, SQLite schema/seed from CSV, DB helpers, `main.py` shell |
| **1 — Airline & static data** | Built | Create airline, aircraft buy/lease + cabins, airports, open routes, Rich panels |
| **2 — Demand & pricing** | Built | Demand model, fare elasticity, revenue estimates, set prices |
| **3 — Clock & scheduling** | Built | Continuous clock (pause / 1×–20×), rotations, flight segments, depart/arrive |
| **4 — Settlement** | Built | Week boundary P&L → `week_ledger`, cash move, week summary |
| **5 — Persistence** | Built | Schema migrations, clock resume from `game_state`, crash-safe save habits |
| **6 — KPI / analytics** | Built | Route cards, fleet utilization, dashboard KPIs |
| **7 — Overlay & polish** | Built | Browser map UI + dock windows, news/notifications, auto-pause hooks |
| **8 — Fuel** | Built | Spot ticker, shocks, hedge / reserve / dip alerts |
| **9 — Events** | Built | AOG, weather closures, delay cascade, reputation ↔ brand power |
| **10 — AI & runway slots** | Built | Named AI competitors, logit share, weekly movement quotas + slot auctions |
| **11 — Gate auctions** | Built | Stand-unit auctions (separate from runway slots), utilization / retention |
| **12 — Banking** | Built | Loans, credit score, weekly debt service, Chapter 11 path, Bank overlay/CLI |
| **13 — Marketing** | Unbuilt | Route/network campaigns, demand bonuses, permanent brand gains *(prep: `marketing_brand_bonus` column so settlement won’t wipe campaign brand)* |
| **14 — Onboarding** | Unbuilt | First-run tutorial / sandbox week |

