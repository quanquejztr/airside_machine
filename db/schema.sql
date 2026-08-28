-- Airline Sim Database Schema
-- Phase 0 - Foundation
-- All 13 tables: 5 static reference tables + 8 dynamic game state tables

-- ============================================================================
-- STATIC REFERENCE TABLES (seed-only, never mutated during play)
-- ============================================================================

-- Airport Categories: defines default fee structures for 3 airport tiers
CREATE TABLE IF NOT EXISTS airport_categories (
    category TEXT PRIMARY KEY CHECK(category IN ('small_airport', 'medium_airport', 'large_airport')),
    slot_tier INTEGER NOT NULL CHECK(slot_tier IN (1, 2, 3)),
    landing_fee_per_1000 REAL NOT NULL,
    gate_fee REAL NOT NULL,
    runway_min_ft INTEGER NOT NULL,
    curfew_active INTEGER NOT NULL CHECK(curfew_active IN (0, 1)),
    curfew_start TEXT,
    curfew_end TEXT
);

-- Airports: one row per airport with per-airport overrides
CREATE TABLE IF NOT EXISTS airports (
    iata TEXT PRIMARY KEY,
    icao TEXT NOT NULL,
    name TEXT NOT NULL,
    city TEXT NOT NULL,
    country TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    runway_length_ft INTEGER,
    gate_count INTEGER NOT NULL,
    timezone TEXT NOT NULL,
    score INTEGER NOT NULL DEFAULT 0,
    category TEXT NOT NULL,
    landing_fee_override REAL,
    gate_fee_override REAL,
    has_curfew INTEGER CHECK(has_curfew IN (0, 1)),
    curfew_start TEXT,
    curfew_end TEXT,
    FOREIGN KEY (category) REFERENCES airport_categories(category)
);

-- Aircraft Types: one row per aircraft model
CREATE TABLE IF NOT EXISTS aircraft_types (
    type_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('TURBOPROP', 'REGIONAL_JET', 'NARROW', 'WIDE')),
    range_nm INTEGER NOT NULL,
    cruise_speed_kts INTEGER NOT NULL,
    fuel_burn_gph REAL NOT NULL,
    mtow_lbs INTEGER NOT NULL,
    runway_req_ft INTEGER NOT NULL,
    purchase_price INTEGER NOT NULL,
    weekly_lease_cost INTEGER NOT NULL,
    eec INTEGER,
    maintenance_interval_weeks INTEGER
);

-- Aircraft Default Cabin Configurations: one row per aircraft type
CREATE TABLE IF NOT EXISTS aircraft_default_config (
    type_id TEXT PRIMARY KEY,
    seats_economy INTEGER NOT NULL,
    seats_premium_economy INTEGER NOT NULL,
    seats_business INTEGER NOT NULL,
    seats_first INTEGER NOT NULL,
    eec_used INTEGER NOT NULL,
    FOREIGN KEY (type_id) REFERENCES aircraft_types(type_id)
);

-- Seasonality: 12 rows (one per month)
CREATE TABLE IF NOT EXISTS seasonality (
    month INTEGER PRIMARY KEY CHECK(month >= 1 AND month <= 12),
    business_multiplier REAL NOT NULL,
    leisure_multiplier REAL NOT NULL
);

-- Financial Constants: key-value store for static parameters
CREATE TABLE IF NOT EXISTS financial_constants (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL
);

-- US BTS market anchors (precomputed from US_Route/bts_calibrate.py → data/bts_demand_anchors.csv).
CREATE TABLE IF NOT EXISTS bts_demand_anchors (
    origin_iata TEXT NOT NULL,
    dest_iata TEXT NOT NULL,
    anchor_annual REAL NOT NULL,
    anchor_weekly REAL NOT NULL,
    years_used INTEGER,
    first_year INTEGER,
    last_year INTEGER,
    method TEXT,
    PRIMARY KEY (origin_iata, dest_iata)
);
CREATE INDEX IF NOT EXISTS idx_bts_anchors_dest ON bts_demand_anchors(dest_iata);

-- ============================================================================
-- DYNAMIC GAME STATE TABLES (read/write during play)
-- ============================================================================

