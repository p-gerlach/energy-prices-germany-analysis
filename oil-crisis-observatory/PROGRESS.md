# Progress log (resume point after a context reset)

## Environment facts (2026-10-05, cloud build session)
- The cloud session's egress policy returned **HTTP 403 on CONNECT for every data host** (EIA, ECB, EC, JODI, PortWatch/arcgis,
  GDELT, FIRMS, CDSE, CEMS, Eurostat, SEC, Natural Earth, RSS publishers). Only PyPI and raw.githubusercontent.com were reachable.
  => No live smoke test could succeed from the build session. Live verification must be run on the user's machine with
  `oco doctor --live` (or after the environment's network allowlist is extended).
- PortWatch schema confirmed from the World Bank notebook source (raw.githubusercontent.com): fields date (epoch ms, local-midnight
  shifted), year/month/day, portid (chokepointN), portname, n_tanker, n_total, n_cargo, capacity_* ; pagination 1000 rows.

## Done
- Section 0: config/access_policy.yaml (deny-by-default, exact host/path/method routes, excluded hosts), policy loader
  that refuses payment/trial/credit flags or an allow_paid switch, GuardedClient (pre-request block, redirect validation,
  credential stripping, 402/billing wording/unexpected auth => persistent stop, 429 => backoff+pause, HTML/content-type
  validation, size caps, circuit breaker, redaction). 34 policy tests pass.
- Storage: immutable content-addressed raw files + manifest; versioned observations (new/revised/unchanged, superseded
  history, as-of vintage queries); SQLite job/connector/health state; separate review DB for the dashboard; single-writer lock;
  atomic read-only snapshot for the dashboard.
- Collectors: ECB, EIA (manifest validation via facets, pagination, key scrubbed from stored JSON), EU Oil Bulletin
  (link discovery, two layouts, unit detection, magnitude guard, SchemaChanged on mismatch), PortWatch (metadata-resolved schema,
  chokepoint ids by name, pagination, revision overlap), GDELT (window splitting, rate-limit text), RSS (per-feed admission),
  manual headlines, JODI (bounded ZIP stream, status codes, balanced-sample helper), FIRMS (verified facilities only),
  CDSE OData catalogue, optional SEC/Comext.
- Analysis: derived Brent EUR (same-date ECB, ≤3-day fallback), EUR/L, taxes component, cost wedge (labelled not a margin);
  anomaly rules price_daily, price_weekly, inventory_week (seasonal), shipping_7d (fixed baseline, ≥6/7 days, persistence),
  fuel_lag_resid (exploratory); idempotent storage with supersession; replay (--as-of); backtest with vintage caveat.
- Cards: headline→indicators, anomaly→reporting, context/normal cards; claims tested per indicator type; temporal mismatch;
  contrary observations; alternatives; separate quality/freshness/relevance; content-hash versioning; template narrative +
  numeric/causal validator; optional local-LLM (loopback only) draft validated.
- Exports: 16:9 PNG/SVG, CSV of plotted data with version ids, source note; unit guard for snapshot vs transits.
- Satellite: thermal events (overpass/event dedupe, routine flare, site baseline, review vocabulary), coverage report,
  comparability rules, CDSE bounded download with budgets, Natural Earth land mask reader, S2 before/after (SCL quality,
  same stretch, native resolutions, radiometric check, dNBR candidate change), S1 CFAR (calibration, range noise, GCP geoloc),
  CEMS manual import.
- Scheduler (foreground, PID file, SIGTERM stop), release-aware schedule (EIA ET/DST/holidays, bulletin Thursday, ECB).
- CLI, Streamlit dashboard (7 tabs, AppTest passes on demo data), demo fixtures (data_demo/, labelled SYNTHETIC).

## Verification results (2026-10-05)
- `pytest`: 94 passed (cost policy, storage/revisions, anomalies, collectors on mocked responses, schedule/DST/holidays,
  cards/narrative validation, exports, satellite logic on synthetic rasters, notebook on demo data).
- `oco doctor --live`: all anonymous connectors `unavailable` (egress 403 in build session), EIA/FIRMS/CDSE download
  `unconfigured`, local_llm/eia_release `documented_free`; cloud env vars detected and ignored.
- `oco refresh` live: every source reported its reason; nothing stored; no connector stopped.
- Dashboard: Streamlit AppTest + real server screenshots (demo data and live health page); Deploy button hidden.
- Scheduler: started in background, stopped via `oco stop-scheduler`, PID file removed.
- Bugs found by tests and fixed: invalid tz `Europe/Frankfurt`; RSS duplicate erased excerpt; CFAR k-sigma false alarms
  (replaced by gamma-PFA CFAR); card version churn from volatile fields; mixed-claim headlines.

## Outstanding / next
- Live smoke tests: BLOCKED in build session (egress 403). Run `oco doctor --live` and `oco refresh` locally.
- If live Oil Bulletin / JODI layouts differ from the documented ones, collectors report `schema_changed`/`denied` —
  adjust parser or policy route after inspecting the real file (explicit review).
- Real Sentinel-1/-2 products never processed (no credential/network); S1 detection remains experimental.

## Blockers
- Network egress from build session (see above).
- Free credentials not supplied: EIA_API_KEY, FIRMS_MAP_KEY, CDSE_USERNAME/PASSWORD (user must obtain; never auto-registered).
- Facilities have no verified boundaries (deliberately empty — must be traced by the user from public sources).
