"""Refresh external inputs and provide the dashboard Market Watch rebuild."""
import argparse
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent
KEYS = ['state', 'item_code']
FESTIVALS = {'eid al-fitr': 'Aidilfitri', 'chinese new year': 'Chinese New Year',
             'diwali': 'Deepavali', 'christmas day': 'Christmas'}
WEATHER_FIELDS = ['rain_total_mm', 'temperature_mean_c', 'humidity_mean_pct',
                  'wet_streak_days', 'dry_streak_days']


def get_with_retry(url, *, params=None, attempts=4):
    """Retry transient GET failures only; TLS verification remains enabled."""
    import requests
    import time
    for attempt in range(1,attempts+1):
        try:
            response=requests.get(url,params=params,timeout=(30,120))
            response.raise_for_status()
            return response
        except (requests.exceptions.Timeout,requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,requests.exceptions.HTTPError) as exc:
            status=getattr(getattr(exc,'response',None),'status_code',None)
            if status is not None and status not in (408,425,429,500,502,503,504):
                raise
            if attempt==attempts:
                raise RuntimeError(f'External source unavailable after {attempts} attempts ({type(exc).__name__}). Weather progress was kept; retry with a NEW workflow run.') from exc
            delay=min(30,2**attempt)
            retry_after=getattr(getattr(exc,'response',None),'headers',{}).get('Retry-After','')
            if str(retry_after).isdigit():delay=min(60,max(delay,int(retry_after)))
            print(f'External request retry {attempt}/{attempts-1}: {type(exc).__name__}; waiting {delay}s',flush=True)
            time.sleep(delay)
    raise AssertionError('Unreachable retry state')