-- Game State: single-row master session record
CREATE TABLE IF NOT EXISTS game_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    schema_version INTEGER NOT NULL DEFAULT 1,
    game_week INTEGER NOT NULL DEFAULT 1,
    game_hours_elapsed REAL NOT NULL DEFAULT 0.0,
    speed_multiplier INTEGER NOT NULL DEFAULT 0 CHECK(speed_multiplier IN (0, 1, 2, 4, 20)),
    current_month INTEGER NOT NULL CHECK(current_month >= 1 AND current_month <= 12),
    fuel_price_current REAL NOT NULL,
    fuel_price_trend REAL NOT NULL,
    demand_noise_seed INTEGER NOT NULL DEFAULT 1,
    pause_on_week_summary INTEGER NOT NULL DEFAULT 0 CHECK(pause_on_week_summary IN (0, 1)),
    fuel_shock_pending INTEGER NOT NULL DEFAULT 0 CHECK(fuel_shock_pending IN (0, 1)),
    fuel_shock_message TEXT,
    ui_blackout INTEGER NOT NULL DEFAULT 0 CHECK(ui_blackout IN (0, 1))
);

-- Airline: single-row player record
CREATE TABLE IF NOT EXISTS airline (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    name TEXT NOT NULL,
    callsign TEXT NOT NULL,
    home_hub_iata TEXT NOT NULL,
    cash REAL NOT NULL,
    total_debt REAL NOT NULL DEFAULT 0.0,
    credit_score INTEGER NOT NULL CHECK(credit_score >= 300 AND credit_score <= 850),
    reputation_score REAL NOT NULL CHECK(reputation_score >= 0 AND reputation_score <= 100),
    brand_power REAL NOT NULL DEFAULT 1.0,
    marketing_brand_bonus REAL NOT NULL DEFAULT 0.0,
    xp INTEGER NOT NULL DEFAULT 0,
    fuel_hedged_price REAL,
    fuel_hedged_weeks_remaining INTEGER,
    fuel_reserve_gallons REAL NOT NULL DEFAULT 0.0,
    fuel_dip_alert_price REAL,
    fuel_reserve_avg_price REAL,
    negative_cash_weeks INTEGER NOT NULL DEFAULT 0,
    last_chapter11_week INTEGER,
    FOREIGN KEY (home_hub_iata) REFERENCES airports(iata)
);

-- Fleet: one row per aircraft owned or leased
CREATE TABLE IF NOT EXISTS fleet (
    tail_number TEXT PRIMARY KEY,
    type_id TEXT NOT NULL,
    ownership TEXT NOT NULL CHECK(ownership IN ('OWNED', 'LEASED')),
    status TEXT NOT NULL CHECK(status IN ('IDLE', 'SCHEDULED', 'IN_AIR', 'LANDED', 'AOG', 'MAINTENANCE')),
    current_airport_iata TEXT,
    lease_weekly_cost REAL NOT NULL DEFAULT 0.0,
    lease_weeks_remaining INTEGER,
    lease_prepaid INTEGER NOT NULL DEFAULT 0,
    weeks_since_maintenance INTEGER NOT NULL DEFAULT 0,
    aog_reason TEXT,
    FOREIGN KEY (type_id) REFERENCES aircraft_types(type_id),
    FOREIGN KEY (current_airport_iata) REFERENCES airports(iata)
);

-- Fleet Cabin Configurations: one row per aircraft in fleet
CREATE TABLE IF NOT EXISTS fleet_cabin_config (
    tail_number TEXT PRIMARY KEY,
    seats_economy INTEGER NOT NULL,
    seats_premium_economy INTEGER NOT NULL,
    seats_business INTEGER NOT NULL,
    seats_first INTEGER NOT NULL,
    eec_used INTEGER NOT NULL,
    last_reconfig_week INTEGER,
    reconfig_cost_paid REAL DEFAULT 0.0,
    FOREIGN KEY (tail_number) REFERENCES fleet(tail_number)
);

