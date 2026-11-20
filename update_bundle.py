"""Cumulative updates over the fixed initial DB; historical rows stay in DuckDB.
No arbitrary tables, SQL, archive paths or executable code are accepted from a bundle.
"""
from pathlib import Path
from datetime import date, datetime, timezone
from contextlib import nullcontext
import hashlib
import json
import re
import shutil
import tempfile
import zipfile
import duckdb
from cloud_database import DATABASE_SHA256

TABLES=('lookup_items','lookup_premises','clean_joined_prices','daily_state_prices',
        'agrovista_v2_forecast_current','agrovista_v2_forecast_batches',
        'diagnostic_weather_daily','diagnostic_holiday_events','diagnostic_food_cpi_monthly',
        'diagnostic_calendar_yearly','diagnostic_weather_full_summary','diagnostic_cpi_monthly_context',
        'agrovista_overview_precomputed_v1')
REQUIRED={'lookup_items','lookup_premises','clean_joined_prices','agrovista_v2_forecast_current',
          'agrovista_v2_forecast_batches','diagnostic_weather_daily','diagnostic_holiday_events',
          'diagnostic_food_cpi_monthly','diagnostic_calendar_yearly',
          'diagnostic_weather_full_summary','diagnostic_cpi_monthly_context'}
EXTERNAL=('weather_state_daily.csv','holiday_state_daily.csv','cpi_state_food_and_overall.csv','external_manifest.json')
STATE_TABLE='agrovista_cloud_update_state'

def sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(8*1024**2),b''):h.update(block)
    return h.hexdigest()

def quote(value):return "'"+str(value).replace("'","''")+"'"
def ident(value):return '"'+value.replace('"','""')+'"'

def connect(database,read_only=False):
    spill=Path(database).resolve().parent/'duckdb_spill'
    spill.mkdir(exist_ok=True,parents=True)
    return duckdb.connect(str(database),read_only=read_only,config={
        'memory_limit':__import__('os').getenv('AGROVISTA_DUCKDB_MEMORY','256MB'),
        'threads':1,'preserve_insertion_order':False,'temp_directory':str(spill),
        'max_temp_directory_size':'4GB'})

def current_state(database):
    with connect(database,True) as con:
        exists=con.execute('SELECT COUNT(*) FROM information_schema.tables WHERE table_name=?',[STATE_TABLE]).fetchone()[0]
        if not exists:return None
        row=con.execute(f'SELECT update_id,cutoff,source_last,forecast_today,generated_at FROM {STATE_TABLE} LIMIT 1').fetchone()
        return dict(zip(('update_id','cutoff','source_last','forecast_today','generated_at'),map(str,row))) if row else None

def initial_cutoff(database):
    state=current_state(database)
    if state:return state['cutoff']
    with connect(database,True) as con:
        last=con.execute('SELECT MAX(CAST(date AS DATE)) FROM clean_joined_prices').fetchone()[0]
    if not last:raise ValueError('Initial database has no price history')
    # Include previous month, because the existing importer revisits late corrections.
    y,m=last.year,last.month-1
    if not m:y,m=y-1,12
    return date(y,m,1).isoformat()

def validate_forecasts(con, expected_today=None):
    t='agrovista_v2_forecast_current'
    rows,dates,batches,first,last=con.execute(f'SELECT count(*),count(DISTINCT forecast_date),count(DISTINCT batch_id),min(CAST(forecast_date AS DATE)),max(CAST(forecast_date AS DATE)) FROM {t}').fetchone()
    if not rows or dates!=7 or batches!=1:raise ValueError('Forecast is empty, mixed-batch or not seven days')
    bad=con.execute(f'''SELECT COUNT(*) FROM (SELECT level,state,item_code,premise_code
        FROM {t} GROUP BY level,state,item_code,premise_code
        HAVING count(*)<>7 OR count(DISTINCT forecast_date)<>7
        OR count(*) FILTER(WHERE predicted_price>0 AND isfinite(predicted_price))<>7
        OR min(CAST(forecast_date AS DATE)-CAST(forecast_today AS DATE))<>0
        OR max(CAST(forecast_date AS DATE)-CAST(forecast_today AS DATE))<>6)''').fetchone()[0]
    levels={r[0] for r in con.execute(f'SELECT DISTINCT level FROM {t}').fetchall()}
    if bad or levels!={'state','premise'}:raise ValueError('Invalid complete state/premise forecast series')
    today=con.execute(f'SELECT DISTINCT CAST(forecast_today AS DATE) FROM {t}').fetchall()
    if len(today)!=1 or (expected_today and str(today[0][0])!=str(expected_today)):
        raise ValueError('Forecast date does not match requested run date')
    return str(today[0][0])

