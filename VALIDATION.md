# Checked refresh validation

Environment: Python 3.12, Streamlit 1.65.0, scikit-learn 1.9.1; actual uploaded model artifacts.

Passed tests:

1. Seed variants: LF, CRLF, UTF-8 BOM, legacy raw digest, missing companion with pinned digest, uppercase/BOM checksum, and no final newline.
2. Actual local Git repository with core.autocrlf=true: commit a CRLF seed, clone with LF checkout, verify old checksum compatibility and identical data.
3. Reject altered values, malformed/empty checksum, missing required seed, duplicate dates, missing state/day, invalid humidity/rain, NaN and wrong columns.
4. Preflight: missing seed/model and corrupt scope fail BEFORE database download; correct files pass.
5. curl HTTP 503 retry, permanent 403 stop, and exhausted TLS/transport failures; no insecure TLS flags.
6. Actual uploaded 18,272-row seed, 16 states, daily continuity; only four missing days/state requested under simulated sources.
7. Retry exhaustion, 800-day chunks, checkpoint recovery and no partial publication.
8. External weather/CPI/calendar refresh with simulated source responses; holiday subdivisions for all 16 states/territories.
9. Both real model manifests/contracts and predictions over 21 horizons.
10. Full inference script on synthetic source data with ACTUAL models and BOM-prefixed manifests: 14 forecasts, 2 levels, 7 dates, write/read-back and Market Watch.
11. Price importer without duplicate clean_prices, and lookup column order normalisation.
12. Bundle idempotence, historical preservation, lookup corrections, corrupt checksum/path rejection, semantic validation, rollback and concurrent application.
13. GitHub transport simulation: no incomplete publication, archive-before-manifest ordering, upload failure stop, active-job dispatch guard.
14. Admin password, dispatch cooldown and status rerun.
15. Full Home/Overview/Analysis/Basket and catalog regression suite on fixture; quantity/removal; repeated warm navigation with zero new database queries.
16. Python compilation and pyflakes.

Not verified: live new GitHub Actions run after these changes, account permissions, provider connectivity from that runner, and nationwide full-scale memory/time. A real cloud acceptance run is still required. No production data or account settings were modified by these tests.
