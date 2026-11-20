"""Versioned JSON snapshots for the two expensive Overview sections.
No pickles or executable payloads. Missing/stale snapshots fall back to live queries.
"""
from pathlib import Path
from io import StringIO
import hashlib,json,time
import pandas as pd
import duckdb
TABLE='agrovista_overview_precomputed_v1'
VERSION=1

def cache_key(family,snapshot,batch,state,codes,first=None,last=None):
    values=[VERSION,family,str(pd.Timestamp(snapshot).date()),str(batch),state,sorted(set(map(int,codes))),first,last]
    return hashlib.sha256(json.dumps(values,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def encode_frame(frame):
    return frame.to_json(orient='table',date_format='iso',double_precision=15)

def decode_frame(value):
    return pd.read_json(StringIO(value),orient='table')

def encode_summary(value):
    totals,counts,types,end,movers,days,weekly=value
    return json.dumps({'version':VERSION,'totals':encode_frame(totals.to_frame().T),'counts':encode_frame(counts),
        'types':encode_frame(types),'end':pd.Timestamp(end).isoformat(),'movers':encode_frame(movers),
        'days':int(days),'weekly':encode_frame(weekly)},allow_nan=False)

def decode_summary(value):
    d=json.loads(value)
    if d['version']!=VERSION:raise ValueError('Unknown prepared overview version')
    return (decode_frame(d['totals']).iloc[0],decode_frame(d['counts']),decode_frame(d['types']),
            pd.Timestamp(d['end']),decode_frame(d['movers']),int(d['days']),decode_frame(d['weekly']))

def read_prepared(store,family,snapshot,state,codes,first=None,last=None):
    if TABLE not in store.tables:return None
    batch=store.batch()
    if batch is None:return None
    key=cache_key(family,snapshot,batch.batch_id,state,codes,first,last)
    try:
        found=store.q(f'SELECT payload FROM {TABLE} WHERE cache_key=?',[key])
        if len(found)!=1:return None
        value=found.iloc[0,0]
        return decode_summary(value) if family=='summary' else decode_frame(value)
    except (ValueError,TypeError,KeyError,duckdb.Error):
        # An optional acceleration table must never break the original dashboard.
        return None

def build_precomputed(database):
    from update_bundle import connect
    from overview_compute import Store,compute_summary
    from pulse_history import monthly_pulse
    started=time.perf_counter();rows=[];measurements=[]
    with connect(database) as con:
        store=Store.__new__(Store);store._connection=con;Store.__init__(store,Path(database))
        snap=store.snapshot();batch=store.batch()
        if snap is None or batch is None:raise ValueError('Cannot prepare Overview without prices and forecasts')
        eligible=store.eligible();cat=store.catalogue(Path(database).parent)
        states=store.q('SELECT DISTINCT state FROM clean_joined_prices WHERE state IS NOT NULL').state.tolist()
        states=sorted(set(states)|set(eligible.state))
        start=snap-pd.Timedelta(days=89)
        reports=f'''FROM {store.source}
        WHERE date >= CAST(? AS DATE) AND date < CAST(? AS DATE) + INTERVAL 1 DAY
        AND price > 0 AND isfinite(price)'''
        for state in [None,*states]:
            subset=eligible if state is None else eligible[eligible.state.eq(state)]
            codes=cat.loc[cat.item_code.isin(subset.item_code),'item_code'].astype(int).tolist()
            clause=' AND state=?' if state is not None else ''
            params=[state] if state is not None else []
            dates=[str(start.date()),str(snap.date()),*params]
            summary_started=time.perf_counter()
            value=compute_summary(store.db,store.stamp,snap,reports+clause,tuple(dates),tuple(codes),state,_store=store)
            summary_ms=(time.perf_counter()-summary_started)*1000
            measurements.append({'area':state or 'Malaysia','direct_compute_ms':round(summary_ms,3)})
            rows.append((cache_key('summary',snap,batch.batch_id,state,codes),'summary',state or 'Malaysia',encode_summary(value)))
            state_codes=subset.loc[subset.level.eq('state'),'item_code'].astype(int).unique().tolist()
            outlook=cat.loc[cat.item_code.isin(state_codes),'item_code'].astype(int).tolist()
            first_date=store.q(f'SELECT MIN(CAST(date AS DATE)) first_date FROM clean_joined_prices WHERE state IS NOT NULL AND price>0 AND isfinite(price){clause}',params).iloc[0,0]
            if outlook and pd.notna(first_date):
                first_date=pd.Timestamp(first_date);first=first_date.to_period('M')
                first+=int(first_date!=first.start_time)
                last=snap.to_period('M')-int(not snap.is_month_end)
                if last>first:
                    pulse=monthly_pulse(store,first,last,outlook,clause,params)
                    rows.append((cache_key('pulse',snap,batch.batch_id,state,outlook,str(first),str(last)),
                                 'pulse',state or 'Malaysia',encode_frame(pulse)))
            print('Prepared Overview:',state or 'Malaysia',flush=True)
        # Only replace the current snapshots if every requested area succeeded.
        con.execute('BEGIN')
        try:
            con.execute(f'CREATE TABLE IF NOT EXISTS {TABLE}(cache_key VARCHAR PRIMARY KEY,family VARCHAR,area VARCHAR,payload VARCHAR)')
            con.execute(f'DELETE FROM {TABLE}')
            con.executemany(f'INSERT INTO {TABLE} VALUES (?,?,?,?)',rows)
            con.execute('COMMIT')
        except Exception:
            con.execute('ROLLBACK');raise
        for entry,row in zip(measurements,[r for r in rows if r[1]=='summary']):
            t=time.perf_counter()
            decode_summary(con.execute(f'SELECT payload FROM {TABLE} WHERE cache_key=?',[row[0]]).fetchone()[0])
            entry['prepared_read_ms']=round((time.perf_counter()-t)*1000,3)
    return {'areas':len(states)+1,'snapshots':len(rows),'payload_bytes':sum(len(r[3].encode()) for r in rows),
            'build_seconds':round(time.perf_counter()-started,3),'summary_timings':measurements}

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--database',required=True)
    args=parser.parse_args();print(json.dumps(build_precomputed(args.database),indent=2))
