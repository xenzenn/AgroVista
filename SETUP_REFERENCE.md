# AgroVista: refresh online tanpa upload database penuh setiap hari

Pakej ini belum dipasang pada akaun GitHub/Streamlit anda. Ia mengandungi kod yang siap diuji secara local, kedua-dua model, scope, dan workflow. Pemasangan serta run GitHub Actions pertama masih diperlukan.

## 1. Copy fail ke repo yang betul

Extract ZIP ini ke folder sementara. Copy **isi folder pakej**, termasuk folder `.github`, `models` dan `data/analysis/scope`, ke:

`D:\UITM DEGREE\FINAL YEAR PROJECT\AgroVista_V2\AgroVista_Online`

Ini folder repository yang digunakan oleh GitHub Desktop. Pastikan `app.py` berada terus di root repo, bukan satu folder tambahan di dalamnya.

- Merge folder; jangan delete folder `assets`, `.streamlit` atau fail lain.
- Replace fail `.py` yang mempunyai nama sama dengan versi pakej ini.
- Kekalkan `requirements.txt` sedia ada. `requirements-refresh.txt` hanya untuk GitHub Actions.
- **Jangan copy database DuckDB local ke repo.** Release `data-initial` yang sedia ada dikekalkan.
- Jangan gantikan skrip dalam projek FYP local asal. Pakej ini untuk `AgroVista_Online`.
- Tambahkan rules dalam `.gitignore.refresh.example` ke `.gitignore` sedia ada. Jangan commit secrets sebenar.

Dalam GitHub Desktop, pastikan fail berikut muncul dalam Changes:

- `.github/workflows/refresh-data.yml`
- `app.py`, `cloud_refresh_ui.py`, `cloud_updates.py`, `update_bundle.py`
- `run_cloud_refresh.py`, tiga `refresh_*.py`, `agrovista_horizon_models.py`
- `requirements-refresh.txt`
- `models/agrovista_per_horizon_candidates/state_level/selected_direct_model.joblib` dan `manifest.json`
- `models/agrovista_per_horizon_candidates/premise_level/selected_direct_model.joblib` dan `manifest.json`
- `data/analysis/scope/state_scope.csv`

Kalau folder `models`/scope tidak muncul, periksa `.gitignore` repo. Fail model mesti benar-benar masuk ke GitHub. Kedua-dua model berjumlah kira-kira 9.5 MiB.

Tulis commit `Add cloud data refresh` → **Commit to main → Push origin**.

## 2. Jalankan refresh pertama dari GitHub

Buka https://github.com/xenzenn/AgroVista/actions

Pilih **Refresh dashboard data → Run workflow → branch main → Run workflow**.

Run pertama akan:

1. Semak model dan manifest serta dependency yang sesuai.
2. Download database asas `data-initial` di server GitHub, bukan dari laptop anda.
3. Jika ada, pulihkan update terakhir yang telah berjaya.
4. Download harga terkini dan lookup, refresh weather/CPI/calendar, kemudian jana forecast.
5. Bina serta semak Market Watch, forecast, dan bundle update.
6. Upload archive update dahulu, kemudian manifest yang menandakan update lengkap.

Tunggu tick hijau. Run pertama boleh mengambil masa kerana server perlu mendapatkan database asas dan weather cache. Jangan upload database penuh semula sendiri. Masa dan saiz update sebenar akan ditunjukkan dalam log / job summary; belum diukur pada database sebenar anda di sini.

Workflow mencipta release **Dashboard data updates** (`data-updates`) secara automatik. **Jangan padam release `data-initial`.**

Kalau run merah, buka step **Refresh and publish update** dan salin error terakhir. Dashboard kekal menggunakan data terakhir yang berjaya; job gagal tidak menerbitkan update baharu.

## 3. Aktifkan auto-download update di Streamlit

Dalam Streamlit Community Cloud, buka **Manage app → Settings → Secrets**. Jika nama menu berbeza, cari editor Secrets bagi app tersebut.

Tambah seksyen ini tanpa memadam secrets lain:

```toml
[cloud_refresh]
enabled = true
admin_password = "GANTI_DENGAN_PASSWORD_SENDIRI_MINIMUM_16_AKSARA"
github_token = "TOKEN_AKAN_DIISI_DALAM_LANGKAH_4"
```

Tukar password kepada password sendiri. Jangan masukkan secrets ini ke fail yang di-commit ke GitHub.

Save. Buka semula dashboard. App akan menyemak update yang diterbitkan setiap lima minit ketika digunakan, dan memasukkannya ke DuckDB sebelum memuatkan page. Pengguna tidak perlu tahu password untuk menerima data yang telah diterbitkan.

