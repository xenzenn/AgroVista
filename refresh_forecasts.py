"""Refresh the dashboard using the saved Notebook 06A/06B per-horizon models."""

def main():
    from pathlib import Path
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from collections import Counter
    import argparse
    import sys, json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Generate and validate without publishing")
    args = parser.parse_args()
    import numpy as np
    import pandas as pd
    import duckdb
    import joblib

    ROOT=Path(__file__).resolve().parent
    if not (ROOT/'data').is_dir():
        raise FileNotFoundError(f'Cannot locate AgroVista_V2/data from {Path.cwd()}. Run from project root.')
    DB=ROOT/'data'/'database'/'agrovista.duckdb'
    MODEL_ROOT=ROOT/'models'/'agrovista_per_horizon_candidates'
    EXT=ROOT/'data'/'external'/'final_rebuild'
    if not DB.is_file(): raise FileNotFoundError(f'Missing DuckDB: {DB}')
    print('Root:',ROOT,'\nDatabase:',DB,'\nPython:',sys.version.split()[0],
          '| DuckDB:',duckdb.__version__,'| LightGBM:',__import__('lightgbm').__version__)
    if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
    from agrovista_horizon_models import validate_contract, raw_predict

    TZ=ZoneInfo('Asia/Kuala_Lumpur')
    # Set FORECAST_TODAY='2026-11-02' for reproducible demonstration; None means current KL date.
    FORECAST_TODAY=None
    TODAY=pd.Timestamp(FORECAST_TODAY).normalize() if FORECAST_TODAY else pd.Timestamp.now(tz=TZ).tz_localize(None).normalize()
    TARGET_DATES=pd.date_range(TODAY,periods=7,freq='D')
    MAX_SUPPORTED_STALENESS=15  # Model trained on horizons 1..21: 15 + 6 = 21.
    MAX_TOTAL_FORECAST_ROWS=600000  # Nationwide OOV may exceed previous 250k limit; monitor RAM
    DUCKDB_THREADS=4
    DUCKDB_MEMORY_LIMIT='4GB'
    SAVE_TO_DUCKDB = not args.dry_run
    ALLOW_PARTIAL_LEVEL=False  # If either level has zero predictions, stop before writing.
    # No schema-breaking replacement of an existing dashboard table.
    CURRENT_TABLE='agrovista_v2_forecast_current'
    HISTORY_TABLE='agrovista_v2_forecast_history'
    BATCH_TABLE='agrovista_v2_forecast_batches'
    print('Forecast window:',[str(d.date()) for d in TARGET_DATES]); assert len(TARGET_DATES)==7

    ARTIFACTS={}; MANIFESTS={}
    for level,folder in [('state','state_level'),('premise','premise_level')]:
        mp=MODEL_ROOT/folder/'selected_direct_model.joblib'
        jp=MODEL_ROOT/folder/'manifest.json'
        if not mp.is_file() or not jp.is_file():
            raise FileNotFoundError(f'Missing 06{ "A" if level=="state" else "B" } artifacts: {mp} / {jp}')
        art=joblib.load(mp); manifest=json.loads(jp.read_text(encoding='utf-8-sig'))
        for k in ('pipeline','columns','vocab','window_size','target_mode','algorithm','supported_horizons'):
            if k not in art: raise ValueError(f'{level}: missing artifact field {k}')
        if art['level']!=level: raise ValueError(f'{level}: artifact level mismatch')
        expected = {'state': ('LightGBM', 'PriceCatcher + Weather'), 'premise': ('RandomForest', 'PriceCatcher + Holidays')}[level]
        if (art['algorithm'], art['configuration']) != expected or art['window_size'] != 30:
            raise ValueError(f'{level}: unexpected Notebook 06 candidate configuration')
        if art.get('architecture') != 'per_horizon':
            raise ValueError(f'{level}: expected a separate model per horizon')
        if art['target_mode'] not in ('delta','absolute'):raise ValueError(f'{level}: unknown target mode')
        if len(art['columns'])!=len(set(art['columns'])):raise ValueError(f'{level}: duplicate feature columns')
        validate_contract(art)
        supported=set(int(h) for h in art['supported_horizons'])
        if not set(range(1,22)).issubset(supported):
            raise ValueError(f'{level}: model must support horizon 1..21 for gap policy')
        if level=='premise' and art.get('oov_supported_fields'):
            if 'unknown_category_code' not in art:
                raise ValueError('Premise OOV contract missing unknown_category_code')
            if not set(art['oov_supported_fields']).issubset({'item_code','premise_code'}):
                raise ValueError('Unexpected premise OOV fields')
        if level=='state' and art.get('oov_supported_fields'):
            if set(art['oov_supported_fields']) != {'item_code'}:
                raise ValueError('State OOV contract must include only item_code')
            if 'unknown_category_code' not in art:
                raise ValueError('State OOV model missing unknown_category_code')
            if 'enc_item_code' not in art['columns']:
                raise ValueError('State OOV model missing item encoding feature')
            if 'oov_training_mask_fraction' not in art:
                raise ValueError('State OOV model missing masking provenance')
        ARTIFACTS[level]=art; MANIFESTS[level]=manifest
        print(level, '| algorithm:',art['algorithm'],'| config:',art['configuration'],
              '| window:',art['window_size'],'| target:',art['target_mode'],
              '| features:',len(art['columns']), '| snapshot:',art.get('snapshot_date'))
    assert set(ARTIFACTS)=={'state','premise'}

    con=duckdb.connect(str(DB),read_only=True)
    try:
        con.execute(f"SET threads={int(DUCKDB_THREADS)}")
        con.execute(f"SET memory_limit='{DUCKDB_MEMORY_LIMIT}'")
        schema=con.execute('DESCRIBE clean_joined_prices').df()
        cols=set(schema.column_name)
        required={'date','state','item_code','premise_code','price'}
        if not required.issubset(cols):raise ValueError(f'clean_joined_prices missing {required-cols}')
        source=con.execute('''SELECT MIN(TRY_CAST(date AS DATE)) AS min_date,
           MAX(TRY_CAST(date AS DATE)) AS max_date, COUNT(*) AS total_rows
           FROM clean_joined_prices''').df().iloc[0]
        SOURCE_LAST=pd.Timestamp(source['max_date']).normalize()
        if pd.isna(SOURCE_LAST):raise ValueError('Source table has no valid date')
        print('Source:',str(source['min_date']),'to',SOURCE_LAST.date(),
              '| total rows:',int(source['total_rows']))
        print('Age of newest source date:',(TODAY-SOURCE_LAST).days,'days')
        if SOURCE_LAST>TODAY:print('NOTE: Source contains future-dated records; query restricts observations to < Today.')
        for level,art in ARTIFACTS.items():
            print(level,'training snapshot:',art.get('snapshot_date'))
    finally:
        con.close()

    scope_path=ROOT/'data'/'analysis'/'scope'/'state_scope.csv'
    if not scope_path.is_file():raise FileNotFoundError(f'Run Notebook 03 first: {scope_path}')
    scope=pd.read_csv(scope_path,low_memory=False)
    if not {'state','item_code'}.issubset(scope):raise ValueError('state_scope.csv missing state/item_code')
    if 'state_eligible' in scope:
        scope=scope.loc[scope.state_eligible.astype(str).str.lower().isin(['true','1','yes'])].copy()
    scope['state']=scope.state.astype(str).str.strip()
    scope['item_code']=pd.to_numeric(scope.item_code,errors='coerce')
    scope=scope.dropna(subset=['state','item_code'])[['state','item_code']].drop_duplicates()
    scope['item_code']=scope.item_code.astype('int64')
    def active_complete_series(db):
        if not db.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
            [CURRENT_TABLE],
        ).fetchone()[0]:
            return pd.DataFrame(columns=['level', 'state', 'item_code'])
        return db.execute(f"""
            SELECT DISTINCT level, state, item_code FROM (
                SELECT level, state, item_code
                FROM {CURRENT_TABLE}
                WHERE level IN ('state', 'premise') AND state IS NOT NULL AND item_code IS NOT NULL
                  AND (level = 'state' OR premise_code IS NOT NULL)
                GROUP BY batch_id, forecast_today, level, state, item_code, premise_code
                HAVING COUNT(*) = 7
                   AND COUNT(DISTINCT CAST(forecast_date AS DATE)) = 7
                   AND COUNT(*) FILTER (WHERE predicted_price > 0 AND isfinite(predicted_price)) = 7
                   AND MIN(CAST(forecast_date AS DATE) - CAST(forecast_today AS DATE)) = 0
                   AND MAX(CAST(forecast_date AS DATE) - CAST(forecast_today AS DATE)) = 6
            )
        """).df()

    with duckdb.connect(str(DB), read_only=True) as db:
        active_series = active_complete_series(db)
    if not active_series.empty:
        previous_pairs = active_series[['state', 'item_code']].drop_duplicates()
        scope = pd.concat([scope, previous_pairs], ignore_index=True).drop_duplicates()
        print('Restored active item/state pairs to inference scope:', len(previous_pairs))

    if scope.empty:raise ValueError('Approved state scope is empty')
    print('Approved state/item combinations:',len(scope))

    def valid_codes(vocab, field):
        if field not in vocab:raise ValueError(f'Artifact missing vocabulary {field}')
        return list(vocab[field].keys())

    def load_candidates(level,art):
        keys=['state','item_code']+(['premise_code'] if level=='premise' else [])
        vocabulary=art['vocab']
        for k in keys:valid_codes(vocabulary,k)
        # Register only compact category vocab and approved scope, not a huge all-history frame.
        db=duckdb.connect(str(DB),read_only=True)
        try:
            db.execute(f"SET threads={int(DUCKDB_THREADS)}")
            db.execute(f"SET memory_limit='{DUCKDB_MEMORY_LIMIT}'")
            db.register('approved_scope07',scope)
            clauses=['c.date < ?', 'c.price > 0','isfinite(c.price)',
                     'c.date IS NOT NULL','c.state IS NOT NULL','c.item_code IS NOT NULL']
            params=[TODAY.date()]
            oov_fields=set(art.get('oov_supported_fields',[]))
            for key in keys:
                values=valid_codes(vocabulary,key)
                if not values:raise ValueError(f'{level}: empty vocab for {key}')
                if key in oov_fields:
                    # These fields were deliberately masked to OOV in Notebook 06B training.
                    # Do NOT bypass state vocabulary or approved commodity scope.
                    continue
                clauses.append(f'CAST(c.{key} AS VARCHAR) IN (SELECT value FROM allowed_{key})')
                db.register(f'allowed_{key}',pd.DataFrame({'value':values}))
            if level=='premise':clauses.append('c.premise_code IS NOT NULL')
            keycols=', '.join('c.'+k for k in keys)
            sql=f'''SELECT {keycols}, COUNT(DISTINCT c.date) AS observed_dates,
                  MAX(c.date) AS last_observed_date
                  FROM clean_joined_prices c
                  INNER JOIN approved_scope07 s ON c.state=s.state AND c.item_code=s.item_code
                  WHERE {' AND '.join(clauses)}
                  GROUP BY {keycols}
                  HAVING COUNT(DISTINCT c.date)>=?'''
            frame=db.execute(sql,params+[int(art['window_size'])]).df()
        finally:db.close()
        if frame.empty:return frame
        frame['last_observed_date']=pd.to_datetime(frame['last_observed_date'])
        frame['age_days']=(TODAY-frame['last_observed_date']).dt.days
        frame['max_horizon']=frame['age_days']+6
        frame['supported']=frame['age_days'].between(1,MAX_SUPPORTED_STALENESS) & frame['max_horizon'].le(max(art['supported_horizons']))
        # age_days=0 means last actual today; D+0 horizon=0 was NOT trained; cannot forecast today.
        # In that case preserve today's actual separately and forecast D+1..D+6; handled below.
        frame.loc[frame.age_days.eq(0),'supported']=frame.loc[frame.age_days.eq(0),'max_horizon'].le(max(art['supported_horizons']))
        frame=frame.sort_values(['supported','last_observed_date','observed_dates'],ascending=[False,False,False])
        return frame.reset_index(drop=True)

    candidates={}
    for level,art in ARTIFACTS.items():
        candidates[level]=load_candidates(level,art)
        df=candidates[level]
        print(level,'candidate series:',len(df),'| age eligible:',int(df.supported.sum()) if len(df) else 0)

    def fetch_history(level, frame, window_size):
        if frame.empty:return pd.DataFrame()
        keys=['state','item_code']+(['premise_code'] if level=='premise' else [])
        selected=frame.loc[frame.supported,keys].copy()
        if selected.empty:return pd.DataFrame()
        db=duckdb.connect(str(DB),read_only=True)
        try:
            db.execute(f"SET threads={int(DUCKDB_THREADS)}")
            db.execute(f"SET memory_limit='{DUCKDB_MEMORY_LIMIT}'")
            db.register('selected_keys07',selected)
            join=' AND '.join(f'c.{k}=s.{k}' for k in keys)
            key_sql=', '.join('c.'+k for k in keys)
            group_sql=', '.join(keys)
            sql=f'''WITH daily AS (
              SELECT {key_sql}, c.date AS date, MEDIAN(c.price) AS price
              FROM clean_joined_prices c JOIN selected_keys07 s ON {join}
              WHERE c.date < ? AND c.price > 0 AND isfinite(c.price)
              GROUP BY {key_sql},c.date
            ), ranked AS (
              SELECT *, ROW_NUMBER() OVER (PARTITION BY {group_sql} ORDER BY date DESC) AS rn FROM daily
            ) SELECT {group_sql},date,price FROM ranked WHERE rn<=?
              ORDER BY {group_sql},date'''
            result=db.execute(sql,[TODAY.date(),int(window_size)]).df()
        finally:db.close()
        if not result.empty:
            result['date']=pd.to_datetime(result.date)
            result['price']=pd.to_numeric(result.price,errors='coerce')
            result=result.dropna(subset=['date','price']).sort_values(keys+['date'])
            if result.duplicated(keys+['date']).any():raise AssertionError(f'{level}: duplicate daily history keys')
        return result

    histories={level:fetch_history(level,candidates[level],art['window_size']) for level,art in ARTIFACTS.items()}
    for level,frame in histories.items():print(level,'history rows:',len(frame))

    def origin_features(hist, window):
        tail=hist.iloc[-window:]
        values=tail.price.to_numpy(dtype=float)
        dates=tail.date.to_numpy(dtype='datetime64[D]')
        gaps=np.diff(dates).astype('timedelta64[D]').astype(int)
        last=float(values[-1])
        return dict(last_price=last,lag_2=float(values[-2]) if len(values)>=2 else last,
            lag_7=float(values[-7]) if len(values)>=7 else last,
            mean_7=float(np.mean(values[-7:])),median_7=float(np.median(values[-7:])),
            std_7=float(np.std(values[-7:])),mean_window=float(np.mean(values)),
            median_window=float(np.median(values)),std_window=float(np.std(values)),
            min_window=float(np.min(values)),max_window=float(np.max(values)),
            change_1=float(values[-1]-values[-2]) if len(values)>=2 else 0.,
            change_7=float(values[-1]-values[-7]) if len(values)>=7 else 0.,
            days_between_last_obs=float(gaps[-1]) if len(gaps) else 0.,
            mean_reporting_gap=float(gaps.mean()) if len(gaps) else 0.,
            observed_window=float(len(values)))

    def build_inference_frame(level,art,history):
        keys=['state','item_code']+(['premise_code'] if level=='premise' else [])
        rows=[]; skipped=Counter(); supported=set(map(int,art['supported_horizons']))
        if history.empty:return pd.DataFrame(),skipped
        for key_tuple,g in history.groupby(keys,sort=False):
            if not isinstance(key_tuple,tuple):key_tuple=(key_tuple,)
            g=g.sort_values('date').reset_index(drop=True)
            if len(g)<int(art['window_size']):skipped['short_history']+=1;continue
            origin=pd.Timestamp(g.date.iloc[-1]); age=int((TODAY-origin).days)
            if age<0 or age>MAX_SUPPORTED_STALENESS:skipped['stale_or_future']+=1;continue
            feat=origin_features(g,int(art['window_size']))
            for target in TARGET_DATES:
                horizon=int((target-origin).days)
                if horizon<0:skipped['negative_horizon']+=1;continue
                # If last actual is today, h=0 is an actual, not a model forecast.
                if horizon==0:
                    row=dict(zip(keys,key_tuple));row.update(feat)
                    row.update(origin_date=origin,target_date=target,horizon_days=0,
                        target_dow=int(target.dayofweek),target_month=int(target.month),
                        target_is_weekend=int(target.dayofweek>=5),
                        origin_dow=int(origin.dayofweek),origin_month=int(origin.month),
                        prediction_kind='observed_today',predicted_price=float(feat['last_price']))
                    rows.append(row);continue
                if horizon not in supported:skipped['unsupported_horizon']+=1;continue
                row=dict(zip(keys,key_tuple));row.update(feat)
                row.update(origin_date=origin,target_date=target,horizon_days=horizon,
                    target_dow=int(target.dayofweek),target_month=int(target.month),
                    target_is_weekend=int(target.dayofweek>=5),
                    origin_dow=int(origin.dayofweek),origin_month=int(origin.month),
                    prediction_kind='model_forecast',predicted_price=np.nan)
                rows.append(row)
        return pd.DataFrame(rows),skipped

    frames={};skipped_reasons={}
    for level,art in ARTIFACTS.items():
        frames[level],skipped_reasons[level]=build_inference_frame(level,art,histories[level])
        print(level,'inference rows:',len(frames[level]),'| skips:',dict(skipped_reasons[level]))

    def read_optional_csv(filename,required):
        p=EXT/filename
        if not p.is_file():print('Optional source missing:',filename);return pd.DataFrame()
        try:
            f=pd.read_csv(p,low_memory=False)
            missing=set(required)-set(f.columns)
            if missing:raise ValueError(f'Missing columns {missing}')
            f['date']=pd.to_datetime(f['date'],errors='coerce')
            return f.dropna(subset=['date','state'])
        except (OSError,ValueError,KeyError) as e:
            print('Optional source unavailable:',filename,type(e).__name__,str(e)[:200]);return pd.DataFrame()
    weather=read_optional_csv('weather_state_daily.csv',['date','state'])
    cpi=read_optional_csv('cpi_state_food_and_overall.csv',['date','state','index'])
    holiday=read_optional_csv('holiday_state_daily.csv',['date','state'])
    if weather.empty or holiday.empty:
        raise RuntimeError('Weather and holiday inputs are required. Run refresh_external_data.py first.')
    print('External rows: weather',len(weather),'CPI',len(cpi),'holiday',len(holiday))

    def add_external_features(examples):
        if examples.empty:return examples
        examples=examples.copy()
        if not weather.empty:
            wx=weather.copy()
            for col in wx.columns:
                if col not in ['date','state']:wx[col]=pd.to_numeric(wx[col],errors='coerce')
            cols=[c for c in wx.columns if c not in ['date','state'] and wx[c].notna().any()][:6]
            if cols:
                wx=wx[['date','state']+cols].groupby(['state','date'],as_index=False).mean(numeric_only=True)
                wx=wx.rename(columns={c:'wx_'+c for c in cols}).rename(columns={'date':'origin_date'})
                examples=examples.merge(wx,on=['state','origin_date'],how='left',validate='many_to_one')
        if not cpi.empty:
            cp=cpi.copy();cp['index']=pd.to_numeric(cp['index'],errors='coerce')
            if 'division' in cp:
                food=cp.loc[cp.division.astype(str).str.zfill(2).eq('01')].copy()
                if not food.empty:cp=food
            cp['month']=cp.date.dt.to_period('M')
            cp=cp.groupby(['state','month'],as_index=False)['index'].mean().rename(columns={'index':'cpi_index_lag2'})
            examples['cpi_month_lag2']=examples.origin_date.dt.to_period('M')-2
            examples=examples.merge(cp,left_on=['state','cpi_month_lag2'],right_on=['state','month'],how='left',validate='many_to_one')
            examples=examples.drop(columns=['month','cpi_month_lag2'])
        if not holiday.empty:
            hd=holiday.copy()
            cols=[c for c in hd.columns if c not in ['date','state']]
            numeric=[c for c in cols if c in ('is_public_holiday','is_holiday') and pd.to_numeric(hd[c],errors='coerce').notna().any()]
            if numeric:
                hd['is_holiday']=pd.to_numeric(hd[numeric[0]],errors='coerce').fillna(0).gt(0).astype(int)
            else:hd['is_holiday']=1
            hd=hd.groupby(['state','date'],as_index=False)['is_holiday'].max().rename(columns={'date':'target_date'})
            examples=examples.merge(hd,on=['state','target_date'],how='left',validate='many_to_one')
            examples['is_holiday']=examples.is_holiday.fillna(0).astype(int)
        return examples
    for level in frames:frames[level]=add_external_features(frames[level])

    def predict_level(level,art,frame):
        if frame.empty:return frame
        frame=frame.copy()
        keys=['state','item_code']+(['premise_code'] if level=='premise' else [])
        for col in keys:
            frame['enc_'+col]=frame[col].astype(str).map(art['vocab'][col])
            if frame['enc_'+col].isna().any():
                if col in art.get('oov_supported_fields',[]):
                    count=int(frame['enc_'+col].isna().sum())
                    frame['enc_'+col]=frame['enc_'+col].fillna(float(art['unknown_category_code']))
                    print(f'{level}: {count:,} rows using trained OOV encoding for {col}')
                else:
                    raise ValueError(f'{level}: unseen {col} in model vocabulary')
            frame['enc_'+col]=frame['enc_'+col].astype(float)
        needed=list(art['columns'])
        # If optional external source is missing, create NaN; pipeline imputer handles only
        # columns that had non-empty values during training. For absent mandatory features, fail.
        optional_prefixes=('wx_',)
        optional_names={'cpi_index_lag2','is_holiday'}
        for col in needed:
            if col not in frame:
                if col.startswith(optional_prefixes) or col in optional_names:
                    frame[col]=np.nan
                    print(level,'optional feature absent at inference:',col)
                else:raise ValueError(f'{level}: required trained feature missing: {col}')
        X=frame[needed].apply(pd.to_numeric,errors='coerce')
        if np.isinf(X.to_numpy(dtype=float)).any():
            raise ValueError('Infinite feature values; NaN may be imputed, infinity cannot')
        pred_mask=frame.prediction_kind.eq('model_forecast')
        if pred_mask.any():
            inference_input=frame.loc[pred_mask].copy()
            inference_input[needed]=X.loc[pred_mask]
            raw=raw_predict(art,inference_input)
            if art['target_mode']=='delta':raw=frame.loc[pred_mask,'last_price'].to_numpy(dtype=float)+raw
            raw=np.maximum(0.01,raw)
            if not np.isfinite(raw).all():raise ValueError(f'{level}: nonfinite predictions')
            frame.loc[pred_mask,'predicted_price']=raw
        frame['level']=level
        frame['algorithm']=art['algorithm']
        frame['feature_configuration']=art['configuration']
        frame['window_size']=int(art['window_size'])
        frame['last_observed_price']=frame['last_price'].astype(float)
        frame['age_days']=(TODAY-frame.origin_date).dt.days
        return frame

    predicted={level:predict_level(level,ARTIFACTS[level],frames[level]) for level in ARTIFACTS}
    for level,f in predicted.items():
        print(level,'predicted rows:',len(f),'| forecast rows:',int(f.prediction_kind.eq('model_forecast').sum()) if len(f) else 0)

    OUTPUT_COLS=['batch_id','generated_at_utc','forecast_today','forecast_date','level','state',
        'item_code','premise_code','predicted_price','prediction_kind','last_observed_date',
        'last_observed_price','horizon_days','age_days','observed_dates','algorithm',
        'feature_configuration','window_size','model_snapshot']
    BATCH_ID='agrovista_v2_'+TODAY.strftime('%Y%m%d')+'_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    GENERATED_AT=datetime.now(timezone.utc).replace(tzinfo=None)

    def standardized(level,frame):
        if frame.empty:return pd.DataFrame(columns=OUTPUT_COLS)
        art=ARTIFACTS[level]
        f=frame.copy()
        f['batch_id']=BATCH_ID;f['generated_at_utc']=GENERATED_AT
        f['forecast_today']=TODAY;f['forecast_date']=f.target_date
        f['last_observed_date']=f.origin_date
        f['observed_dates']=f['observed_window'].astype('int64')  # observations USED by selected model window, not lifetime history
        f['model_snapshot']=str(art.get('snapshot_date',''))
        if level=='state':f['premise_code']=pd.NA
        f['item_code']=pd.to_numeric(f.item_code,errors='raise').astype('int64')
        f['premise_code']=pd.to_numeric(f.premise_code,errors='coerce').astype('Int64')
        return f[OUTPUT_COLS].copy()

    forecast=pd.concat([standardized(level,frame) for level,frame in predicted.items()],ignore_index=True)
    if forecast.empty:raise ValueError('No forecast rows generated. Check vocab, source freshness and window sizes.')
    # Dashboard may show only shared state/item options; retain ALL valid premise rows independently.
    shared_pairs=(forecast.loc[forecast.level.eq('state'),['state','item_code']].drop_duplicates()
        .merge(forecast.loc[forecast.level.eq('premise'),['state','item_code']].drop_duplicates(),
               on=['state','item_code'],how='inner'))
    print('Shared state/item options (both levels):',len(shared_pairs))
    if shared_pairs.empty:
        raise ValueError('No shared state/item forecast options. Refusing publication.')
    # State-level premise_code is intentionally NULL. Never concatenate nullable
    # pandas string columns directly: on some pandas versions NULL propagates and
    # collapses all series into one NaN key.
    series_keys=['level','state','item_code','premise_code']
    if forecast[['level','state','item_code','forecast_date']].isna().any().any():
        raise ValueError('Missing mandatory series identity or forecast date')
    if forecast.loc[forecast['level'].eq('premise'),'premise_code'].isna().any():
        raise ValueError('Premise-level forecast has missing premise_code')
    if forecast.loc[forecast['level'].eq('state'),'premise_code'].notna().any():
        raise ValueError('State-level forecast must have NULL premise_code')
    if not forecast['level'].isin(['state','premise']).all():
        raise ValueError('Unknown forecast level')

    # Validate with original typed columns; dropna=False treats NULL premise_code
    # as a legitimate state-level group, without any string conversion.
    counts=forecast.groupby(series_keys,dropna=False).agg(
        n=('forecast_date','size'),unique_dates=('forecast_date','nunique'))
    invalid=counts[(counts['n']!=7)|(counts['unique_dates']!=7)]
    if not invalid.empty:
        raise ValueError(f'{len(invalid)} series do not have exactly 7 dates; sample: {invalid.head().to_dict("index")}')
    if forecast.duplicated(series_keys+['forecast_date']).any():
        raise ValueError('Duplicate forecast keys')

    # Convenience ID only for coverage/reporting. Explicit placeholder is safe
    # because premise-level NULLs have already been rejected above.
    key_parts=[forecast[k].astype('string').fillna('<STATE_LEVEL>') for k in series_keys]
    forecast['series_key']=key_parts[0]
    for part in key_parts[1:]:
        forecast['series_key']=forecast['series_key'].str.cat(part,sep='|')
    if forecast['series_key'].isna().any():
        raise AssertionError('series_key unexpectedly contains NULL after normalization')
    if forecast['series_key'].nunique()!=len(counts):
        raise AssertionError('String key collision: typed groups do not match generated series keys')
    if not forecast.forecast_date.isin(TARGET_DATES).all():raise ValueError('Forecast dates outside 7-day window')
    if not (np.isfinite(forecast.predicted_price.astype(float)) & (forecast.predicted_price.astype(float)>0)).all():
        raise ValueError('Invalid predicted prices')
    if len(forecast)>MAX_TOTAL_FORECAST_ROWS:raise ValueError('Forecast row safety limit exceeded')
    if not ALLOW_PARTIAL_LEVEL and set(forecast.level)!={'state','premise'}:
        raise ValueError('One level missing. Refusing partial publication (ALLOW_PARTIAL_LEVEL=False).')
    print('Validated rows:',len(forecast),'| series:',len(counts),'| dates:',len(TARGET_DATES))
    print('Prediction kinds:',forecast.prediction_kind.value_counts().to_dict())

    # DuckDB schema is explicit to avoid inconsistent pandas inference across runs.
    CREATE_FORECAST='''(
     batch_id VARCHAR, generated_at_utc TIMESTAMP, forecast_today DATE, forecast_date DATE,
     level VARCHAR, state VARCHAR, item_code BIGINT, premise_code BIGINT,
     predicted_price DOUBLE, prediction_kind VARCHAR, last_observed_date DATE,
     last_observed_price DOUBLE, horizon_days INTEGER, age_days INTEGER,
     observed_dates INTEGER, algorithm VARCHAR, feature_configuration VARCHAR,
     window_size INTEGER, model_snapshot VARCHAR
    )'''

    CREATE_BATCH='''(
     batch_id VARCHAR, generated_at_utc TIMESTAMP, forecast_today DATE,
     state_series BIGINT, premise_series BIGINT, forecast_rows BIGINT,
     source_snapshot DATE, status VARCHAR
    )'''


    def validate_item_coverage(baseline, candidate):
        for level in ('state', 'premise'):
            before = set(baseline.loc[baseline.level.eq(level), 'item_code'])
            after = set(candidate.loc[candidate.level.eq(level), 'item_code'])
            print(f'{level} forecast items: {len(before)} active -> {len(after)} generated')
            missing = sorted(before - after)
            if missing:
                raise ValueError(
                    f'{level}: {len(missing)} active item codes lack a new complete forecast: {missing}. '
                    'Check observation age and candidate skips. The active batch is preserved.'
                )

    validate_item_coverage(active_series, forecast)

    def publish_forecast(df):
        payload=df.drop(columns=['series_key']).copy()
        for col in ['forecast_today','forecast_date','last_observed_date']:
            payload[col]=pd.to_datetime(payload[col]).dt.date
        payload['premise_code']=pd.to_numeric(payload.premise_code,errors='coerce').astype('Int64')
        db=None
        try:
            db=duckdb.connect(str(DB),read_only=False)
            db.execute(f"SET threads={int(DUCKDB_THREADS)}")
            db.execute(f"SET memory_limit='{DUCKDB_MEMORY_LIMIT}'")
            db.register('forecast_payload07',payload)
            db.execute('BEGIN TRANSACTION')
            db.execute(f'CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} {CREATE_FORECAST}')
            db.execute(f'CREATE TABLE IF NOT EXISTS {CURRENT_TABLE} {CREATE_FORECAST}')
            db.execute(f'CREATE TABLE IF NOT EXISTS {BATCH_TABLE} {CREATE_BATCH}')
            for table in [HISTORY_TABLE,CURRENT_TABLE]:
                existing=db.execute(f'DESCRIBE {table}').df()
                if list(existing.column_name)!=OUTPUT_COLS:
                    raise ValueError(f'{table} has incompatible columns. Back up/rename manually; no destructive overwrite.')
            if db.execute(f'SELECT COUNT(*) FROM {BATCH_TABLE} WHERE batch_id=?',[BATCH_ID]).fetchone()[0]:
                raise ValueError('Batch ID already exists; refusing duplicate history')
            validate_item_coverage(active_complete_series(db), payload)
            col_sql=', '.join(OUTPUT_COLS)
            db.execute(f'INSERT INTO {HISTORY_TABLE} ({col_sql}) SELECT {col_sql} FROM forecast_payload07')
            db.execute(f'DELETE FROM {CURRENT_TABLE}')
            db.execute(f'INSERT INTO {CURRENT_TABLE} ({col_sql}) SELECT {col_sql} FROM forecast_payload07')
            state_series=int(df.loc[df.level.eq('state'),'series_key'].nunique())
            premise_series=int(df.loc[df.level.eq('premise'),'series_key'].nunique())
            db.execute(f'''INSERT INTO {BATCH_TABLE} VALUES (?,?,?,?,?,?,?,?)''',
                 [BATCH_ID,GENERATED_AT,TODAY.date(),state_series,premise_series,len(df),SOURCE_LAST.date(),'published'])
            db.execute('COMMIT')
            return True
        except Exception as exc:
            if db is not None:
                try:db.execute('ROLLBACK')
                except Exception:pass
            message=str(exc).lower()
            if 'lock' in message or 'conflict' in message:
                raise RuntimeError('DuckDB write lock/conflict: close Streamlit and other DuckDB writers, then rerun refresh_forecasts.py. No partial batch committed.') from exc
            raise
        finally:
            if db is not None:db.close()

    if SAVE_TO_DUCKDB:
        published=publish_forecast(forecast)
        print('Published batch:',BATCH_ID,'| rows:',len(forecast))
    else:
        published=False
        print('DRY RUN: No database writes (SAVE_TO_DUCKDB=False).')

    if published:
        verify=duckdb.connect(str(DB),read_only=True)
        try:
            latest=verify.execute(f'SELECT batch_id,forecast_today,forecast_rows FROM {BATCH_TABLE} ORDER BY generated_at_utc DESC LIMIT 1').df()
            current=verify.execute(f'''SELECT COUNT(*) AS total_rows, COUNT(DISTINCT forecast_date) AS date_count,
                 COUNT(DISTINCT batch_id) AS batch_count, MIN(forecast_date) AS min_date,
                 MAX(forecast_date) AS max_date FROM {CURRENT_TABLE}''').df().iloc[0]
            history_n=verify.execute(f'SELECT COUNT(*) FROM {HISTORY_TABLE} WHERE batch_id=?',[BATCH_ID]).fetchone()[0]
        finally:verify.close()
        assert str(latest.batch_id.iloc[0])==BATCH_ID
        assert int(current.total_rows)==len(forecast)==int(history_n)
        assert int(current.date_count)==7 and int(current.batch_count)==1
        assert pd.Timestamp(current.min_date)==TODAY
        assert pd.Timestamp(current.max_date)==TARGET_DATES[-1]
        print('Read-back PASS:',current.to_dict())
    else:print('Read-back skipped (dry run).')

    if not published:
        return
    try:
        from refresh_external_data import refresh_market_watch
        refresh_market_watch()
    except Exception as exc:
        raise RuntimeError('Forecast batch published. Market Watch refresh failed; retry with '
                           'python refresh_external_data.py --market-watch-only.') from exc


if __name__ == "__main__":
    main()
