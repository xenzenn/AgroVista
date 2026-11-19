# AgroVista checked refresh — gunakan pakej ini

Pakej ini menggabungkan refresh cloud, weather seed, checkpoint, retry/curl dan pembaikan checksum. Tidak perlu menggabungkan ZIP lama secara manual.

## Langkah sekarang

1. Extract ZIP ke folder sementara.
2. Copy **semua isi** ke folder repo `AgroVista_Online`. Merge folder dan replace nama fail yang sama. Kekalkan assets, `.streamlit`, `requirements.txt` serta secrets sebenar anda. Jangan copy database DuckDB local.
3. GitHub Desktop: pastikan perubahan `.github/workflows/refresh-data.yml`, `refresh_external_data.py`, `run_cloud_refresh.py`, `refresh_forecasts.py`, `weather_seed.csv` dan `weather_seed.sha256` masuk ke commit. File lain mungkin tidak muncul kerana isinya tidak berubah.
4. **Commit → Push origin**.
5. GitHub Actions → **Refresh dashboard data → Run workflow → main**. Mulakan run BAHARU, bukan Re-run job pada commit lama.

Step baharu **Validate inputs before downloading database** mesti lulus dahulu. Log:

```text
AgroVista checked refresh v3; commit: ...
Verified weather seed: 18272 rows, 16 states, through 2026-11-15 ...
PREFLIGHT PASSED: scripts, scope, weather seed, models and manifests.
```

Preflight tidak download database atau memanggil Open-Meteo. Jika ia gagal, mesej akan menyatakan fail/input yang tidak betul sebelum proses besar bermula.

## Checksum yang dibetulkan

Checksum lama dibuat atas bytes Windows CRLF. Git boleh menukarkannya kepada LF di server. Data masih sama tetapi raw checksum berbeza. Ini direproduce menggunakan commit dan clone Git dalam ujian.

Pengesahan sekarang menerima perubahan CRLF/LF, BOM UTF-8 dan terminal newline tanpa menerima perubahan pada nilai CSV. Checksum lama yang dibekalkan juga dikenali. Jika companion `.sha256` tertinggal, seed asal masih boleh disahkan menggunakan digest yang dipinkan dalam kod. Seed yang benar-benar berubah tetap ditolak; schema, states, tarikh, duplicates dan ukuran turut diperiksa.

Jangan edit seed melalui Excel atau rename fail. Versi dalam pakej ini sudah mempunyai format dan checksum yang betul. Fail input asal anda tidak diubah.

## Semakan tambahan

- Fail/script/scope/model/manifest yang hilang atau salah gagal sebelum download database.
- Scope dengan item code tidak sah atau tiada eligibility ditolak.
- Manifest model boleh menggunakan BOM UTF-8; artifact mesti sepadan dengan model/config/horizon sebenar.
- Cache checkpoint optional yang rosak diabaikan secara jelas dan seed/published cache digunakan. Published dataset yang rosak tidak dianggap valid.
- curl mengekalkan HTTP status: 429/503 boleh dicuba semula; 400/403 tidak diatasi dengan menukar provider atau mematikan TLS.
- Archive update yang rosak/incomplete tidak diterbitkan sebagai update berjaya.
- Model/horizon/feature/polisi forecast asal tidak ditukar.

## Apa yang tidak boleh dijamin

Semua ujian local dan simulasi yang disenaraikan dalam `VALIDATION.md` lulus. Itu bukan jaminan tiada sebarang error dalam semua keadaan. Akses Open-Meteo/DOSM/GitHub, permission akaun, masa proses dan memory pada database sebenar perlu disahkan pada run baharu. Jika sumber luar terus tidak boleh dicapai, workflow akan berhenti dengan jelas dan mengekalkan data dashboard terakhir, bukan mencipta weather atau forecast palsu.

Untuk pemasangan secrets/admin button yang belum dibuat, lihat `SETUP_REFERENCE.md`. Nota checksum dalam dokumen lama digantikan oleh penjelasan di atas.

Rujukan Git line endings: https://docs.github.com/en/get-started/git-basics/configuring-git-to-handle-line-endings
