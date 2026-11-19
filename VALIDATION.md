# Validation results

Passed locally with Python 3.12 and Streamlit 1.65.0:

- Two actual uploaded model contracts/manifests; 21-horizon predictions each; sklearn 1.9.1.
- Full refresh_forecasts.py on synthetic observations and external inputs, actual uploaded models: 14 forecasts, two levels, seven dates; publish/read-back and Market Watch creation.
- Deployment-schema price importer without clean_prices; lookup column order normalisation.
- Cumulative bundle apply, idempotence, historical preservation, lookup corrections and schema checks.
- Corrupted checksum, invalid forecasts and archive traversal rejected; transaction rollback verified.
- Concurrent bundle application: only one commit.
- Mocked source refresh: weather, official CPI Parquet path, holiday library, external tables/files, cache reuse.
- All 16 Malaysia holiday subdivision codes accepted by the pinned library.
- Mocked GitHub transport: incomplete publication ignored, corrupt download discarded, temporary files removed, immutable archive published before manifest, upload failure stops publication, active job avoids repeated dispatch.
- Streamlit admin password gating, cooldown and status rerun with mocked dispatch.
- Home/Overview/Analysis/Basket, catalogs/search, menu addition, quantity/removal and repeated navigation on fixture; warm repeated navigation executed no new database queries.
- Workflow structure, compileall, pyflakes and dependency resolution dry run.

Not performed: real GitHub/Streamlit account configuration; workflow dispatch/publication on the user's account; live nationwide 80-million-row refresh; cloud transfer/latency measurements. Fixture tests do not establish production memory or time requirements. The first real workflow run remains the deployment acceptance test.
