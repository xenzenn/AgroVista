"""Exact monthly price pulse with only two monthly summaries in memory."""
import pandas as pd

def monthly_pulse(store,first_month,last_month,codes,state_clause,state_params,progress=None):
    previous=None;results=[]
    months=pd.period_range(first_month,last_month,freq='M')
    for index,month in enumerate(months):
        current=store.q(f'''SELECT state,item_code,premise_code,MEDIAN(price) AS price
            FROM clean_joined_prices
            WHERE date>=CAST(? AS DATE) AND date<CAST(? AS DATE)
              AND state IS NOT NULL AND premise_code IS NOT NULL
              AND item_code IN (SELECT UNNEST(?::BIGINT[]))
              AND price>0 AND isfinite(price){state_clause}
            GROUP BY state,item_code,premise_code''',
            [str(month.start_time.date()),str((month+1).start_time.date()),codes,*state_params])
        if previous is not None and not previous.empty and not current.empty:
            matched=current.merge(previous,on=['state','item_code','premise_code'],suffixes=('_current','_previous'),validate='one_to_one')
            matched['change_pct']=100*(matched.price_current/matched.price_previous-1)
            summary=matched.groupby('item_code').change_pct.agg(['median','size'])
            summary=summary.loc[summary['size'].ge(3),['median']].rename(columns={'median':'change_pct'}).reset_index()
            summary.insert(0,'month',month.start_time)
            results.append(summary)
        previous=current
        if progress:progress(index+1,len(months))
    return pd.concat(results,ignore_index=True) if results else pd.DataFrame(columns=['month','item_code','change_pct'])
