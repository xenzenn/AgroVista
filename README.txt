AgroVista — prepared Overview experiment

INSTALL FOR A REAL CLOUD TEST
1. Copy these FIVE Python files into your existing AgroVista_Online repository:
   app.py
   overview_compute.py         (new)
   overview_precompute.py      (new)
   run_cloud_refresh.py
   update_bundle.py
2. Commit ALL five files and Push origin in GitHub Desktop.
3. GitHub > Actions > Refresh dashboard data > Run workflow (main).
4. Wait for a green tick. The log now includes "Building prepared Overview" with
   per-area computation/read timings and the snapshot payload size.
5. Open the dashboard. Its normal update check fetches the new data bundle.
   If already open, reload after the normal five-minute update-check interval.
6. Compare the FIRST Market Overview visit, then switch market area and come back.

No manual database upload, no model changes, no new pip dependencies.
Do not replace requirements, assets, models, .streamlit or workflow YAML.
If refresh fails, the last successful data release remains available.

WHAT THIS TEST ACCELERATES
Prepared Overview stores existing totals, reporter counts, premise types, weekly
price medians/movers and Historical Price Pulse results in a versioned DuckDB table.
Calculation moves to the GitHub refresh; the app reads the prepared result.
Ordinary Streamlit caching still applies after that first read.
All state areas present in the production DB are prepared, plus Malaysia.
Missing, stale-key or malformed optional snapshots use the original calculations.
The new table travels inside the existing checked update bundle.

LIMITATIONS
This experiment targets the two heavy Overview sections, not all page rendering.
Initial database download, other queries, chart rendering, network latency and
catalog images are not eliminated by it. Cold/warm whole-page timings must be
measured on the real deployed application after the refresh.
Preparing all areas/history increases work during the GitHub refresh.
Existing historical prices and forecast algorithms are not changed.

VALIDATION COMPLETED
- Synthetic 640,000-row fixture; Malaysia plus two state areas.
- Original versus extracted calculation parity; prepared JSON round-trip parity.
- Weekly summary and monthly pulse values agree (numeric tolerance 1e-12).
- Missing/stale/malformed snapshots fall back.
- New table exports/imports through checked bundles; repeat imports are idempotent.
- Later bundles without prepared data remove the old optional table.
- Streamlit AppTest all pages, categories/search, menus and basket passed.
- Prepared Overview actually renders and bypasses the original heavy summary SQL.
- benchmark.json records median-of-three timings in this environment.

ROLLBACK
Restore the previous app.py, run_cloud_refresh.py and update_bundle.py from GitHub
Desktop History if this experiment is not wanted. The two new helper files can
remain unused. Existing app/database behavior remains the fallback.
