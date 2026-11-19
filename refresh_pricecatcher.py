"""Refresh official monthly PriceCatcher partitions in the Notebook 02 DuckDB.
No outlier treatment, imputation or price transformation. Validate then publish atomically.
"""
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import argparse
import json
import os
import re
import shutil
import tempfile
import urllib.request
import urllib.error
import duckdb

BASE_URL = 'https://storage.data.gov.my/pricecatcher/'
PRICE_COLS = ['date','item_code','premise_code','price']
ITEM_COLS = ['item_code','item','unit','item_group','item_category']
PREMISE_COLS = ['premise_code','premise','address','state','district','premise_type']
JOIN_COLS = ['date','item_code','item','unit','item_group','item_category','premise_code','premise','address','state','district','premise_type','price']

def root_path(explicit=None):
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if not (root/'data').is_dir(): raise FileNotFoundError(f'Project data folder not found: {root}')
        return root
    for base in (Path(__file__).resolve().parent, Path.cwd().resolve()):
        for p in (base, *base.parents):
            if (p/'data'/'database'/'agrovista.duckdb').is_file(): return p
    raise FileNotFoundError('Cannot locate project. Use --root "D:\\...\\AgroVista_V2"')

def month_add(month, step=1):
    y,m = map(int, month.split('-')); n=y*12+m-1+step
    return f'{n//12:04d}-{n%12+1:02d}'

def month_list(start, end):
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',start): raise ValueError('Invalid --from-month; use YYYY-MM')
    if start>end: raise ValueError('Start month is after current month')
    result=[]
    while start<=end:
        result.append(start);start=month_add(start)
    return result

def download(url, destination):
    """Three bounded attempts; atomic local promotion after a complete response."""
    last=None
    for attempt in range(3):
        part=destination.with_suffix(destination.suffix+'.part')
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'AgroVista-FYP/1.0'})
            with urllib.request.urlopen(req,timeout=60) as response, part.open('wb') as output:
                shutil.copyfileobj(response,output)
                expected=response.headers.get('Content-Length')
            if not part.stat().st_size: raise ValueError(f'Empty download: {url}')
            if expected and part.stat().st_size!=int(expected): raise ValueError('Incomplete download')
            os.replace(part,destination);return
        except urllib.error.HTTPError as exc:
            part.unlink(missing_ok=True)
            if exc.code==404: raise
            last=exc
        except (OSError,ValueError) as exc:
            part.unlink(missing_ok=True);last=exc
    raise RuntimeError(f'Download failed after 3 attempts: {url}: {last}')

def schema(db, table, expected):
    actual=[r[0] for r in db.execute(f'DESCRIBE {table}').fetchall()]
    if set(actual)!=set(expected): raise ValueError(f'{table} schema differs from Notebook 02. Expected {expected}; got {actual}. No changes committed.')