def build_bundle(database,destination,update_id,cutoff,external_dir=None):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',update_id):raise ValueError('Invalid update id')
    cutoff=date.fromisoformat(str(cutoff)).isoformat()
    destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='bundle_') as temp:
        temp=Path(temp);metadata={}
        with connect(database,True) as con:
            available={r[0] for r in con.execute('SHOW TABLES').fetchall()}
            if REQUIRED-available:raise ValueError(f'Missing update tables: {sorted(REQUIRED-available)}')
            forecast_today=validate_forecasts(con)
            source_last=str(con.execute('SELECT MAX(CAST(date AS DATE)) FROM clean_joined_prices').fetchone()[0])
            for table in TABLES:
                if table not in available:continue
                query=f'SELECT * FROM {ident(table)}'
                if table=='clean_joined_prices':query+=f' WHERE CAST(date AS DATE)>={quote(cutoff)}::DATE'
                file=temp/f'{table}.parquet'
                con.execute(f'COPY ({query}) TO {quote(file)} (FORMAT PARQUET, COMPRESSION ZSTD)')
                count=con.execute(f'SELECT COUNT(*) FROM ({query})').fetchone()[0]
                schema=[list(row[:2]) for row in con.execute(f'DESCRIBE SELECT * FROM {ident(table)}').fetchall()]
                metadata[table]={'rows':count,'sha256':sha256(file),'schema':schema}
        manifest={'protocol':1,'base_sha256':DATABASE_SHA256,'update_id':update_id,'cutoff':cutoff,
                  'source_last':source_last,'forecast_today':forecast_today,
                  'generated_at':datetime.now(timezone.utc).isoformat(),'tables':metadata,'external':{}}
        if external_dir:
            for name in EXTERNAL:
                source=Path(external_dir)/name
                if source.is_file():
                    target=temp/'external'/name;target.parent.mkdir(exist_ok=True)
                    shutil.copy2(source,target);manifest['external'][name]=sha256(target)
        (temp/'bundle.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
        with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED,compresslevel=3) as archive:
            for file in sorted(temp.rglob('*')):
                if file.is_file():archive.write(file,str(file.relative_to(temp)))
    return {**manifest,'archive':destination.name,'archive_sha256':sha256(destination),'archive_bytes':destination.stat().st_size}

