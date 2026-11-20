"""Offline Overview calculations copied from the existing app, with no Streamlit dependency.
Keep calculations in sync with app.py. Parity tests compare both implementations.
"""
from pathlib import Path
import pandas as pd
FORECAST='agrovista_v2_forecast_current'
def ident(v):
    return '"' + str(v).replace('"', '""') + '"'

def signature(db):
    db = Path(db)
    s = db.stat()
    wal = Path(str(db) + '.wal')
    return (s.st_mtime_ns, s.st_size, wal.stat().st_mtime_ns if wal.exists() else 0)

class Store:

    def __init__(self, db):
        db = Path(db)
        self.db = str(db)
        self.stamp = signature(db)
        self.tables = set(self.q('SHOW TABLES')['name'])
        self.cols = {t: set(self.q(f'DESCRIBE {ident(t)}')['column_name']) for t in ['lookup_items', 'lookup_premises', 'clean_joined_prices', FORECAST, 'daily_state_prices'] if t in self.tables}
        if 'clean_joined_prices' not in self.tables:
            raise RuntimeError('Missing clean_joined_prices from Notebook 02. Historical and premise analysis need this table.')
        needed = {'date', 'state', 'item_code', 'premise_code', 'price'}
        missing = needed - self.cols['clean_joined_prices']
        if missing:
            raise RuntimeError('Historical table is missing columns: ' + ', '.join(sorted(missing)))
        self.source = self.make_source()

    def q(self, query, params=()):
        return self._connection.execute(query, params).fetchdf()

    def make_source(self):
        cc = self.cols['clean_joined_prices']
        joins = []
        fields = ['c.date', 'c.state', 'c.item_code', 'c.premise_code', 'c.price']
        for table, alias, key in [('lookup_items', 'i', 'item_code'), ('lookup_premises', 'p', 'premise_code')]:
            if table in self.tables and key in self.cols[table]:
                joins.append(f'LEFT JOIN (SELECT * EXCLUDE(rn) FROM (SELECT *, ROW_NUMBER() OVER(PARTITION BY {key}) rn FROM {table}) WHERE rn=1) {alias} ON c.{key}={alias}.{key}')
        for name, alias, table, fallback in [('item', 'i', 'lookup_items', "'Item ' || CAST(c.item_code AS VARCHAR)"), ('unit', 'i', 'lookup_items', "''"), ('item_category', 'i', 'lookup_items', "'Uncategorised'"), ('premise', 'p', 'lookup_premises', "'Premise ' || CAST(c.premise_code AS VARCHAR)"), ('district', 'p', 'lookup_premises', "'Unknown district'"), ('premise_type', 'p', 'lookup_premises', "'Unknown type'"), ('address', 'p', 'lookup_premises', "''")]:
            candidates = []
            if name in cc:
                candidates.append(f"NULLIF(CAST(c.{ident(name)} AS VARCHAR),'')")
            if table in self.tables and name in self.cols[table] and (('item_code' if alias == 'i' else 'premise_code') in self.cols[table]):
                candidates.append(f"NULLIF(CAST({alias}.{ident(name)} AS VARCHAR),'')")
            fields.append('COALESCE(' + ','.join(candidates + [fallback]) + f') AS {ident(name)}' if candidates else fallback + f' AS {ident(name)}')
        return '(SELECT ' + ','.join(fields) + ' FROM clean_joined_prices c ' + ' '.join(joins) + ')'

    def snapshot(self):
        if '_cached_snapshot' in self.__dict__:
            return self._cached_snapshot
        d = self.q('SELECT MAX(CAST(date AS DATE)) last_date FROM clean_joined_prices')
        if pd.notna(d.iloc[0, 0]):
            return pd.Timestamp(d.iloc[0, 0])
        if 'daily_state_prices' in self.tables and 'date' in self.cols['daily_state_prices']:
            d = self.q('SELECT MAX(CAST(date AS DATE)) last_date FROM daily_state_prices')
            if pd.notna(d.iloc[0, 0]):
                return pd.Timestamp(d.iloc[0, 0])
        return None

    def eligible(self):
        if hasattr(self, '_eligible'):
            return self._eligible
        batch = self.batch()
        if batch is None:
            self._eligible = pd.DataFrame(columns=['level', 'state', 'item_code', 'premise_code'])
        else:
            self._eligible = self.q(f"SELECT level,state,item_code,premise_code\n            FROM {FORECAST} WHERE batch_id=? AND level IN ('state','premise')\n            AND state IS NOT NULL AND item_code IS NOT NULL\n            AND (level='state' OR premise_code IS NOT NULL)\n            GROUP BY level,state,item_code,premise_code,forecast_today\n            HAVING COUNT(*)=7 AND COUNT(DISTINCT CAST(forecast_date AS DATE))=7\n            AND COUNT(*) FILTER(WHERE predicted_price>0 AND isfinite(predicted_price))=7\n            AND MIN(CAST(forecast_date AS DATE)-CAST(forecast_today AS DATE))=0\n            AND MAX(CAST(forecast_date AS DATE)-CAST(forecast_today AS DATE))=6", [str(batch.batch_id)])
        return self._eligible

    def catalogue(self, root):
        codes = set((int(x) for x in self.eligible().item_code))
        if not codes:
            return pd.DataFrame(columns=['item_code', 'item', 'unit', 'item_category', 'label'])
        if 'lookup_items' in self.tables and {'item_code', 'item'} <= self.cols['lookup_items']:
            fields = [ident(c) if c in self.cols['lookup_items'] else f"'' AS {ident(c)}" for c in ['item_code', 'item', 'unit', 'item_category']]
            d = self.q('SELECT ' + ','.join(fields) + ' FROM lookup_items').drop_duplicates('item_code')
        else:
            d = self.q(f'SELECT DISTINCT item_code,item,unit,item_category FROM {self.source}')
        if codes:
            d = d[d.item_code.isin(codes)].copy()
            known = set(d.item_code)
            missing = [{'item_code': c, 'item': f'Item {c}', 'unit': '', 'item_category': 'Uncategorised'} for c in codes - known]
            if missing:
                d = pd.concat([d, pd.DataFrame(missing)], ignore_index=True)
        d = d.dropna(subset=['item_code'])
        d.item_code = d.item_code.astype(int)
        for col in ['item', 'unit', 'item_category']:
            d[col] = d[col].fillna('').astype(str)
        d['label'] = d['item'] + ' · ' + d.unit + ' [' + d.item_code.astype(str) + ']'
        return d.sort_values(['item', 'item_code']).reset_index(drop=True)

    def batch(self):
        if '_cached_batch' in self.__dict__:
            return self._cached_batch
        req = {'batch_id', 'forecast_today', 'forecast_date', 'level', 'state', 'item_code', 'premise_code', 'predicted_price'}
        if FORECAST not in self.tables or not req <= self.cols[FORECAST]:
            return None
        sort = 'MAX(generated_at_utc)' if 'generated_at_utc' in self.cols[FORECAST] else 'MAX(forecast_today)'
        d = self.q(f'SELECT batch_id,MAX(forecast_today) forecast_today,{sort} AS generated_time FROM {FORECAST} GROUP BY batch_id ORDER BY generated_time DESC,batch_id DESC LIMIT 1')
        return d.iloc[0] if len(d) else None