def import_months(db_path, files, item_file, premise_file, today, dry_run=False):
    """Whole supplied month replacement handles corrections, late rows and repeat runs.
    Downloaded months must be complete official monthly files, never daily extracts.
    """
    if not Path(db_path).is_file(): raise FileNotFoundError(f'Existing Notebook 02 database missing: {db_path}')
    with duckdb.connect(str(db_path)) as db:
        db.execute('SET threads=4');db.execute("SET memory_limit='4GB'")
        has_clean_prices=bool(db.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name='clean_prices' AND table_type='BASE TABLE'").fetchone()[0])
        for table,cols in ([('clean_prices',PRICE_COLS)] if has_clean_prices else [])+[('lookup_items',ITEM_COLS),('lookup_premises',PREMISE_COLS),('clean_joined_prices',JOIN_COLS)]:schema(db,table,cols)
        db.execute('CREATE TEMP TABLE new_items AS SELECT * FROM read_parquet(?)',[str(item_file)])
        db.execute('CREATE TEMP TABLE new_premises AS SELECT * FROM read_parquet(?)',[str(premise_file)])
        for table,cols,key in [('new_items',ITEM_COLS,'item_code'),('new_premises',PREMISE_COLS,'premise_code')]:
            names={r[0] for r in db.execute(f'DESCRIBE {table}').fetchall()}
            if not set(cols)<=names:raise ValueError(f'{table}: missing columns {set(cols)-names}')
            n,unique,missing=db.execute(f'SELECT COUNT(*),COUNT(DISTINCT {key}),COUNT(*) FILTER(WHERE {key} IS NULL) FROM {table}').fetchone()
            if not n or n!=unique or missing:raise ValueError(f'{table}: empty, duplicate or missing lookup keys')
        # Project lookup fields in Notebook 02 order; official Parquet column order may differ.
        for table,cols in [('new_items',ITEM_COLS),('new_premises',PREMISE_COLS)]:
            projection=','.join(f'CAST("{col}" AS '+('BIGINT' if col.endswith('_code') else 'VARCHAR')+f') AS "{col}"' for col in cols)
            db.execute(f'CREATE TEMP TABLE normalized_{table} AS SELECT {projection} FROM {table}')
            db.execute(f'DROP TABLE {table}')
            db.execute(f'ALTER TABLE normalized_{table} RENAME TO {table}')
        db.execute('CREATE TEMP TABLE incoming(date DATE,item_code BIGINT,premise_code BIGINT,price DOUBLE)')
        months=[];raw_total=0
        for month,path in files:
            end=month_add(month)
            db.execute('CREATE OR REPLACE TEMP TABLE raw_month AS SELECT * FROM read_parquet(?)',[str(path)])
            cols={r[0] for r in db.execute('DESCRIBE raw_month').fetchall()}
            if not set(PRICE_COLS)<=cols:raise ValueError(f'{path}: missing price columns')
            n,bad=db.execute('''SELECT COUNT(*), COUNT(*) FILTER(WHERE TRY_CAST(date AS DATE) IS NULL
             OR TRY_CAST(date AS DATE)<CAST(? AS DATE) OR TRY_CAST(date AS DATE)>=CAST(? AS DATE)
             OR TRY_CAST(date AS DATE)>CAST(? AS DATE)
             OR TRY_CAST(item_code AS BIGINT) IS NULL OR TRY_CAST(premise_code AS BIGINT) IS NULL
             OR TRY_CAST(price AS DOUBLE) IS NULL OR TRY_CAST(price AS DOUBLE)<=0 OR NOT isfinite(TRY_CAST(price AS DOUBLE)))
             FROM raw_month''',[month+'-01',end+'-01',today]).fetchone()
            if not n or bad:raise ValueError(f'{path.name}: {n} rows, {bad} invalid; aborting, no price cleaning or row dropping.')
            db.execute('INSERT INTO incoming SELECT DISTINCT CAST(date AS DATE),CAST(item_code AS BIGINT),CAST(premise_code AS BIGINT),CAST(price AS DOUBLE) FROM raw_month')
            months.append(month);raw_total+=n
        if not months:raise ValueError('No monthly sources available; database unchanged')
        # Match Notebook 02 LEFT JOIN policy. Retain historical lookup codes that
        # disappear from the latest catalogue; genuinely unknown metadata stays NULL.
        for new,old,key,cols in [('new_items','lookup_items','item_code',ITEM_COLS),('new_premises','lookup_premises','premise_code',PREMISE_COLS)]:
            colsql=','.join(cols)
            db.execute(f'CREATE TEMP VIEW effective_{new} AS SELECT {colsql} FROM {new} UNION ALL SELECT {colsql} FROM {old} WHERE {key} NOT IN (SELECT {key} FROM {new})')
        missing=db.execute("""SELECT
            COUNT(*) FILTER(WHERE i.item_code IS NULL) AS missing_item_lookup_rows,
            COUNT(*) FILTER(WHERE p.premise_code IS NULL) AS missing_premise_lookup_rows,
            COUNT(*) FILTER(WHERE p.state IS NULL OR TRIM(p.state)='') AS missing_state_rows,
            COUNT(*) FILTER(WHERE i.item_code IS NULL OR p.premise_code IS NULL OR p.state IS NULL OR TRIM(p.state)='') AS unmatched_rows
            FROM incoming c LEFT JOIN effective_new_items i USING(item_code)
            LEFT JOIN effective_new_premises p USING(premise_code)""").fetchone()
        lookup_report=dict(zip(['missing_item_lookup_rows','missing_premise_lookup_rows','missing_state_rows','unmatched_rows'],map(int,missing)))
        print('Lookup coverage (prices retained):',json.dumps(lookup_report),flush=True)
        if lookup_report['unmatched_rows']:
            print('NOTE: Unmatched rows are retained with NULL metadata, matching Notebook 02. Unknown states cannot enter state/premise forecasting until their lookup is available.',flush=True)
        total=db.execute('SELECT COUNT(*) FROM incoming').fetchone()[0]
        report={'months':months,'downloaded_rows':raw_total,'distinct_rows':total,'exact_duplicate_rows':raw_total-total,'dry_run':dry_run,'lookup_coverage':lookup_report}
        if dry_run:return report
        db.execute('BEGIN TRANSACTION')
        try:
            db.execute('CREATE TEMP TABLE affected_old_pairs AS SELECT DISTINCT state,item_code FROM clean_joined_prices WHERE premise_code IN (SELECT premise_code FROM (SELECT premise_code,premise,address,state,district,premise_type FROM new_premises EXCEPT SELECT premise_code,premise,address,state,district,premise_type FROM lookup_premises)) OR item_code IN (SELECT item_code FROM (SELECT item_code,item,unit,item_group,item_category FROM new_items EXCEPT SELECT item_code,item,unit,item_group,item_category FROM lookup_items))')
            # Snapshot old metadata to update only codes whose descriptions actually changed.
            db.execute('CREATE TEMP TABLE changed_items AS SELECT item_code FROM (SELECT item_code,item,unit,item_group,item_category FROM new_items EXCEPT SELECT item_code,item,unit,item_group,item_category FROM lookup_items)')
            db.execute('CREATE TEMP TABLE changed_premises AS SELECT premise_code FROM (SELECT premise_code,premise,address,state,district,premise_type FROM new_premises EXCEPT SELECT premise_code,premise,address,state,district,premise_type FROM lookup_premises)')
            for table,new,key,cols in [('lookup_items','new_items','item_code',ITEM_COLS),('lookup_premises','new_premises','premise_code',PREMISE_COLS)]:
                colsql=','.join(cols)
                db.execute(f'DELETE FROM {table} WHERE {key} IN (SELECT {key} FROM {new})')
                db.execute(f'INSERT INTO {table} ({colsql}) SELECT {colsql} FROM {new}')
            for month in months:
                args=[month+'-01',month_add(month)+'-01']
                for table in (['clean_prices'] if has_clean_prices else [])+['clean_joined_prices']:
                    db.execute(f'DELETE FROM {table} WHERE date>=CAST(? AS DATE) AND date<CAST(? AS DATE)',args)
            if has_clean_prices:
                db.execute('INSERT INTO clean_prices (date,item_code,premise_code,price) SELECT date,item_code,premise_code,price FROM incoming')
            db.execute('''INSERT INTO clean_joined_prices (date,item_code,item,unit,item_group,item_category,premise_code,premise,address,state,district,premise_type,price) SELECT c.date,c.item_code,i.item,i.unit,i.item_group,i.item_category,
                c.premise_code,p.premise,p.address,p.state,p.district,p.premise_type,c.price
                FROM incoming c LEFT JOIN lookup_items i USING(item_code) LEFT JOIN lookup_premises p USING(premise_code)''')
            # Keep historical metadata consistent when a lookup code changes.
            db.execute('''UPDATE clean_joined_prices c SET item=i.item,unit=i.unit,item_group=i.item_group,item_category=i.item_category
                FROM lookup_items i WHERE c.item_code=i.item_code AND i.item_code IN (SELECT item_code FROM changed_items)''')
            db.execute('''UPDATE clean_joined_prices c SET premise=p.premise,address=p.address,state=p.state,district=p.district,premise_type=p.premise_type
                FROM lookup_premises p WHERE c.premise_code=p.premise_code AND p.premise_code IN (SELECT premise_code FROM changed_premises)''')
            tables={r[0] for r in db.execute('SHOW TABLES').fetchall()}
            # An optional derived table must not continue serving stale state medians.
            if 'daily_state_prices' in tables:
                expected=['date','item_code','state','median_price','item_name','item_unit','item_group','item_category']
                if set(r[0] for r in db.execute('DESCRIBE daily_state_prices').fetchall())!=set(expected):
                    raise ValueError('daily_state_prices has an unrecognised schema. No changes committed.')
                db.execute('CREATE TEMP TABLE affected_pairs AS SELECT * FROM affected_old_pairs UNION SELECT DISTINCT c.state,c.item_code FROM clean_joined_prices c WHERE c.premise_code IN (SELECT premise_code FROM changed_premises) OR c.item_code IN (SELECT item_code FROM changed_items)')
                db.execute('CREATE TEMP TABLE refreshed_months(month VARCHAR)')
                db.executemany('INSERT INTO refreshed_months VALUES (?)',[(m,) for m in months])
                # Explicit outer aliases prevent correlated predicates from becoming tautologies.
                db.execute("DELETE FROM daily_state_prices d WHERE strftime(d.date, '%Y-%m') IN (SELECT month FROM refreshed_months) OR EXISTS (SELECT 1 FROM affected_pairs a WHERE a.state IS NOT DISTINCT FROM d.state AND a.item_code=d.item_code)")
                db.execute("""INSERT INTO daily_state_prices (date,item_code,state,median_price,item_name,item_unit,item_group,item_category)
                 SELECT c.date,c.item_code,c.state,MEDIAN(c.price),MAX(c.item),MAX(c.unit),MAX(c.item_group),MAX(c.item_category)
                 FROM clean_joined_prices c WHERE c.price>0 AND isfinite(c.price) AND
                 (strftime(c.date, '%Y-%m') IN (SELECT month FROM refreshed_months) OR EXISTS
                 (SELECT 1 FROM affected_pairs a WHERE a.state IS NOT DISTINCT FROM c.state AND a.item_code=c.item_code))
                 GROUP BY c.date,c.item_code,c.state""")
            for month in months:
                n=db.execute('SELECT COUNT(*) FROM clean_joined_prices WHERE date>=CAST(? AS DATE) AND date<CAST(? AS DATE)',[month+'-01',month_add(month)+'-01']).fetchone()[0]
                incoming_n=db.execute('SELECT COUNT(*) FROM incoming WHERE date>=CAST(? AS DATE) AND date<CAST(? AS DATE)',[month+'-01',month_add(month)+'-01']).fetchone()[0]
                if n!=incoming_n:raise ValueError('Joined count mismatch before commit')
            db.execute('COMMIT')
        except Exception:
            db.execute('ROLLBACK');raise
        report['last_date']=str(db.execute('SELECT MAX(date) FROM clean_joined_prices').fetchone()[0])
        return report

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root');parser.add_argument('--from-month',help='YYYY-MM; default last recorded month minus one')
    parser.add_argument('--offline-dir',type=Path,help='Use complete local monthly Parquet and lookup_item/premise.parquet files instead of downloading')
    parser.add_argument('--dry-run',action='store_true');parser.add_argument('--backup',action='store_true',help='Copy the closed DuckDB before importing (requires enough disk space)')
    args=parser.parse_args();root=root_path(args.root);db=root/'data'/'database'/'agrovista.duckdb'
    today=datetime.now(ZoneInfo('Asia/Kuala_Lumpur')).date();current=today.strftime('%Y-%m')
    with duckdb.connect(str(db),read_only=True) as con:last=con.execute('SELECT MAX(date) FROM clean_joined_prices').fetchone()[0]
    if last is None and not args.from_month:raise ValueError('Empty source table: specify --from-month YYYY-MM')
    start=args.from_month or month_add(str(last)[:7],-1)
    months=month_list(start,current)
    print('Refresh months:',', '.join(months),flush=True)
    cache=root/'data'/'refresh_downloads';cache.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='staging_',dir=cache) as tmp:
        tmp=Path(tmp);files=[]
        for month in months:
            name=f'pricecatcher_{month}.parquet';target=tmp/name
            if args.offline_dir:
                source=args.offline_dir/name
                if not source.is_file():raise FileNotFoundError(source)
                shutil.copy2(source,target)
            else:
                print('Downloading',name,flush=True)
                try:download(BASE_URL+name,target)
                except urllib.error.HTTPError as exc:
                    if exc.code==404 and month==current:
                        print('Current month not published yet; retaining existing data for that month.',flush=True);continue
                    raise
            shutil.copy2(target,cache/name)
            files.append((month,target))
        lookups=[]
        for name in ['lookup_item.parquet','lookup_premise.parquet']:
            target=tmp/name
            if args.offline_dir:shutil.copy2(args.offline_dir/name,target)
            else:download(BASE_URL+name,target)
            shutil.copy2(target,cache/name)
            lookups.append(target)
        if args.backup and not args.dry_run:
            with duckdb.connect(str(db)) as con:con.execute('CHECKPOINT')
            shutil.copy2(db,db.with_name('agrovista_before_refresh_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.duckdb'))
        report=import_months(db,files,*lookups,today,args.dry_run)
        if not args.dry_run:
            for _,path in files:shutil.copy2(path,cache/path.name)
            for path in lookups:shutil.copy2(path,cache/path.name)
    audit=root/'data'/'audit'/'refresh';audit.mkdir(parents=True,exist_ok=True)
    (audit/'pricecatcher_refresh_latest.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2),flush=True)

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(f'REFRESH FAILED: {exc}\nNo partial database transaction is committed.',flush=True)
        if 'lock' in str(exc).lower() or 'conflict' in str(exc).lower():
            print('Close Streamlit/Jupyter DuckDB connections before writing.',flush=True)
        raise SystemExit(1)