def apply_bundle(database,archive,expected_sha256,lock=None,external_dir=None):
    archive=Path(archive)
    if sha256(archive)!=expected_sha256:raise ValueError('Update checksum mismatch; database unchanged')
    with tempfile.TemporaryDirectory(prefix='update_',dir=Path(database).parent) as temp:
        temp=Path(temp)
        with zipfile.ZipFile(archive) as zipped:
            entries=zipped.infolist()
            if len({e.filename for e in entries})!=len(entries):raise ValueError('Duplicate archive paths')
            if sum(e.file_size for e in entries)>4*1024**3:raise ValueError('Update archive too large')
            allowed={'bundle.json',*(f'{t}.parquet' for t in TABLES),*(f'external/{n}' for n in EXTERNAL)}
            if any(e.filename not in allowed for e in entries):raise ValueError('Unrecognised update member')
            if 'bundle.json' not in zipped.namelist():raise ValueError('Missing bundle metadata')
            m=json.loads(zipped.read('bundle.json'))
            if m.get('protocol')!=1 or m.get('base_sha256')!=DATABASE_SHA256:raise ValueError('Wrong update protocol/base')
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',m.get('update_id','')):raise ValueError('Invalid update id')
            if not REQUIRED<=set(m['tables']) or set(m['tables'])-set(TABLES):raise ValueError('Incomplete/unrecognised update tables')
            cutoff=date.fromisoformat(m['cutoff']).isoformat()
            if cutoff!=m['cutoff'] or date.fromisoformat(m['source_last'])<date.fromisoformat(cutoff):raise ValueError('Invalid source range')
            for table,meta in m['tables'].items():
                name=f'{table}.parquet';target=temp/name
                with zipped.open(name) as src,target.open('wb') as dst:shutil.copyfileobj(src,dst)
                if sha256(target)!=meta['sha256']:raise ValueError(f'Bad table checksum: {table}')
            for name,digest in m.get('external',{}).items():
                if name not in EXTERNAL:raise ValueError('Unexpected external filename')
                target=temp/'external'/name;target.parent.mkdir(exist_ok=True)
                with zipped.open(f'external/{name}') as src,target.open('wb') as dst:shutil.copyfileobj(src,dst)
                if sha256(target)!=digest:raise ValueError('External cache checksum mismatch')
        with lock if lock is not None else nullcontext():
            with connect(database) as con:
                old=None
                exists=con.execute('SELECT COUNT(*) FROM information_schema.tables WHERE table_name=?',[STATE_TABLE]).fetchone()[0]
                if exists:
                    old=con.execute(f'SELECT update_id,cutoff,generated_at FROM {STATE_TABLE} LIMIT 1').fetchone()
                    if old and old[0]==m['update_id']:return False
                    if old and (str(old[1])!=cutoff or datetime.fromisoformat(old[2])>datetime.fromisoformat(m['generated_at'])):
                        raise ValueError('Update would change baseline range or go backwards')
                con.execute('BEGIN')
                try:
                    available={r[0] for r in con.execute('SHOW TABLES').fetchall()}
                    for table,meta in m['tables'].items():
                        path=temp/f'{table}.parquet'
                        con.execute(f'CREATE OR REPLACE TEMP VIEW {ident("stage_"+table)} AS SELECT * FROM read_parquet({quote(path)})')
                        actual=[list(row[:2]) for row in con.execute(f'DESCRIBE {ident("stage_"+table)}').fetchall()]
                        if actual!=meta['schema']:raise ValueError(f'Update schema mismatch: {table}')
                        if con.execute(f'SELECT COUNT(*) FROM {ident("stage_"+table)}').fetchone()[0]!=meta['rows']:raise ValueError('Update row count mismatch')
                    if con.execute(f'SELECT count(*) FROM stage_clean_joined_prices WHERE CAST(date AS DATE)<{quote(cutoff)}::DATE').fetchone()[0]:raise ValueError('Prices outside declared update range')
                    # Update only changed historical lookup codes, before replacing lookups.
                    for lookup,key,fields in [('lookup_items','item_code',['item','unit','item_group','item_category']),
                                              ('lookup_premises','premise_code',['premise','address','state','district','premise_type'])]:
                        columns=','.join([key,*fields])
                        con.execute(f'CREATE TEMP TABLE changed_{key} AS SELECT {key} FROM (SELECT {columns} FROM stage_{lookup} EXCEPT SELECT {columns} FROM {lookup})')
                        assignments=','.join(f'{c}=i.{c}' for c in fields)
                        con.execute(f'UPDATE clean_joined_prices c SET {assignments} FROM stage_{lookup} i WHERE c.{key}=i.{key} AND c.{key} IN (SELECT {key} FROM changed_{key})')
                    for table in TABLES:
                        if table not in m['tables']:continue
                        stage=ident('stage_'+table)
                        if table=='clean_joined_prices':
                            original=[list(r[:2]) for r in con.execute('DESCRIBE clean_joined_prices').fetchall()]
                            if original!=m['tables'][table]['schema']:raise ValueError('Price schema differs from initial DB')
                            con.execute('DELETE FROM clean_joined_prices WHERE CAST(date AS DATE)>=?::DATE',[cutoff])
                            con.execute(f'INSERT INTO clean_joined_prices BY NAME SELECT * FROM {stage}')
                        else:
                            con.execute(f'CREATE OR REPLACE TABLE {ident(table)} AS SELECT * FROM {stage}')
                    # A derived table absent in the source must not serve stale observations.
                    if 'daily_state_prices' in available and 'daily_state_prices' not in m['tables']:
                        con.execute('DROP TABLE daily_state_prices')
                    if 'agrovista_overview_precomputed_v1' in available and 'agrovista_overview_precomputed_v1' not in m['tables']:
                        con.execute('DROP TABLE agrovista_overview_precomputed_v1')
                    if 'clean_prices' in available:
                        con.execute('DELETE FROM clean_prices WHERE CAST(date AS DATE)>=?::DATE',[cutoff])
                        con.execute('INSERT INTO clean_prices BY NAME SELECT date,item_code,premise_code,price FROM stage_clean_joined_prices')
                    validate_forecasts(con,m['forecast_today'])
                    actual_last=str(con.execute('SELECT MAX(CAST(date AS DATE)) FROM clean_joined_prices').fetchone()[0])
                    if actual_last!=m['source_last']:raise ValueError('Price snapshot does not match update')
                    con.execute(f'CREATE TABLE IF NOT EXISTS {STATE_TABLE}(update_id VARCHAR,cutoff VARCHAR,source_last VARCHAR,forecast_today VARCHAR,generated_at VARCHAR)')
                    con.execute(f'DELETE FROM {STATE_TABLE}')
                    con.execute(f'INSERT INTO {STATE_TABLE} VALUES (?,?,?,?,?)',[m[k] for k in ('update_id','cutoff','source_last','forecast_today','generated_at')])
                    con.execute('COMMIT')
                except Exception:
                    con.execute('ROLLBACK');raise
        if external_dir:
            destination=Path(external_dir);destination.mkdir(parents=True,exist_ok=True)
            for name in m.get('external',{}):shutil.copy2(temp/'external'/name,destination/name)
        return True

OVERVIEW_REPAIR_VERSION=2
