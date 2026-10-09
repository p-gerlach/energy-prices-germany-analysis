# Oil Crisis Observatory

A local research tool for a solo political YouTube creator. It collects the newest **free** data on oil markets,
European fuel prices, Strait of Hormuz shipping, satellite coverage and refinery heat signals. It flags unusual
changes with transparent statistics, connects measurements to news headlines in both directions, and produces
reproducible 16:9 charts, CSVs and evidence cards.

It is designed to **cost nothing**. It never creates accounts, never touches payment, trials or processing
credits, and runs entirely on your computer (see [Zero-charge safeguards](#zero-charge-safeguards)).

> **Status (5 Oct 2026):** live-tested. ECB, EIA, the EU Oil Bulletin, PortWatch, JODI, RSS, NASA FIRMS (key check),
> the Copernicus catalogue and an authenticated Sentinel-1 download all worked with real data. GDELT was rate-limited
> from the cloud test machine. Sentinel-1 vessel counts remain experimental. Details: `PROGRESS.md`.

---

## 1. Install (once)

You need Python 3.11 or 3.12. The commands below use [uv](https://docs.astral.sh/uv/), which installs exactly the
locked versions. Plain `pip` works too.

```bash
cd oil-crisis-observatory
uv sync --extra satellite          # economic + news + satellite tools (rasterio, shapely, scipy)
# or, without the optional satellite tools:
uv sync
# without uv:  python -m venv .venv && .venv/bin/pip install -e ".[satellite]"
```

Every command below is run as `uv run oco …`. If you activated `.venv` yourself, plain `oco …` works too.

## 2. Configure (once, all free)

```bash
cp .env.example .env
```

Open `.env` and fill in what you have. Everything is optional: connectors without a key stay visibly
**unconfigured**, and everything else keeps working.

| Variable | What it unlocks | Where you get it (free, you register yourself) |
|---|---|---|
| `OCO_CONTACT_EMAIL` | polite User-Agent (needed for SEC EDGAR) | your email |
| `EIA_API_KEY` | Brent/WTI, US stocks, refinery utilisation | https://www.eia.gov/opendata/register.php: enter your email; the key arrives by email |
| `FIRMS_MAP_KEY` | NASA thermal detections near refineries | https://firms.modaps.eosdis.nasa.gov/api/map_key/: enter your email; the MAP_KEY arrives by email |
| `TANKERKOENIG_API_KEY` | live German pump prices (every 10 minutes, ~100 stations in 10 cities) | https://creativecommons.tankerkoenig.de/: request a free personal key; data is CC BY 4.0 (MTS-K). The public demo key is refused |
| `CDSE_USERNAME` / `CDSE_PASSWORD` | raw Sentinel-1/-2 downloads (username = the **e-mail address** you registered with) | https://dataspace.copernicus.eu/: "Register" creates a free **General User** account. Do **not** buy or activate processing units, Sentinel Hub plans or paid extensions; the app only uses raw downloads |

The app will **never** register for you, read cloud or billing credentials (AWS, Google, ArcGIS…), or ask for
payment details.

## 3. First run

```bash
uv run oco doctor --live           # one bounded request per connector -> data/reports/connector_verification.md
uv run oco news verify-feeds       # admits only RSS feeds that answer anonymously with valid RSS
uv run oco refresh                 # ECB, EIA, Oil Bulletin, PortWatch, JODI, GDELT, RSS (incremental)
uv run oco backfill --source portwatch --source ecb_fx   # bounded history (start dates in config/sources.yaml)
uv run oco analyse                 # derived series, anomaly screening, thermal events, evidence cards
uv run oco dashboard               # opens http://127.0.0.1:8501 ; stop with Ctrl+C
```

## 4. Everyday commands

| Task | Command |
|---|---|
| What is configured and reachable? | `uv run oco doctor` (add `--live` to test connectors) |
| Source freshness | `uv run oco status` |
| Update everything once | `uv run oco refresh` |
| Update one source | `uv run oco refresh --source oil_bulletin` |
| Bounded backfill | `uv run oco backfill --source eia_weekly` / `--source gdelt --days 7` |
| Recompute analysis | `uv run oco analyse` (replay a past vintage: `--as-of 2026-10-01T12:00`) |
| List evidence cards | `uv run oco cards` |
| Add a headline by hand | `uv run oco news add --url URL --title "…" --published 2026-10-05T08:00` then `oco analyse` |
| Export a card (16:9 PNG/SVG + CSV + notes) | `uv run oco export --card-id h-1234abcd` |
| Export any chart | `uv run oco export --series eia.brent_spot,eia.wti_spot --title "Brent vs WTI" --start 2026-01-01` |
| Rule backtest (alert frequency) | `uv run oco backtest --series eia.brent_spot --rule price_daily --start 2025-01-01 --end 2025-12-31` |
| Re-enable a stopped connector after reviewing why | `uv run oco connectors reset portwatch` |
| "What is unusual this week?" digest | `uv run oco digest` (Markdown story list from current alerts and analyses) |
| Rebuild the research page | `uv run oco build-page` → `data/exports/observatory.html` (also rebuilt after every `analyse`) |
| Pick the Tankerkönig station panel (once, after adding the key) | `uv run oco tankerkoenig build-panel` |

### The world shipping map

The first view of the page is a dashboard built around a world map of all 28 chokepoints IMF PortWatch tracks
(Hormuz, Suez, Bab el-Mandeb, Malacca, Panama, the Danish straits …). Circle size = vessel transits per day
(7-day mean); colour = change against the 2025 average. Press play to watch 2019 → today, or jump to an event.
Click a circle (or a table row) to see that chokepoint's daily transits by vessel type, its trend, and its vessel
mix. Gulf oil ports (Yanbu, Fujairah, Ras Tanura …) are shown as diamonds with their estimated tanker exports.

What it is not: there are **no individual ship positions or tracks**. Free sources only give daily counts per
chokepoint; ship-by-ship AIS (MarineTraffic, Kpler, MyShipTracking) is paid. The grey lane lines are IMF's static
drawing of common routes, not measured traffic.

Fetch the map's reference shapes and full history once:

```bash
uv run oco backfill --source portwatch_geo --source portwatch    # ~10 minutes, anonymous, free
uv run oco build-page
```

Only the four oil-route chokepoints (Hormuz, Suez, Bab el-Mandeb, Cape of Good Hope) raise screening alerts; the
others are on the map for context (`screen: true` in `config/sources.yaml` changes that).

### Live ships (individual vessels, on your computer)

`oco ships` opens a live map of individual ships: arrows point along each ship's heading and glide to every new
position report; tap or click a ship for its name, type (tanker, cargo, passenger …), flag, speed, course,
destination, draught, size, MMSI and IMO. Filter by ship type or search by name/MMSI/IMO.

1. Create a free key yourself at https://aisstream.io/authenticate (sign in with GitHub → Account → API key).
   The app never registers for you.
2. Add it to `.env`: `AISSTREAM_API_KEY=...`
3. Run `uv run oco ships` (Gulf + Red Sea by default; `--area north_sea --area baltic`, or `--area world`).
   It opens http://127.0.0.1:8765. Add `--lan` to watch on your phone in the same Wi-Fi
   (http://<computer's IP>:8765). Stop with Ctrl+C. The dashboard's "Live ships" tab shows it too.

Why only on your computer: aisstream does not allow browser connections and the key must stay private, so the
shared web page cannot show live ships. Limits: terrestrial receivers only (ships far offshore are missing); ships
with AIS switched off (common in the Gulf now) are invisible; names and destinations are typed by crews and can be
wrong. MyShipTracking and MarineTraffic data APIs are paid and are blocked in the access policy.

### The research page (charts that always load)

`data/exports/observatory.html` is a single file with **no external resources**: the charts are drawn as SVG by a
small built-in script, so they show offline, inside the dashboard, and when the file is shared. It contains the story
finder plus these sections: market, Hormuz shipping, German fuel prices, live pumps (Tankerkönig), bypass ports
(Yanbu / Fujairah vs inside-Gulf terminals), rockets & feathers (asymmetric pass-through test), refining margins
(EIA crack spreads), tax take per litre, and Germany's crude imports & household energy prices (Eurostat).
The dashboard's first tab embeds it and reloads by itself when new data is published.

### Activating live pump prices

1. Put your key in `.env` as `TANKERKOENIG_API_KEY=...`
2. `uv run oco tankerkoenig build-panel` (one-off station list for 10 cities)
3. `uv run oco run-scheduler` — prices are read every 10 minutes while it runs; completed days become observations.

Exports go to `data/exports/<card>/`: a 1920×1080 PNG and SVG per chart, a CSV of exactly the plotted
observations (with version ids), a `.md` source and method note, and `card.md`/`card.json`.

### Continuous collection (start / stop)

```bash
uv run oco run-scheduler           # foreground; follows release calendars (EIA Wed 10:30 New York, Bulletin Thu, ECB ~16:00 CET)
# stop: press Ctrl+C in that window, or from another terminal:
uv run oco stop-scheduler
```

The scheduler is an ordinary program: **it collects only while it runs and the computer is awake.** A sleeping
laptop collects nothing. No background service is installed, and nothing keeps running after it exits. For
unattended collection, run it on a machine that stays on.

### Practice with fake data (clearly labelled)

```bash
uv run oco demo-build              # writes SYNTHETIC fixtures to data_demo/ (never mixed with real data)
uv run oco --demo dashboard        # red "SYNTHETIC — NOT LIVE" banner on every page; exports are watermarked
```

## 5. Satellite workflow (optional, experimental parts marked)

1. **Facilities.** `config/facilities.geojson` deliberately has **no coordinates**. For each refinery you want to
   follow, trace its boundary from a public source (e.g. the OpenStreetMap industrial outline), add the links to
   `source_links`, and set `"verified": true`. Unverified sites are skipped, and the app tells you so.
2. **Study areas.** `config/regions.geojson` holds coarse, editable Hormuz and anchorage polygons. Adjust them
   against a public chart before publishing anything.
3. **Coverage first:** `uv run oco refresh --source cdse` and then `uv run oco satellite coverage`. These report
   real scene dates, gaps, orbits, polarisation and cloud cover. Catalogue access alone does **not** mean products
   are downloadable.
4. **Bounded download** (needs your free CDSE login): `uv run oco satellite download --product-id <Id>`. This is
   capped by `budgets` in `config/sources.yaml` (default 8 GB per 30 days, 20 GB disk).
5. **Refinery before/after (Sentinel-2):**
   `uv run oco satellite compare-s2 --facility <id> --pre <Id1> --pre <Id2> --post <Id3> --event-date 2026-10-01`.
   The comparison is refused when clouds, non-overlapping tiles or radiometric differences make it invalid.
6. **Vessel snapshots (Sentinel-1, EXPERIMENTAL):** `uv run oco satellite detect-s1 --product-id <Id> --aoi hormuz_strait`,
   then `oco satellite compare-s1 …`. Each result is a count *in one radar snapshot*. It is never daily transits
   and never vessel tracks. Review detections with `oco satellite review-detection <id> --confirm/--reject`.
   Precision is reported only from your reviewed sample.
7. **Thermal (FIRMS):** `uv run oco refresh --source firms` and then `oco analyse`. Events are classified as
   `routine_flaring_pattern`, `within_site_thermal_baseline`, `thermal_anomaly_near_facility` or
   `insufficient_baseline`. These are review items, never "destroyed".
8. **Copernicus EMS maps** you downloaded yourself: `uv run oco satellite cems-import --file … --activation EMSR… …`.

## 6. How the analysis works (short)

* **Three layers:** immutable raw files (sha256-addressed, read-only), versioned observations (every revision is a
  new vintage; nothing is overwritten), and derived findings (anomalies, cards) tied to exact input version ids.
* **Dates:** observation date, provider publication date (only when the provider states it) and retrieval date
  are stored separately. Missing values stay missing; they are never zero.
* **Anomaly screening:** robust z = 0.67449·(x − median)/MAD on log returns or changes, excluding the current
  point and all later data. It fires only if |z| ≥ 3.5 **and** an economic threshold is met. With too little
  history the score is suppressed; when MAD = 0 an IQR fallback is used, or the score is suppressed. Inventories
  use same-week seasonal baselines. Shipping uses a **fixed** baseline window that you choose
  (`shipping_baseline` in `config/sources.yaml`) plus quantiles, needs at least 6 of 7 observed days and 2 days
  of persistence. Fuel prices use an exploratory lag-model residual. These thresholds are screening rules, not
  probabilities.
* **Euro conversions:** Brent EUR/bbl = Brent USD ÷ ECB USD-per-EUR (same date). Oil Bulletin prices in
  EUR/1000 L ÷ 1000 = EUR/L. A bulletin Monday price is compared with mean Brent EUR/L over the previous
  Monday–Friday. The crude-to-retail spread is a **cost wedge, not a profit margin**.
* **Evidence cards** state the question tested and record:
  * the measurements with their observation periods, the baseline and the change;
  * a relationship: `supports`, `complicates`, `insufficient_evidence` or `context_only`;
  * contrary observations, alternative explanations and limitations;
  * evidence quality, freshness and editorial relevance as separate ratings.

  If every observation predates the headline, the card says *insufficient evidence (temporal mismatch)*.
  Nearby headlines are only ever "context", never a cause.
* **Narratives** come from a template. Every number in them is checked against the calculations, and causal
  wording sends a card to review. An optional local model (Ollama on 127.0.0.1, `OCO_LOCAL_LLM_MODEL`) may draft
  wording, which goes through the same checks. There is never a remote model.
* **News is untrusted data:** only the title, link and a short feed excerpt are stored. It is never executed or
  followed as instructions.

## Zero-charge safeguards

* `config/access_policy.yaml` is **deny by default**. Each connector lists its exact host, path pattern and HTTP
  method, plus free-access evidence and the date it was checked. PortWatch is restricted to the single IMF
  `Daily_Chokepoints_Data` layer (no arcgis.com wildcard, no `token`). Sentinel Hub, openEO, CREODIAS, BigQuery,
  ArcGIS services, commercial tiles and geocoders, and remote LLMs are listed as excluded.
* Every request goes through `GuardedClient`:
  * Unapproved URLs and redirects are blocked **before** sending, and credentials are only sent on approved hosts.
  * HTTP 402, "trial expired", "insufficient credits", billing pages or an unexpected login demand **stop** the
    connector, which stays stopped until you reset it.
  * HTTP 429 only backs off and pauses.
  * HTML error pages served with HTTP 200 are rejected, and size limits and a circuit breaker apply.
* There is no `allow_paid` switch and no cloud fallback. The policy loader rejects any connector that declares
  payment, a trial or credit dependence.
* Satellite downloads are capped by a 30-day download budget and a disk budget. All processing is local.
* The tests include a static scan of the source code for account-creation, billing and cloud-provisioning code.

## What is verified and what is not

See `PROGRESS.md` and `data/reports/connector_verification.md`. In short:

* **Tested offline (mocked provider responses and synthetic rasters):**
  * the policy enforcement;
  * every parser, including pagination, revisions, deduplication, unit conversion and schema-change detection;
  * scheduling across daylight-saving changes and holidays;
  * the anomaly edge cases, the cards, the exports, the satellite comparison rules, CFAR and the thermal logic.
* **Not yet tested against live providers:** none of the connectors has fetched live data.
  * The Oil Bulletin workbook layout and the JODI download path were coded from documentation and refuse to
    ingest anything they do not recognise. If the real files differ, `oco refresh` reports `schema_changed` or
    `denied` and stores nothing.
  * Sentinel processing has only run on synthetic files. Validate it on real products before relying on it.
    Sentinel-1 detection stays experimental until a reviewed sample exists.

## Licences and attribution

The source attribution and licence for each connector are stored in `config/access_policy.yaml` and printed in
every export note. Copernicus outputs carry "Contains modified Copernicus Sentinel data [year]".
