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

## Live verification (2026-10-05, after the user allow-listed the data domains)
- doctor --live: ECB, Oil Bulletin, PortWatch, RSS (tagesschau, BBC), Copernicus catalogue, Natural Earth = anonymous_read_tested;
  EIA, FIRMS = credential_read_tested; CDSE login accepted (username must be the registration e-mail, not the account UUID);
  CDSE authenticated download TESTED (S1C GRDH COG 2026-09-25, 809,400,792 bytes = catalogue size).
- Backfill/refresh stored real data: ECB 3,010 obs (to 2026-10-05); EIA spot 5,930 (to 2026-09-29); EIA weekly 5,517 (week to 2026-09-25);
  PortWatch 45,232 (to 2026-09-27); Oil Bulletin 32,076 (to 2026-09-28); JODI 3,800 (to 2026-07); RSS 70 headlines; CDSE 354 scenes.
- Real finding surfaced by the rules: Hormuz tanker transit calls fell from ~45-65/day to 0-5/day on 2026-03-01 and stayed near zero
  (7-day mean 1.3/day vs 2025 median 49.4); Brent monthly mean 70.9 (Feb) -> 117.3 USD (Apr) -> 114.1 (Sep); DE diesel 2.437 EUR/L (2026-09-28).
- Live fixes: JODI page now lists yearly CSVs (parser + policy route updated after review); CDSE firewall 403 ("rejected due to a
  violation") after a burst => now a 30-min pause + 3 s spacing, not a stop; GDELT returned 429 from the shared cloud IP (paused, retry later);
  topic matching false positives ("branded"/"rebrand" -> refinery, "Förderung" -> JODI) fixed with word-start matching + anchor words;
  anomaly cards now one per alert episode showing the latest reading; stale cards withdrawn (history kept); provider OAuth error text surfaced;
  CFAR threshold lookup vectorised (100M-pixel AOI).
- S1 detection ran on the real 2026-09-25 scene (2 min after fixing three performance hotspots): 189 unreviewed candidates in the
  hormuz_strait polygon, mostly along coasts/islets not resolved by Natural Earth -> NOT usable as vessel counts until a finer
  coastline and a reviewed sample exist. GDELT: still HTTP 429 from the cloud IP after the pause (paused again, no retry storm).
- Not reachable from the cloud session: rss.dw.com, spiegel.de, aljazeera.com, www.eia.gov (not allow-listed), data.sec.gov (optional).

## Second wave (2026-10-05)
- Self-contained research page (`oco.web`, SVG charts, no CDN) embedded as the first dashboard tab; verified in a
  browser with every external request blocked: 16 charts, 0 external requests, no overflow at phone width.
- New connectors: Tankerkönig (code done, waiting for the user's key), Eurostat (nrg_ti_oilm, prc_hicp_minr, tested
  anonymously), PortWatch daily ports, EIA product spot prices.
- New analyses (`analysis/extras.py`): US crack spreads, VAT/other tax per litre, rockets & feathers (Newey-West),
  Gulf import share, household HICP illustration; `analysis/digest.py` story finder.
- Findings on live data: diesel week-0 pass-through 140 % on rises vs 58 % on falls since 2015 (p speed = 0.014,
  no long-run difference); Yanbu exports surged Mar–Jul 2026 then collapsed Aug–Sep; VAT on diesel €0.389/L vs €0.256
  (2025 average); US diesel crack ≈ $96/bbl vs $29 median; car fuel HICP +26 % vs 2025 (Aug 2026).
- Tests: 102 passed.

## World shipping map (2026-10-06)
- PortWatch extended to all 28 chokepoints and all vessel types (container, dry bulk, general cargo, ro-ro, tanker);
  backfill 2019-01-01 → 2026-09-27: 588,016 new observations, 86 anonymous requests.
- New reference collector `portwatch_geo` (3 reviewed query-only routes: chokepoint and port locations, IMF shipping
  lanes) + Natural Earth land simplified locally. Stored in warehouse meta.
- Page redesigned as a dashboard: icon rail, KPI strip, world map with timeline playback and region zoom, alerts feed,
  stacked daily bars, trend line, vessel-mix donut, sortable 28-chokepoint table. Browser check offline: 0 external
  requests, 0 errors, no overflow (desktop light/dark, phone).
- Anomaly screening limited to `screen: true` chokepoints; earlier alerts on others marked `not_screened`.
- Tests: 106 passed.
- No individual vessel tracks: free per-ship AIS with history is not available on approved routes.

## Live ships + phone map (2026-10-06)
- MyShipTracking checked: credit-metered API, only a 10-day trial → excluded (with MarineTraffic API).
- New `aisstream` connector (free key the user creates; websocket route only, policy now supports `wss` routes) and
  `oco ships`: foreground local server (127.0.0.1, optional `--lan`), canvas map with ship arrows by class,
  click details, search, filters, pinch zoom. Key never reaches the browser. Provider errors stop the stream.
- Verified with a local fake stream (tests + headless browser, desktop/phone/dark); NOT verified against the real
  service: aisstream.io is not reachable from the cloud session and no key exists yet.
- Overview map: full-screen button, pinch zoom and drag on phones, labels constant size, no pulsing circles.
- Tests: 111 passed.

## Outstanding / next
- aisstream: user creates key, then `oco ships` live check.
- Tankerkönig: once the key arrives, `oco tankerkoenig build-panel` then `oco run-scheduler`.
- If live Oil Bulletin / JODI layouts differ from the documented ones, collectors report `schema_changed`/`denied` —
  adjust parser or policy route after inspecting the real file (explicit review).
- Real Sentinel-1/-2 products never processed (no credential/network); S1 detection remains experimental.

## Blockers
- GDELT throttles the shared cloud IP; expected to work from a home connection.
- Facilities have no verified boundaries (deliberately empty — must be traced by the user from public sources).
