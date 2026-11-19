"""Training and inference helpers for pooled versus horizon-specific LightGBM.
Artifacts contain standard sklearn pipelines only; no custom estimator is pickled.
"""
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from lightgbm import LGBMRegressor

ARCHITECTURES = {'pooled', 'per_horizon'}

def numeric_features(frame, columns):
    missing=set(columns)-set(frame.columns)
    if missing:raise ValueError(f'Missing trained features: {sorted(missing)}')
    return frame[list(columns)].replace([np.inf,-np.inf],np.nan).apply(pd.to_numeric,errors='coerce')

def fit_candidate(frame, columns, params, target_mode, architecture='pooled', horizons=range(1,22), min_rows=40):
    if architecture not in ARCHITECTURES:raise ValueError(f'Unknown architecture: {architecture}')
    if target_mode not in {'absolute','delta'}:raise ValueError('Unknown target mode')
    expected=sorted(set(map(int,horizons)))
    columns=list(columns)
    if architecture=='per_horizon':columns=[c for c in columns if c!='horizon_days']
    if not columns:raise ValueError('No predictor columns')
    result={'architecture':architecture,'columns':columns,'target_mode':target_mode,
            'supported_horizons':expected,'pipeline':None,'horizon_pipelines':{}}
    counts=[]
    groups=[(None,frame)] if architecture=='pooled' else [(h,frame.loc[frame.horizon_days.eq(h)]) for h in expected]
    for horizon,group in groups:
        if len(group)<min_rows:raise ValueError(f'Horizon {horizon}: {len(group)} training rows, need {min_rows}; bundle rejected without publishing')
        x=numeric_features(group,columns)
        # All-empty columns are rejected for this candidate; no silent per-horizon schema drift.
        if x.isna().all().any():raise ValueError(f'Horizon {horizon}: all-empty features {x.columns[x.isna().all()].tolist()}')
        y=(group.target_price-group.last_price if target_mode=='delta' else group.target_price)
        if not np.isfinite(np.asarray(y,dtype=float)).all():raise ValueError('Nonfinite training labels')
        pipe=Pipeline([('imputer',SimpleImputer(strategy='median')),('model',LGBMRegressor(**params))])
        pipe.fit(x,y)
        if horizon is None:result['pipeline']=pipe
        else:result['horizon_pipelines'][int(horizon)]=pipe
        counts.append({'horizon':horizon,'training_rows':len(group)})
    result['training_counts']=counts
    return result

def validate_contract(artifact):
    arch=artifact.get('architecture','pooled')
    if arch not in ARCHITECTURES:raise ValueError(f'Unsupported architecture: {arch}')
    if arch=='per_horizon':
        models=artifact.get('horizon_pipelines',{})
        expected=set(map(int,artifact['supported_horizons']))
        if set(models)!=expected:raise ValueError(f'Horizon model coverage mismatch: missing {expected-set(models)}, extra {set(models)-expected}')
        if 'horizon_days' in artifact['columns']:raise ValueError('Per-horizon predictors must exclude the constant horizon feature')
        if not all(hasattr(model,'predict') for model in models.values()):raise ValueError('Invalid horizon pipeline')
    elif not hasattr(artifact.get('pipeline'),'predict'):raise ValueError('Invalid pooled pipeline')
    return arch

def raw_predict(artifact, frame):
    """Dispatch by positional masks, preserving row order even with repeated indices."""
    arch=validate_contract(artifact)
    if frame.empty:return np.empty(0,dtype=float)
    horizons=pd.to_numeric(frame.horizon_days,errors='raise').to_numpy(dtype=float)
    if not np.isfinite(horizons).all() or not np.equal(horizons,np.floor(horizons)).all():raise ValueError('Invalid horizon values')
    horizons=horizons.astype(int)
    allowed=set(map(int,artifact['supported_horizons']))
    if not set(horizons)<=allowed:raise ValueError(f'Unsupported horizons: {set(horizons)-allowed}')
    x=numeric_features(frame,artifact['columns'])
    if arch=='pooled':out=np.asarray(artifact['pipeline'].predict(x),dtype=float)
    else:
        out=np.full(len(frame),np.nan,dtype=float)
        for h in sorted(set(horizons)):
            pos=np.flatnonzero(horizons==h)
            out[pos]=np.asarray(artifact['horizon_pipelines'][h].predict(x.iloc[pos]),dtype=float)
    if out.shape!=(len(frame),) or not np.isfinite(out).all():raise ValueError('Invalid model output')
    return out

def predict_candidate(artifact,frame):
    pred=raw_predict(artifact,frame)
    if artifact['target_mode']=='delta':pred=frame.last_price.to_numpy(dtype=float)+pred
    elif artifact['target_mode']!='absolute':raise ValueError('Unknown target mode')
    return np.maximum(0.01,pred)

def flatness_report(frame, prediction_column='predicted'):
    """Only evaluate complete h=1..7 origin groups; flatness is diagnostic, not a selection objective."""
    near=frame.loc[frame.horizon_days.between(1,7)].copy()
    keys=[c for c in ['state','item_code','premise_code','origin_date'] if c in near]
    if not keys or near.empty:return {'complete_7day_origins':0,'exact_flat_percent':None,'cent_flat_percent':None,'actual_cent_flat_percent':None}
    rows=[]
    for _,group in near.groupby(keys,dropna=False,sort=False):
        if len(group)!=7 or set(group.horizon_days)!={1,2,3,4,5,6,7}:continue
        pred=group[prediction_column].to_numpy(dtype=float)
        rows.append((np.ptp(pred)<=1e-10,len(np.unique(np.round(pred,2)))==1,
                     len(np.unique(np.round(group.target_price.to_numpy(dtype=float),2)))==1))
    if not rows:return {'complete_7day_origins':0,'exact_flat_percent':None,'cent_flat_percent':None,'actual_cent_flat_percent':None}
    arr=np.asarray(rows,dtype=float)
    return dict(complete_7day_origins=len(rows),exact_flat_percent=float(arr[:,0].mean()*100),
                cent_flat_percent=float(arr[:,1].mean()*100),actual_cent_flat_percent=float(arr[:,2].mean()*100))