def calendar_results(prices, holidays):
    rows = []
    holidays = holidays.assign(festival=holidays.holiday_names.str.split(r'\s*;\s*|\s+\|\s+'))
    holidays = holidays.explode('festival')
    holidays['festival'] = holidays.festival.str.strip().str.lower().map(FESTIVALS)
    holidays = holidays.dropna(subset=['festival'])
    dates = holidays.groupby(['state', 'festival', holidays.date.dt.year]).date.min()
    for state, group in prices.groupby('state'):
        if state not in dates.index.get_level_values(0):
            continue
        for (festival, year), date in dates.loc[state].items():
            gap = (date - group.date).dt.days
            part = group.loc[gap.between(1, 21)].assign(week=(gap-1)//7)
            weeks = part.groupby(['item_code', 'week']).price.agg(['median', 'size'])
            for code, item in weeks.groupby(level=0):
                item = item.droplevel(0)
                if len(item) == 3 and item['size'].ge(3).all():
                    p = item['median']
                    rows.append((state, code, festival, year,
                                 round(p[0]-p[1], 2), round(p[1]-p[2], 2)))
    return pd.DataFrame(rows, columns=KEYS+['festival', 'year', 'final_week_change_rm',
                                            'earlier_week_change_rm'])


def weather_results(prices, weather, eligible):
    parts = []
    for state, group in weather.groupby('state'):
        daily = group.set_index('date').sort_index().asfreq('D')
        rain = daily.precipitation_sum.where(daily.precipitation_sum.ge(0))
        daily['precipitation_sum'] = rain
        for field, mask in [('wet_streak_days', rain.ge(1)), ('dry_streak_days', rain.lt(1))]:
            daily[field] = mask.astype(int).groupby((~mask).cumsum()).cumsum()
        daily['week'] = daily.index.to_period('W-SUN').start_time
        weeks = daily.groupby('week')
        inputs = ['precipitation_sum', 'temperature_2m_mean', 'relative_humidity_2m_mean']
        complete = weeks[inputs].count().eq(7).all(axis=1)
        part = weeks.agg(rain_total_mm=('precipitation_sum', 'sum'),
                         temperature_mean_c=('temperature_2m_mean', 'mean'),
                         humidity_mean_pct=('relative_humidity_2m_mean', 'mean'),
                         wet_streak_days=('wet_streak_days', 'max'),
                         dry_streak_days=('dry_streak_days', 'max'))
        parts.append(part.loc[complete].reset_index().assign(state=state))
    if not parts:
        raise ValueError('No daily weather records available.')
    p = prices.assign(week=prices.date.dt.to_period('W-SUN').dt.start_time)
    p = p.groupby(KEYS+['week']).price.agg(['median', 'size']).reset_index()
    p = p.loc[p['size'].ge(3), KEYS+['week', 'median']]
    following = p.rename(columns={'median': 'next_price'}).copy()
    following['week'] -= pd.Timedelta(days=7)
    matched = p.merge(following, on=KEYS+['week']).merge(pd.concat(parts), on=['state', 'week'])
    matched['change'] = matched.next_price-matched['median']
    matched['month'] = matched.week.dt.month
    matched['year'] = matched.week.dt.year
    groups = KEYS+['month']
    matched = matched.loc[matched.groupby(groups).year.transform('nunique').ge(2)].copy()
    fields = WEATHER_FIELDS+['change']
    matched[fields] -= matched.groupby(groups)[fields].transform('mean')
    rows = []
    for (state, code), group in matched.groupby(KEYS):
        correlations = [group[field].corr(group.change)
                        if len(group) >= 3 and group[[field, 'change']].nunique().min() > 1
                        else None for field in WEATHER_FIELDS]
        rows.append((state, code, len(group), *correlations))
    summary = pd.DataFrame(rows, columns=KEYS+['comparable_weeks']+
                           [f'{field}_pearson_r' for field in WEATHER_FIELDS])
    summary = eligible.merge(summary, on=KEYS, how='left')
    summary['comparable_weeks'] = summary.comparable_weeks.fillna(0).astype(int)
    return summary


def cpi_results(prices, cpi, today):
    def changes(data, keys, field):
        data = data.sort_values(keys+['month']).copy()
        groups = data.groupby(keys)
        adjacent = data.month.eq(groups.month.shift()+1)
        data[field+'_mom_pct'] = ((data[field]/groups[field].shift()-1)*100).where(adjacent)
        return data
    cutoff = today.to_period('M')
    prices = prices.assign(month=prices.date.dt.to_period('M'))
    monthly = prices.groupby(KEYS+['month']).price.agg(price='median', days='size').reset_index()
    monthly = changes(monthly[monthly.days.ge(10) & monthly.month.lt(cutoff)], KEYS, 'price')
    cpi = cpi.assign(month=cpi.date.dt.to_period('M'))
    cpi = changes(cpi[cpi.month.lt(cutoff)], ['state'], 'food_cpi')
    return monthly.merge(cpi[['state', 'month', 'food_cpi_mom_pct']],
                         on=['state', 'month']).dropna(subset=['price_mom_pct', 'food_cpi_mom_pct'])


def refresh_market_watch():
    today = pd.Timestamp(datetime.now(ZoneInfo('Asia/Kuala_Lumpur')).date())
    with duckdb.connect(str(ROOT/'data/database/agrovista.duckdb'), read_only=True) as con:
        con.execute("""
            CREATE TEMP TABLE eligible AS
            WITH latest AS (
                SELECT batch_id FROM agrovista_v2_forecast_current
                GROUP BY batch_id ORDER BY MAX(generated_at_utc) DESC, batch_id DESC LIMIT 1
            )
            SELECT DISTINCT state, item_code FROM (
                SELECT state, item_code FROM agrovista_v2_forecast_current
                JOIN latest USING (batch_id)
                WHERE level='state' AND state IS NOT NULL AND item_code IS NOT NULL
                GROUP BY state,item_code,premise_code,forecast_today
                HAVING COUNT(*)=7 AND COUNT(DISTINCT CAST(forecast_date AS DATE))=7
                   AND COUNT(*) FILTER (WHERE predicted_price>0 AND isfinite(predicted_price))=7
                   AND MIN(CAST(forecast_date AS DATE)-CAST(forecast_today AS DATE))=0
                   AND MAX(CAST(forecast_date AS DATE)-CAST(forecast_today AS DATE))=6
            ) options
        """)
        eligible = con.execute('SELECT * FROM eligible').df()
        prices = con.execute("""
            SELECT state,item_code,CAST(date AS DATE) AS date,median(price) AS price
            FROM clean_joined_prices JOIN eligible USING(state,item_code)
            WHERE CAST(date AS DATE) < ? AND price>0 AND isfinite(price)
            GROUP BY 1,2,3
        """, [today.date()]).df()
        holidays = con.execute("""
            SELECT state, CAST(date AS DATE) AS date, holiday_names
            FROM diagnostic_holiday_events WHERE CAST(date AS DATE) <= ?
        """, [today.date()]).df()
        cpi = con.execute("""
            SELECT state, CAST(date AS DATE) AS date, food_cpi
            FROM diagnostic_food_cpi_monthly WHERE food_cpi>0 AND isfinite(food_cpi)
        """).df()
    if eligible.empty or prices.empty:
        raise ValueError('The latest batch has no eligible State options with observed prices.')
    weather_path=ROOT/'data/external/final_rebuild/weather_state_daily.csv'
    if not weather_path.is_file():
        raise FileNotFoundError('Full weather cache missing; run refresh_external_data.py before Market Watch.')
    weather=pd.read_csv(weather_path,parse_dates=['date'])
    required_weather={'state','date','precipitation_sum','temperature_2m_mean','relative_humidity_2m_mean'}
    if not required_weather<=set(weather):
        raise ValueError('Full weather cache lacks temperature/humidity/rainfall fields.')
    results = {
        'calendar_yearly.csv': calendar_results(prices, holidays),
        'weather_full_summary.csv': weather_results(prices, weather[weather.date.lt(today)], eligible),
        'cpi_monthly_context.csv': cpi_results(prices, cpi, today),
    }
    with duckdb.connect(str(ROOT/'data/database/agrovista.duckdb')) as con:
        con.execute('BEGIN')
        try:
            for filename, frame in results.items():
                for column in [c for c in frame if isinstance(frame[c].dtype, pd.PeriodDtype)]:
                    frame[column] = frame[column].astype(str)
                con.register('watch_stage', frame)
                table = 'diagnostic_' + Path(filename).stem
                con.execute(f'CREATE OR REPLACE TABLE {table} AS SELECT * FROM watch_stage')
                con.unregister('watch_stage')
            con.execute('COMMIT')
        except Exception:
            con.execute('ROLLBACK')
            raise
    out = ROOT/'data/audit/final_rebuild/diagnostic_full'
    out.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=out) as temp:
        for name, frame in results.items():
            frame.to_csv(Path(temp)/name, index=False)
        for name in results:
            (Path(temp)/name).replace(out/name)
    print('Market Watch updated:', out, flush=True)



