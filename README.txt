Weather seed + alternative HTTPS transport

COPY ALL FILES FROM THIS ZIP into AgroVista_Online, preserving folder paths:
  refresh_external_data.py                 replace
  .github/workflows/refresh-data.yml       replace
  weather_seed.csv                        add at repository root
  weather_seed.sha256                     add at repository root

Commit and Push origin. Ensure both weather_seed files appear in GitHub Desktop.
Then Actions -> Refresh dashboard data -> Run workflow -> main -> Run workflow.
Start a NEW workflow, rather than rerunning an old commit.
Keep app.py, models, requirements and the existing database release unchanged.

YOUR UPLOADED SEED
18,272 rows: 16 states/territories, 1,142 consecutive dates per state.
2023-10-01 through 2026-11-15 inclusive. No nulls or duplicate state-date keys.
The seed is copied unchanged, with SHA256 integrity checking.
For a source ending 2026-11-19, only 2026-11-16..2026-11-19 must be fetched.
If the price source advances, additional missing dates will also be requested.

TRANSPORT
Normal Python HTTPS request is attempted first. On transport failure, the same
Open-Meteo archive endpoint can be requested with curl over IPv4 / HTTP 1.1.
A successful curl fallback becomes the preferred transport for the remaining
weather calls in that process. Certificate verification remains enabled.
No other weather provider, fake values or changed model is substituted.
There is no guarantee that curl can resolve a provider-side/routing outage.
If both transports fail, the run still stops, saves its completed progress when
GitHub cache is available, and does not publish a partial update.

EXPECTED LOG
Using verified local weather seed: 18272 rows, 16 states, through 2026-11-15
Weather Johor: requesting 2026-11-16 to 2026-11-19
If Python cannot connect:
Python weather connection failed; trying HTTPS curl over IPv4.

VALIDATION
Actual uploaded seed: columns, checksum, all states, daily continuity, missing
values and duplicates checked. An offline source simulation requested only the
four missing dates for each state and published 18,336 weather rows. Corrupt
seed rejected. Python handshake-to-curl fallback, successful transport reuse,
verified HTTPS command options, retry/chunk/checkpoint/resume regressions,
syntax and pyflakes passed. Actual GitHub connectivity is not yet verified.