def overview_period_prices(store, end, days, codes, state_clause, state_params):
    return store.q(f"SELECT\n        CAST(FLOOR(DATE_DIFF('day', CAST(date AS DATE), CAST(? AS DATE))/{days}) AS INTEGER) AS period_age,\n        item_code,state,premise_code,MEDIAN(price) AS price\n        FROM clean_joined_prices\n        WHERE date >= CAST(? AS DATE) AND date < CAST(? AS DATE) + INTERVAL 1 DAY\n            AND item_code IN (SELECT UNNEST(?::BIGINT[]))\n            AND state IS NOT NULL AND premise_code IS NOT NULL\n            AND price > 0 AND isfinite(price){state_clause}\n        GROUP BY period_age,item_code,state,premise_code", [str(end.date()), str((end - pd.Timedelta(days=2 * days - 1)).date()), str(end.date()), codes, *state_params])

def overview_mover_changes(prices):
    recent = prices.loc[prices.period_age.eq(0)]
    previous = prices.loc[prices.period_age.eq(1)]
    matched = recent.merge(previous, on=['item_code', 'state', 'premise_code'], suffixes=('_recent', '_earlier'))
    matched['change_pct'] = 100 * (matched.price_recent / matched.price_earlier - 1)
    changes = matched.groupby('item_code').change_pct.agg(change_pct='median', matched_premises='size').reset_index()
    changes['change_pct'] = changes.change_pct.round(1)
    return changes[changes.matched_premises.ge(3) & changes.change_pct.ne(0)]

def compute_summary(database, stamp, snap, scoped_reports, scoped_dates, codes, state, _store=None):
    store = _store
    state_clause = ' AND state=?' if state is not None else ''
    state_params = [state] if state is not None else []
    totals = store.q(f"SELECT COUNT(DISTINCT premise_code) premises, COUNT(DISTINCT state) states,\n        COUNT(DISTINCT NULLIF(TRIM(district),'')) FILTER (\n            WHERE premise_code IS NOT NULL AND LOWER(TRIM(district)) NOT IN ('unknown','unknown district')\n        ) AS districts,\n        COUNT(DISTINCT premise_type) FILTER (\n            WHERE premise_code IS NOT NULL AND premise_type NOT IN ('Unknown','Unknown type')\n        ) AS premise_type_count {scoped_reports}", scoped_dates).iloc[0]
    item_counts = store.q(f'SELECT item_code, COUNT(DISTINCT premise_code) reporters\n        {scoped_reports} GROUP BY item_code', scoped_dates)
    types = store.q(f'WITH type_counts AS (\n        SELECT premise_code, premise_type, COUNT(*) AS observations\n        {scoped_reports} AND premise_code IS NOT NULL AND premise_type IS NOT NULL\n        GROUP BY premise_code,premise_type\n    ), selected_type AS (\n        SELECT premise_code,premise_type AS premise_type_name\n        FROM type_counts\n        QUALIFY ROW_NUMBER() OVER (\n            PARTITION BY premise_code ORDER BY observations DESC,premise_type ASC\n        )=1\n    ) SELECT premise_type_name,COUNT(*) AS premises\n      FROM selected_type GROUP BY premise_type_name\n      ORDER BY premises DESC,premise_type_name', scoped_dates)
    codes = list(codes)
    recent_end = pd.Timestamp(store.q(f'SELECT MAX(CAST(date AS DATE)) AS latest\n        FROM clean_joined_prices WHERE state IS NOT NULL AND premise_code IS NOT NULL\n        AND price > 0 AND isfinite(price){state_clause}', state_params).iloc[0].latest)
    if pd.isna(recent_end):
        recent_end = snap
    weekly_prices = overview_period_prices(store, recent_end, 7, codes, state_clause, state_params)
    movers = overview_mover_changes(weekly_prices)
    mover_days = 7
    if len(movers) < 6:
        fortnight_prices = overview_period_prices(store, recent_end, 14, codes, state_clause, state_params)
        fortnight_movers = overview_mover_changes(fortnight_prices)
        if len(fortnight_movers) > len(movers):
            movers, mover_days = (fortnight_movers, 14)
    return (totals, item_counts, types, recent_end, movers, mover_days, weekly_prices)