-- Routes: one row per active route (city-pair + fare class configuration)
CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT PRIMARY KEY,
    origin_iata TEXT NOT NULL,
    dest_iata TEXT NOT NULL,
    distance_nm REAL NOT NULL,
    base_demand_business INTEGER NOT NULL,
    base_demand_leisure INTEGER NOT NULL,
    price_business REAL NOT NULL,
    price_leisure REAL NOT NULL,
    price_premium_economy REAL NOT NULL,
    price_first REAL NOT NULL,
    competitor_share_this_week REAL NOT NULL DEFAULT 0.0,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
    demand_source TEXT,
    FOREIGN KEY (origin_iata) REFERENCES airports(iata),
    FOREIGN KEY (dest_iata) REFERENCES airports(iata)
);

-- Player routes: which directional routes the PLAYER has opened (so UI can avoid listing AI-only market routes).
CREATE TABLE IF NOT EXISTS player_routes (
    route_id TEXT PRIMARY KEY,
    opened_week INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (route_id) REFERENCES routes(route_id)
);
CREATE INDEX IF NOT EXISTS idx_player_routes_opened_week ON player_routes(opened_week);

-- Phase 11 (revised): airport gate-use auctions (realistic scarcity at major hubs)
-- Airports with score >= 900000 are "auctioned airports".
-- Gate units represent how many gate-uses (flight endpoints) you may operate per week at that airport.

CREATE TABLE IF NOT EXISTS airport_gate_allocations (
    allocation_id TEXT PRIMARY KEY,
    airport_iata TEXT NOT NULL,
    holder_id TEXT NOT NULL,          -- 'PLAYER' or competitor_id
    gate_units INTEGER NOT NULL,      -- capacity for the week
    used_this_week INTEGER NOT NULL DEFAULT 0,
    scheduled_this_week INTEGER NOT NULL DEFAULT 0,
    below_threshold_weeks INTEGER NOT NULL DEFAULT 0,
    effective_week INTEGER NOT NULL DEFAULT 1,  -- gates won at settlement become effective next week
    status TEXT NOT NULL CHECK(status IN ('ACTIVE','REVOKED')),
    FOREIGN KEY (airport_iata) REFERENCES airports(iata)
);

CREATE TABLE IF NOT EXISTS airport_gate_auctions (
    auction_id TEXT PRIMARY KEY,
    airport_iata TEXT NOT NULL,
    opens_week INTEGER NOT NULL,
    closes_week INTEGER NOT NULL,
    units_available INTEGER NOT NULL,
    current_price_per_unit REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('OPEN','RESOLVED','CANCELLED')),
    FOREIGN KEY (airport_iata) REFERENCES airports(iata)
);

CREATE TABLE IF NOT EXISTS airport_gate_bids (
    bid_id TEXT PRIMARY KEY,
    auction_id TEXT NOT NULL,
    bidder_id TEXT NOT NULL,          -- 'PLAYER' or competitor_id
    units_requested INTEGER NOT NULL,
    price_per_unit REAL NOT NULL,
    submitted_week INTEGER NOT NULL,
    FOREIGN KEY (auction_id) REFERENCES airport_gate_auctions(auction_id)
);

-- Sticky product flight numbers by directed route (e.g. ONT-TPA → ALL1992)
CREATE TABLE IF NOT EXISTS route_flight_numbers (
    route_id TEXT PRIMARY KEY,
    flight_number TEXT NOT NULL,
    updated_week INTEGER NOT NULL DEFAULT 1
);

-- Flight Schedules: weekly recurring flight schedules
CREATE TABLE IF NOT EXISTS flight_schedules (
    schedule_id TEXT PRIMARY KEY,
    tail_number TEXT NOT NULL,
    route_id TEXT NOT NULL,
    flight_number TEXT NOT NULL,
    days_of_week TEXT NOT NULL,  -- JSON array: ["MON","WED","FRI"] or "DAILY"
    departure_time TEXT NOT NULL,  -- HH:MM format (e.g., "08:00", "14:30")
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_week INTEGER NOT NULL,
    FOREIGN KEY (tail_number) REFERENCES fleet(tail_number),
    FOREIGN KEY (route_id) REFERENCES routes(route_id)
);

-- Weekly rotation templates: one row per tail; legs repeat each in-game week
CREATE TABLE IF NOT EXISTS weekly_rotations (
    tail_number TEXT PRIMARY KEY,
    legs_json TEXT NOT NULL,
    created_game_week INTEGER NOT NULL,
    FOREIGN KEY (tail_number) REFERENCES fleet(tail_number)
);