def refresh_external_data():
    from datetime import timedelta
    import json
    import numpy as np

    database = ROOT/'data/database/agrovista.duckdb'
    if not database.is_file():
        raise FileNotFoundError(database)
    destination = ROOT/'data/external/final_rebuild'
    destination.mkdir(parents=True, exist_ok=True)
    today = datetime.now(ZoneInfo('Asia/Kuala_Lumpur')).date()
    with duckdb.connect(str(database), read_only=True) as con:
        first, last = con.execute("""SELECT MIN(CAST(date AS DATE)), MAX(CAST(date AS DATE))
            FROM clean_joined_prices WHERE state IS NOT NULL AND CAST(date AS DATE) <= ?""", [today]).fetchone()
        STATES = [row[0] for row in con.execute("""SELECT DISTINCT state FROM clean_joined_prices
            WHERE state IS NOT NULL AND CAST(date AS DATE) <= ? ORDER BY state""", [today]).fetchall()]
    if first is None or last is None or not STATES:
        raise ValueError('No dated PriceCatcher observations with state metadata.')
    START, END = first, last
    weather_end = min(END, today-timedelta(days=1))
    calendar_end = today+timedelta(days=6)


    STATE_COORDS={
    'Johor':(1.50,103.70),'Kedah':(6.10,100.40),'Kelantan':(6.10,102.25),
    'Melaka':(2.20,102.25),'Negeri Sembilan':(2.70,102.20),'Pahang':(3.80,103.30),
    'Perak':(4.60,101.10),'Perlis':(6.40,100.20),'Pulau Pinang':(5.40,100.30),
    'Sabah':(5.40,116.80),'Sarawak':(2.80,113.50),'Selangor':(3.20,101.50),
    'Terengganu':(5.10,103.00),'W.P. Kuala Lumpur':(3.14,101.69),
    'W.P. Labuan':(5.28,115.24),'W.P. Putrajaya':(2.93,101.69)}


    weather_columns=['temperature_2m_mean','precipitation_sum','relative_humidity_2m_mean']
    cached_path=destination/'weather_state_daily.csv'
    checkpoint_path=destination/'.weather_checkpoint.csv'
    required={'state','date',*weather_columns}
    cache_parts=[]
    for cache_file in (checkpoint_path,cached_path):
        if not cache_file.is_file():continue
        part=pd.read_csv(cache_file)
        if part.empty:continue
        if not required<=set(part.columns):
            raise ValueError(f'Weather cache is missing {required-set(part.columns)}')
        part['date']=pd.to_datetime(part.date,errors='raise').dt.normalize()
        if part.duplicated(['state','date']).any():
            raise ValueError('Duplicate state/date keys in weather cache.')
        for field in weather_columns:
            part[field]=pd.to_numeric(part[field],errors='raise')
            if np.isinf(part[field]).any():raise ValueError('Infinite weather cache values')
        cache_parts.append(part[['date','state',*weather_columns]])
    cached=(pd.concat(cache_parts,ignore_index=True).drop_duplicates(['state','date'],keep='last')
            if cache_parts else pd.DataFrame(columns=['date','state',*weather_columns]))
    if weather_end<START:
        raise ValueError('No completed weather dates in the observed price window.')
    expected_dates=pd.date_range(START,weather_end)
    frames=[]
    for state in STATES:
        if state not in STATE_COORDS:
            raise ValueError(f'No reference coordinates for {state}')
        existing=(cached.loc[cached.state.eq(state)&cached.date.isin(expected_dates),
                            ['date','state',*weather_columns]].copy()
                  if not cached.empty else pd.DataFrame(columns=['date','state',*weather_columns]))
        missing=expected_dates.difference(existing.date)
        if len(missing):
            lat,lon=STATE_COORDS[state]
            while len(missing):
                first=missing.min()
                last=min(first+pd.Timedelta(days=365),missing.max())
                needed=missing[(missing>=first)&(missing<=last)]
                params={'latitude':lat,'longitude':lon,'start_date':str(first.date()),
                        'end_date':str(last.date()),'daily':','.join(weather_columns),
                        'timezone':'Asia/Kuala_Lumpur'}
                print(f'Weather {state}: requesting {first.date()} to {last.date()}',flush=True)
                response=get_with_retry('https://archive-api.open-meteo.com/v1/archive',params=params)
                fresh=pd.DataFrame(response.json()['daily']).rename(columns={'time':'date'})
                fresh['date']=pd.to_datetime(fresh.date,errors='raise').dt.normalize()
                fresh['state']=state
                fresh=fresh.loc[fresh.date.isin(needed),['date','state',*weather_columns]]
                if fresh.date.duplicated().any() or len(needed.difference(fresh.date)):
                    raise ValueError(f'Incomplete archive dates for {state}; existing published files retained.')
                for field in weather_columns:
                    fresh[field]=pd.to_numeric(fresh[field],errors='raise')
                    if np.isinf(fresh[field]).any():raise ValueError('Infinite archive weather values')
                existing=fresh.copy() if existing.empty else pd.concat([existing,fresh],ignore_index=True)
                cached=fresh.copy() if cached.empty else pd.concat([cached,fresh],ignore_index=True)
                cached=cached.drop_duplicates(['state','date'],keep='last')
                # Separate progress file: never present partial weather as a published dataset.
                temporary=checkpoint_path.with_suffix('.tmp')
                cached.to_csv(temporary,index=False)
                temporary.replace(checkpoint_path)
                missing=expected_dates.difference(existing.date)
        frames.append(existing)
        print(f'Weather {state}: {len(existing)} dates ready (checkpoint saved)',flush=True)
    weather=pd.concat(frames,ignore_index=True).sort_values(['state','date'])
    for field in weather_columns:
        weather[field]=pd.to_numeric(weather[field],errors='raise')
        if np.isinf(weather[field]).any() or not weather.groupby('state')[field].count().gt(0).all():
            raise ValueError(f'Invalid weather values for {field}; existing files retained.')


    cpi_errors=[];cpi=pd.DataFrame();cpi_ok=False
    try:
        # Official full Parquet avoids a truncated, unpaginated 10,000-row API response.
        from io import BytesIO
        response=get_with_retry('https://storage.dosm.gov.my/cpi/cpi_2d_state.parquet')
        cpi=pd.read_parquet(BytesIO(response.content))
        required={'date','state','division','index'}
        if not required<=set(cpi.columns):raise ValueError(f'Missing CPI columns: {required-set(cpi.columns)}')
        cpi['date']=pd.to_datetime(cpi['date'],errors='coerce')
        cpi['index']=pd.to_numeric(cpi['index'],errors='coerce')
        cpi=cpi.dropna(subset=['date','state','division','index']).copy()
        cpi=cpi[cpi.date.between(pd.Timestamp(START),pd.Timestamp(END))].copy()
        cpi['state']=cpi['state'].replace({'Penang':'Pulau Pinang','Kuala Lumpur':'W.P. Kuala Lumpur',
                                         'Putrajaya':'W.P. Putrajaya','Labuan':'W.P. Labuan'})
        cpi['division']=cpi['division'].astype(str).str.strip()
        cpi=cpi[cpi['division'].isin(['01','overall','Overall'])].copy()
        cpi['month']=cpi['date'].dt.to_period('M').astype(str)
        if cpi.empty:raise ValueError('No food/overall CPI records')
        if cpi.duplicated(['date','state','division']).any():raise ValueError('Duplicate CPI date-state-division keys')
        cpi_ok=(set(STATES)<=set(cpi['state']))
        if not cpi_ok:raise ValueError('CPI does not cover all observed PriceCatcher states')
        print('CPI rows:',len(cpi),'latest:',cpi['month'].max())
    except Exception as exc:
        cpi_errors.append(repr(exc));cpi_ok=False
        print('CPI unavailable:',repr(exc))
    print('CPI complete:',cpi_ok)

    HOLIDAY_CODES={'Johor':'JHR','Kedah':'KDH','Kelantan':'KTN','Melaka':'MLK',
    'Negeri Sembilan':'NSN','Pahang':'PHG','Perak':'PRK','Perlis':'PLS',
    'Pulau Pinang':'PNG','Sabah':'SBH','Sarawak':'SWK','Selangor':'SGR',
    'Terengganu':'TRG','W.P. Kuala Lumpur':'KUL','W.P. Labuan':'LBN','W.P. Putrajaya':'PJY'}
    holiday_errors=[];holiday_rows=[]
    try:
        import holidays
        for state in STATES:
            code=HOLIDAY_CODES.get(state)
            if code is None:
                holiday_errors.append(f'Unknown state {state}');continue
            try:
                calendar=holidays.country_holidays('MY',subdiv=code,years=range(START.year,calendar_end.year+1))
                for d,name in calendar.items():
                    if START<=d<=calendar_end:
                        holiday_rows.append({'date':str(d),'state':state,'holiday_name':str(name),'is_public_holiday':1})
            except Exception as exc:holiday_errors.append(f'{state}: {exc!r}')
    except ImportError:
        holiday_errors.append('Missing dependency: pip install holidays')
    holiday_df=pd.DataFrame(holiday_rows)
    holiday_ok=not holiday_errors and not holiday_df.empty and set(STATES)<=set(holiday_df['state'])
    if holiday_ok:
        holiday_daily=holiday_df.groupby(['date','state'],as_index=False).agg(
            is_public_holiday=('is_public_holiday','max'),
            holiday_names=('holiday_name',lambda x:' | '.join(sorted(set(x)))))
        if holiday_daily.duplicated(['date','state']).any():raise AssertionError('Duplicate holiday keys')
    print('Holiday complete:',holiday_ok,'errors:',holiday_errors)

    if not cpi_ok or not holiday_ok:
        raise RuntimeError(f'External data incomplete. Existing files retained. CPI: {cpi_errors}; holidays: {holiday_errors}')
    food=cpi.loc[cpi.division.eq('01'),['state','date','month','index']].rename(columns={'index':'food_cpi'})
    food=food.loc[food.food_cpi.gt(0)&np.isfinite(food.food_cpi)].copy()
    if not set(STATES)<=set(food.state) or food.duplicated(['state','month']).any():
        raise ValueError('Invalid food CPI coverage or duplicate monthly keys.')
    rain=weather[['state','date','precipitation_sum']].rename(columns={'precipitation_sum':'rain_mm'}).copy()
    rain['rain_mm']=pd.to_numeric(rain.rain_mm,errors='coerce')
    rain['rain_mm']=rain.rain_mm.where(rain.rain_mm.ge(0)&np.isfinite(rain.rain_mm))
    events=holiday_daily[['state','date','holiday_names']].copy()
    events['date']=pd.to_datetime(events.date,errors='raise')
    tables={'diagnostic_weather_daily':rain,'diagnostic_holiday_events':events,'diagnostic_food_cpi_monthly':food}
    csvs={'weather_state_daily.csv':weather,'holiday_state_daily.csv':holiday_daily,
          'cpi_state_food_and_overall.csv':cpi}
    manifest={'generated_at':datetime.now(ZoneInfo('Asia/Kuala_Lumpur')).isoformat(timespec='seconds'),
              'source_start':str(START),'source_end':str(calendar_end),
              'price_source_end':str(END),'weather_archive_requested_end':str(weather_end),
              'states':STATES,'datasets':{name:{'complete':True,'rows':len(frame),'errors':[]}
                      for name,frame in [('weather',weather),('holidays',events),('cpi',cpi)]}}
    with TemporaryDirectory(prefix='external_',dir=destination) as tmp:
        stage=Path(tmp)
        for name,frame in csvs.items():
            frame.to_csv(stage/name,index=False)
        (stage/'external_manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
        with duckdb.connect(str(database)) as con:
            con.execute('BEGIN TRANSACTION')
            try:
                for name,frame in tables.items():
                    con.register('external_stage',frame)
                    con.execute(f'CREATE OR REPLACE TABLE {name} AS SELECT * FROM external_stage')
                    con.unregister('external_stage')
                con.execute('COMMIT')
            except Exception:
                con.execute('ROLLBACK')
                raise
        for name in [*csvs,'external_manifest.json']:
            (stage/name).replace(destination/name)
    print(f'Published external data: weather through {weather_end}, holidays through {calendar_end}.')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--market-watch-only',action='store_true')
    args=parser.parse_args()
    if args.market_watch_only:
        refresh_market_watch()
    else:
        refresh_external_data()


if __name__ == '__main__':
    main()