Boleh aktifkan `enabled = true` walaupun token belum siap: menerima data tidak memerlukan token untuk repo public ini. Token hanya diperlukan untuk butang admin yang mencetuskan workflow.

## 4. Tambah butang admin dalam dashboard

Pada akaun GitHub anda:

**Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.

- Resource owner: akaun `xenzenn`.
- Repository access: **Only select repositories → AgroVista**.
- Repository permissions: **Actions → Read and write**.
- Pilih tarikh expiry yang sesuai.
- Generate, kemudian copy token ke `github_token` dalam Streamlit Secrets tadi.

Jangan hantar token/password ke chat atau commit ke repo. Tidak perlu menambah token ini dalam GitHub Actions secrets: workflow menggunakan `GITHUB_TOKEN` automatik.

Selepas Save, di bahagian bawah dashboard buka **Data updates · Admin**. Masukkan password anda → **Update data**. Butang itu mencetuskan kerja di server; ia tidak bermaksud forecast siap serta-merta.

Gunakan **View refresh progress** untuk membuka log. Selepas job berjaya, tekan **Check refresh status / load published data**, atau biarkan semakan automatik memuatkan update semasa app digunakan.

## 5. Refresh harian

Workflow disertakan jadual harian **09:17 pagi Malaysia**. Masa mula boleh lewat bergantung pada GitHub. Laptop tidak perlu hidup.

Kalau hanya mahu manual, buang blok `schedule` daripada `.github/workflows/refresh-data.yml`, kemudian Commit dan Push.

## Apa sebenarnya yang dihantar?

Database asas besar kekal di release sedia ada. Setiap run menghasilkan snapshot **kumulatif sejak bulan sebelum snapshot asas**, bukan sekadar append harga sehari. Ini membolehkan pembetulan harga bulanan, kemas kini lookup, serta pemulihan walaupun app tertinggal beberapa hari.

Archive mengandungi:

- Harga dalam tempoh update, lookup terkini dan optional state medians.
- Forecast semasa dan metadata batch.
- Table diagnostik untuk weather, CPI dan festival.
- CSV external kecil untuk cache inference server; dashboard masih membaca table DuckDB.

Raw monthly downloads, keseluruhan sejarah lama dan fail model tidak dihantar ke app setiap hari. Model dimuat naik sekali dalam repo; model hanya perlu dikemas kini apabila dilatih semula.

Harga bulanan masih dibaca semula di server kerana sumber asal mungkin membuat pembetulan. Jadi fail update bukan semestinya beberapa kilobyte. Saiznya bergantung pada data dan akan bertambah dari masa ke masa. Had transfer pakej ini ialah 512 MiB; jika akhirnya melebihinya, bina checkpoint/database asas baharu secara berkala, bukan membuang sejarah secara senyap.

## Ujian yang sudah dilakukan

- Kedua-dua model sebenar: manifest/contract dan prediction 21 horizon.
- Full forecast refresh dengan model sebenar pada fixture: 14 rows, 2 level, 7 tarikh; read-back dan Market Watch lulus.
- Import harga pada schema deployment tanpa `clean_prices`.
- Bundle apply, ulang apply tanpa duplikasi, pemeliharaan sejarah, pembetulan metadata lookup.
- Checksum rosak, path traversal, forecast invalid, rollback transaksi dan dua pemohon serentak.
- External refresh dengan respons sumber simulasi, holiday sebenar library dan penggunaan semula cache weather.
- Admin password, dispatch simulasi, cooldown dan status rerun.
- Semua route app, katalog/search, quantity/removal dan warm navigation pada fixture.

**Belum diuji:** job di akaun GitHub anda, data rasmi live pada skala 80 juta rows, masa refresh sebenar, dan pemasangan secrets. Tiada tindakan publish atau perubahan akaun dibuat dari sesi ini.

## Perubahan terhadap skrip local

- `refresh_pricecatcher.py`: `clean_prices` kini optional supaya database deployment tidak perlu menggandakan seluruh sejarah.
- `refresh_external_data.py`: CPI menggunakan Parquet rasmi untuk mengelakkan response API terpotong; Market Watch membaca cache weather lengkap termasuk suhu/kelembapan.
- `refresh_forecasts.py`: model, horizon, feature, scope dan policy asal dikekalkan. Satu variable local yang tidak digunakan dibuang.
- Refresh menggunakan Python 3.12 dan scikit-learn 1.9.1 bagi keserasian artifact anda; inference tidak dijalankan dalam proses Streamlit.

## Rujukan teknikal

- GitHub dispatch dan Actions write permission: https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event
- Workflow schedule/concurrency/permissions: https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
- Release asset uploads: https://docs.github.com/en/rest/releases/assets#upload-a-release-asset
- Fail rasmi CPI: https://data.gov.my/data-catalogue/cpi_state