-- Flight Segments: one row per actual flight execution
CREATE TABLE IF NOT EXISTS flight_segments (
    segment_id TEXT PRIMARY KEY,
    schedule_id TEXT,  -- Link to recurring schedule (NULL for one-off flights)
    game_week INTEGER NOT NULL,
    day_of_week TEXT NOT NULL CHECK(day_of_week IN ('MON','TUE','WED','THU','FRI','SAT','SUN')),
    tail_number TEXT NOT NULL,
    route_id TEXT NOT NULL,
    origin_iata TEXT,
    dest_iata TEXT,
    flight_number TEXT NOT NULL,
    scheduled_dep_time TEXT NOT NULL,  -- HH:MM format (display)
    scheduled_dep_game_hour REAL NOT NULL,  -- Absolute game hours since game start (continuous clock)
    actual_dep_game_hour REAL,
    scheduled_arr_time TEXT NOT NULL,  -- HH:MM format (display)
    scheduled_arr_game_hour REAL NOT NULL,
    baseline_dep_game_hour REAL NOT NULL,
    baseline_arr_game_hour REAL NOT NULL,
    actual_arr_game_hour REAL,
    status TEXT NOT NULL CHECK(status IN ('SCHEDULED', 'IN_AIR', 'LANDED', 'CANCELLED', 'DELAYED', 'DIVERTED', 'HOLDING')),
    pax_business INTEGER NOT NULL,
    pax_leisure INTEGER NOT NULL,
    pax_economy INTEGER NOT NULL DEFAULT 0,
    pax_premium_economy INTEGER NOT NULL DEFAULT 0,
    pax_business_cabin INTEGER NOT NULL DEFAULT 0,
    pax_first INTEGER NOT NULL DEFAULT 0,
    revenue_gross REAL NOT NULL,
    excise_tax REAL NOT NULL,
    segment_fee REAL NOT NULL,
    security_fee REAL NOT NULL,
    pfc_fee REAL NOT NULL,
    landing_fee REAL NOT NULL,
    gate_fee REAL NOT NULL,
    fuel_burned_gallons REAL,
    fuel_spot_price_per_gallon REAL,
    fuel_cost REAL,
    fuel_cost_usd REAL,
    net_contribution REAL,
    revenue_economy REAL NOT NULL DEFAULT 0,
    revenue_premium_economy REAL NOT NULL DEFAULT 0,
    revenue_business_cabin REAL NOT NULL DEFAULT 0,
    revenue_first REAL NOT NULL DEFAULT 0,
    delay_minutes INTEGER NOT NULL DEFAULT 0,
    turn_minutes INTEGER,
    divert_airport_iata TEXT,
    divert_surcharge REAL NOT NULL DEFAULT 0,
    is_ferry INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (schedule_id) REFERENCES flight_schedules(schedule_id),
    FOREIGN KEY (tail_number) REFERENCES fleet(tail_number),
    FOREIGN KEY (route_id) REFERENCES routes(route_id)
);

-- Event log: append-only gameplay events (Phase 9)
CREATE TABLE IF NOT EXISTS event_log (
    event_id TEXT PRIMARY KEY,
    game_week INTEGER NOT NULL,
    game_time_hours REAL NOT NULL,
    event_type TEXT NOT NULL,
    affected_iata TEXT,
    affected_tail TEXT,
    description TEXT NOT NULL,
    financial_impact REAL NOT NULL DEFAULT 0,
    resolved INTEGER NOT NULL DEFAULT 0 CHECK(resolved IN (0, 1))
);

-- Phase 10: AI competitors & slot auctions
CREATE TABLE IF NOT EXISTS competitors (
    competitor_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    callsign TEXT NOT NULL,
    home_hub_iata TEXT NOT NULL,
    cash REAL NOT NULL,
    strategy TEXT NOT NULL,
    aggressiveness REAL NOT NULL,
    risk_tolerance REAL NOT NULL,
    expansion_rate INTEGER NOT NULL,
    fleet_size INTEGER NOT NULL,
    max_fleet_size INTEGER NOT NULL,
    weekly_route_budget REAL NOT NULL,
    bid_probability REAL NOT NULL,
    weekly_slot_budget REAL NOT NULL,
    reputation REAL NOT NULL,
    brand_power REAL NOT NULL,
    consecutive_loss_weeks INTEGER NOT NULL DEFAULT 0,
    last_evaluation_week INTEGER NOT NULL DEFAULT 0,
    stance TEXT NOT NULL DEFAULT 'GROW',
    stance_since_week INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (home_hub_iata) REFERENCES airports(iata)
);

CREATE TABLE IF NOT EXISTS competitor_routes (
    competitor_id TEXT NOT NULL,
    route_pair_id TEXT NOT NULL,
    outbound_route_id TEXT NOT NULL,
    inbound_route_id TEXT NOT NULL,
    fare_business REAL NOT NULL,
    fare_leisure REAL NOT NULL,
    frequency_per_week INTEGER NOT NULL,
    aircraft_type_id TEXT,
    opened_week INTEGER NOT NULL,
    status TEXT NOT NULL,
    estimated_weekly_profit REAL NOT NULL DEFAULT 0.0,
    actual_weekly_revenue_avg REAL NOT NULL DEFAULT 0.0,
    consecutive_loss_weeks INTEGER NOT NULL DEFAULT 0,
    contested INTEGER NOT NULL DEFAULT 0,
    market_share REAL NOT NULL DEFAULT 0.0,
    actual_weekly_net_avg REAL NOT NULL DEFAULT 0.0,
    actual_lf_avg REAL NOT NULL DEFAULT 0.0,
    paper_loss_weeks INTEGER NOT NULL DEFAULT 0,
    low_lf_weeks INTEGER NOT NULL DEFAULT 0,
    fare_war_weeks INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (competitor_id, route_pair_id),
    FOREIGN KEY (competitor_id) REFERENCES competitors(competitor_id),
    FOREIGN KEY (outbound_route_id) REFERENCES routes(route_id),
    FOREIGN KEY (inbound_route_id) REFERENCES routes(route_id)
);

CREATE TABLE IF NOT EXISTS competitor_bids (
    bid_id TEXT PRIMARY KEY,
    auction_id TEXT NOT NULL,
    competitor_id TEXT NOT NULL,
    bid_amount REAL NOT NULL,
    submitted_week INTEGER NOT NULL,
    FOREIGN KEY (competitor_id) REFERENCES competitors(competitor_id)
);

CREATE INDEX IF NOT EXISTS idx_competitor_routes_outbound ON competitor_routes(outbound_route_id);
CREATE INDEX IF NOT EXISTS idx_competitor_routes_inbound ON competitor_routes(inbound_route_id);
CREATE INDEX IF NOT EXISTS idx_competitor_bids_auction ON competitor_bids(auction_id);
CREATE INDEX IF NOT EXISTS idx_gate_alloc_airport ON airport_gate_allocations(airport_iata);
CREATE INDEX IF NOT EXISTS idx_gate_alloc_holder ON airport_gate_allocations(holder_id);
CREATE INDEX IF NOT EXISTS idx_gate_auctions_status ON airport_gate_auctions(status);
CREATE INDEX IF NOT EXISTS idx_gate_bids_auction ON airport_gate_bids(auction_id);

-- Phase 11+ AI system tables (PDF spec)
CREATE TABLE IF NOT EXISTS ai_route_candidates (
    candidate_id TEXT PRIMARY KEY,
    competitor_id TEXT NOT NULL,
    route_pair_id TEXT NOT NULL,
    score REAL NOT NULL,
    estimated_weekly_profit REAL NOT NULL,
    estimated_market_share REAL NOT NULL,
    estimated_entry_fare_leisure REAL NOT NULL,
    estimated_entry_fare_business REAL NOT NULL,
    evaluated_week INTEGER NOT NULL,
    decision TEXT NOT NULL,
    rejection_reason TEXT,
    FOREIGN KEY (competitor_id) REFERENCES competitors(competitor_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_candidates_competitor ON ai_route_candidates(competitor_id);
CREATE INDEX IF NOT EXISTS idx_ai_candidates_score ON ai_route_candidates(score);

CREATE TABLE IF NOT EXISTS ai_memory (
    memory_id TEXT PRIMARY KEY,
    competitor_id TEXT NOT NULL,
    route_pair_id TEXT NOT NULL,
    event TEXT NOT NULL,
    game_week INTEGER NOT NULL,
    cooldown_weeks INTEGER NOT NULL,
    FOREIGN KEY (competitor_id) REFERENCES competitors(competitor_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_memory_competitor ON ai_memory(competitor_id);
CREATE INDEX IF NOT EXISTS idx_ai_memory_pair ON ai_memory(route_pair_id);

CREATE TABLE IF NOT EXISTS ai_fleet (
    ai_tail TEXT PRIMARY KEY,
    competitor_id TEXT NOT NULL,
    type_id TEXT NOT NULL,
    status TEXT NOT NULL,
    assigned_route_pair_id TEXT,
    FOREIGN KEY (competitor_id) REFERENCES competitors(competitor_id),
    FOREIGN KEY (type_id) REFERENCES aircraft_types(type_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_fleet_competitor ON ai_fleet(competitor_id);

CREATE TABLE IF NOT EXISTS ai_turn_log (
    log_id TEXT PRIMARY KEY,
    competitor_id TEXT NOT NULL,
    game_week INTEGER NOT NULL,
    routes_opened TEXT,
    routes_closed TEXT,
    fares_adjusted TEXT,
    auctions_bid TEXT,
    candidates_evaluated INTEGER NOT NULL DEFAULT 0,
    duration_ms REAL NOT NULL DEFAULT 0.0,
    error TEXT,
    narrative TEXT,
    FOREIGN KEY (competitor_id) REFERENCES competitors(competitor_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_turn_log_competitor ON ai_turn_log(competitor_id, game_week);

CREATE TABLE IF NOT EXISTS ai_flight_segments (
    segment_id TEXT PRIMARY KEY,
    competitor_id TEXT NOT NULL,
    route_id TEXT NOT NULL,
    game_week INTEGER NOT NULL,
    flight_number TEXT NOT NULL,
    origin_iata TEXT NOT NULL,
    dest_iata TEXT NOT NULL,
    scheduled_dep_game_hour REAL NOT NULL,
    scheduled_arr_game_hour REAL NOT NULL,
    actual_dep_game_hour REAL,
    actual_arr_game_hour REAL,
    frequency INTEGER NOT NULL,
    status TEXT NOT NULL,
    simulated_pax_leisure INTEGER NOT NULL DEFAULT 0,
    simulated_pax_business INTEGER NOT NULL DEFAULT 0,
    simulated_revenue REAL NOT NULL DEFAULT 0.0,
    simulated_load_factor REAL NOT NULL DEFAULT 0.0,
    simulated_net REAL NOT NULL DEFAULT 0.0,
    FOREIGN KEY (competitor_id) REFERENCES competitors(competitor_id),
    FOREIGN KEY (route_id) REFERENCES routes(route_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_segments_origin ON ai_flight_segments(origin_iata, game_week);
CREATE INDEX IF NOT EXISTS idx_ai_segments_dest ON ai_flight_segments(dest_iata, game_week);

CREATE TABLE IF NOT EXISTS player_notifications (
    notification_id TEXT PRIMARY KEY,
    game_week INTEGER NOT NULL,
    type TEXT NOT NULL,
    route_pair_id TEXT,
    body TEXT NOT NULL,
    read INTEGER NOT NULL DEFAULT 0 CHECK(read IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_player_notifications_read ON player_notifications(read, game_week);

-- Airport board indexes for player segments (Phase 11+)
CREATE INDEX IF NOT EXISTS idx_flight_segments_origin_week ON flight_segments(origin_iata, game_week);
CREATE INDEX IF NOT EXISTS idx_flight_segments_dest_week ON flight_segments(dest_iata, game_week);

-- Phase 12: player loans and credit journal
CREATE TABLE IF NOT EXISTS loans (
    loan_id TEXT PRIMARY KEY,
    principal_original REAL NOT NULL,
    principal_remaining REAL NOT NULL,
    weekly_interest_rate REAL NOT NULL,
    weekly_payment REAL NOT NULL,
    weeks_remaining INTEGER NOT NULL,
    originated_week INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ACTIVE','CLOSED','DEFAULTED'))
);
CREATE INDEX IF NOT EXISTS idx_loans_status ON loans(status);

CREATE TABLE IF NOT EXISTS credit_events (
    event_id TEXT PRIMARY KEY,
    game_week INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    score_delta INTEGER NOT NULL,
    description TEXT
);
CREATE INDEX IF NOT EXISTS idx_credit_events_week ON credit_events(game_week);

-- Week Ledger: one row per week, written at settlement
CREATE TABLE IF NOT EXISTS week_ledger (
    game_week INTEGER PRIMARY KEY,
    revenue_gross REAL NOT NULL,
    excise_tax REAL NOT NULL,
    segment_fees REAL NOT NULL,
    security_fees REAL NOT NULL,
    pfc_fees REAL NOT NULL,
    landing_fees REAL NOT NULL,
    gate_fees REAL NOT NULL,
    fuel_cost REAL NOT NULL,
    lease_costs REAL NOT NULL,
    maintenance_costs REAL NOT NULL,
    loan_payments REAL NOT NULL,
    corporate_tax REAL NOT NULL,
    net_income REAL NOT NULL,
    cash_end_of_week REAL NOT NULL
);

-- Per-step settlement markers so a crash mid-week can retry without double-charging
CREATE TABLE IF NOT EXISTS settlement_flags (
    game_week INTEGER PRIMARY KEY,
    cash_applied INTEGER NOT NULL DEFAULT 0,
    banking_done INTEGER NOT NULL DEFAULT 0,
    post_ops_done INTEGER NOT NULL DEFAULT 0,
    loan_collected REAL NOT NULL DEFAULT 0
);

-- Phase 8: weekly fuel market OHLC (written at settlement)
CREATE TABLE IF NOT EXISTS fuel_price_history (
    game_week INTEGER PRIMARY KEY,
    open_price REAL NOT NULL,
    high_price REAL NOT NULL,
    low_price REAL NOT NULL,
    close_price REAL NOT NULL,
    shock_event TEXT
);

-- ============================================================================
-- INDEXES for frequently queried fields
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_airports_category ON airports(category);
CREATE INDEX IF NOT EXISTS idx_fleet_status ON fleet(status);
CREATE INDEX IF NOT EXISTS idx_fleet_current_airport ON fleet(current_airport_iata);
CREATE INDEX IF NOT EXISTS idx_flight_segments_week ON flight_segments(game_week);
CREATE INDEX IF NOT EXISTS idx_flight_segments_tail ON flight_segments(tail_number);
CREATE INDEX IF NOT EXISTS idx_event_log_week ON event_log(game_week);

-- Runway slot caps (hourly movements). Independent of apron/gate concurrency.
CREATE TABLE IF NOT EXISTS slot_controlled_airports (
    iata TEXT PRIMARY KEY,
    declared_hourly_cap INTEGER NOT NULL,
    slot_season TEXT NOT NULL DEFAULT 'IATA',
    effective_week INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS slot_allocations (
    allocation_id TEXT PRIMARY KEY,
    airport_iata TEXT NOT NULL,
    holder_id TEXT NOT NULL,
    game_week INTEGER NOT NULL,
    slots_held INTEGER NOT NULL DEFAULT 0,
    used_this_week INTEGER NOT NULL DEFAULT 0,
    below_threshold_weeks INTEGER NOT NULL DEFAULT 0,
    UNIQUE(airport_iata, holder_id, game_week)
);

CREATE TABLE IF NOT EXISTS slot_usages (
    usage_id TEXT PRIMARY KEY,
    airport_iata TEXT NOT NULL,
    holder_id TEXT NOT NULL,
    game_week INTEGER NOT NULL,
    segment_id TEXT NOT NULL,
    movement_type TEXT NOT NULL,
    clock_hour INTEGER NOT NULL,
    UNIQUE(segment_id, movement_type)
);

CREATE INDEX IF NOT EXISTS idx_slot_usages_airport_week ON slot_usages(airport_iata, game_week, clock_hour);
