"""Run only on GitHub Actions (or a separate copy of the deployment database)."""
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import os
import subprocess
import sys
import warnings
import joblib
import sklearn
from cloud_database import ensure_database
from cloud_updates import latest_update,sync_database,api,TAG,REPOSITORY
from update_bundle import initial_cutoff,build_bundle,connect,validate_forecasts
from agrovista_horizon_models import validate_contract

ROOT=Path(__file__).resolve().parent

def validate_models():
    # Refuse incompatible sklearn pickles, rather than silently changing predictions.
    from sklearn.exceptions import InconsistentVersionWarning
    with warnings.catch_warnings():
        warnings.simplefilter('error',InconsistentVersionWarning)
        for level,algorithm in [('state','LightGBM'),('premise','RandomForest')]:
            folder=ROOT/'models/agrovista_per_horizon_candidates'/f'{level}_level'
            for name in ('selected_direct_model.joblib','manifest.json'):
                if not (folder/name).is_file():raise FileNotFoundError(f'Missing {level} model file: {folder/name}. Commit both model folders.')
            model=joblib.load(folder/'selected_direct_model.joblib')
            manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8-sig'))
            validate_contract(model)
            for key in ('level','columns','algorithm','architecture','window_size','configuration','supported_horizons'):
                if model[key]!=manifest[key]:raise ValueError(f'{level} model/manifest mismatch: {key}')
            if model['level']!=level or model['algorithm']!=algorithm:raise ValueError('Wrong model placed in folder')
            expected_config={'state':'PriceCatcher + Weather','premise':'PriceCatcher + Holidays'}[level]
            if model['window_size']!=30 or model['configuration']!=expected_config or not set(range(1,22))<=set(model['supported_horizons']):
                raise ValueError(f'{level} model configuration/horizons do not match the refresh contract')
            print(f'Validated {level}: {algorithm}, sklearn {sklearn.__version__}',flush=True)

def preflight():
    import pandas as pd
    from refresh_external_data import load_weather_seed
    print('AgroVista checked refresh v3; commit:',os.environ.get('GITHUB_SHA','local')[:12],flush=True)
    for name in ('refresh_pricecatcher.py','refresh_external_data.py','refresh_forecasts.py',
                 'agrovista_horizon_models.py','update_bundle.py','cloud_updates.py','cloud_database.py',
                 'overview_compute.py','overview_precompute.py','pulse_history.py'):
        path=ROOT/name
        if not path.is_file():raise FileNotFoundError(f'Missing deployment file: {name}')
        compile(path.read_text(encoding='utf-8-sig'),str(path),'exec')
    scope=ROOT/'data/analysis/scope/state_scope.csv'
    if not scope.is_file():raise FileNotFoundError('Missing data/analysis/scope/state_scope.csv; commit the supplied scope.')
    frame=pd.read_csv(scope)
    if frame.empty or not {'state','item_code'}<=set(frame):raise ValueError('Empty or invalid state scope.')
    import numpy as np
    from refresh_external_data import SEED_STATES
    codes=pd.to_numeric(frame.item_code,errors='coerce')
    if codes.isna().any() or not np.isfinite(codes).all() or codes.le(0).any() or not codes.eq(np.floor(codes)).all():
        raise ValueError('Scope contains missing/noninteger/invalid item codes.')
    if frame.state.isna().any() or not set(frame.state)<=SEED_STATES:
        raise ValueError('Scope contains missing or unknown state names.')
    if 'state_eligible' in frame:
        labels=frame.state_eligible.astype(str).str.lower()
        if not labels.isin(['true','false','1','0','yes','no']).all() or not labels.isin(['true','1','yes']).any():
            raise ValueError('Scope contains invalid eligibility values or no eligible pairs.')
    load_weather_seed(ROOT,required=True)
    validate_models()
    print('PREFLIGHT PASSED: scripts, scope, weather seed, models and manifests.',flush=True)


def run_step(script,*args):
    print('\nStarting '+script,flush=True)
    subprocess.run([sys.executable,str(ROOT/script),*args],cwd=ROOT,check=True)

def publish(archive,manifest_file,token):
    # Immutable archive first, manifest last; a failed run never advertises half a bundle.
    release=api('/releases/tags/'+TAG,token)
    if release is None:
        release=api('/releases',token,method='POST',payload={'tag_name':TAG,'target_commitish':'main',
            'name':'Dashboard data updates','body':'Cumulative updates over data-initial. Keep the base release.',
            'make_latest':'false'})
    import requests
    from urllib.parse import urlencode
    headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json',
             'Content-Type':'application/octet-stream','X-GitHub-Api-Version':'2026-03-10'}
    for file in (archive,manifest_file):
        url=f'https://uploads.github.com/repos/{REPOSITORY}/releases/{int(release["id"])}/assets?'+urlencode({'name':Path(file).name})
        with Path(file).open('rb') as stream:
            response=requests.post(url,headers=headers,data=stream,timeout=(10,600))
        if response.status_code!=201:
            raise RuntimeError(f'Release upload failed (HTTP {response.status_code}); no complete update advertised.')

def main():
    preflight()
    token=os.environ.get('GITHUB_TOKEN','')
    if not token:raise RuntimeError('Missing Actions GITHUB_TOKEN. Run through the supplied workflow.')
    database=ROOT/'data/database/agrovista.duckdb'
    # The server fetches the existing base. The user does not re-upload it.
    ensure_database(database)
    previous=latest_update(token)
    if previous:
        print('Restoring last successful update:',previous['update_id'],flush=True)
        sync_database(database,previous,external_dir=ROOT/'data/external/final_rebuild')
    cutoff=initial_cutoff(database)
    run_step('refresh_pricecatcher.py','--root',str(ROOT))
    run_step('refresh_external_data.py')
    run_step('refresh_forecasts.py') # Includes Market Watch refresh and validation.
    from overview_precompute import build_precomputed
    print('Building prepared Overview:',build_precomputed(database),flush=True)
    today=datetime.now(ZoneInfo('Asia/Kuala_Lumpur')).date().isoformat()
    with connect(database,True) as con:validate_forecasts(con,today)
    update_id=os.environ.get('GITHUB_RUN_ID','manual')+'-'+os.environ.get('GITHUB_RUN_ATTEMPT','1')
    output=ROOT/'cloud_output';output.mkdir(exist_ok=True)
    archive=output/f'update-{update_id}.zip'
    manifest=build_bundle(database,archive,update_id,cutoff,ROOT/'data/external/final_rebuild')
    manifest['run_url']=f'https://github.com/{REPOSITORY}/actions/runs/'+os.environ.get('GITHUB_RUN_ID','')
    pointer=output/f'manifest-{update_id}.json'
    pointer.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    publish(archive,pointer,token)
    print(f'Published update {update_id}: {archive.stat().st_size/1024**2:.1f} MiB (not the full database).',flush=True)
    summary=os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary,'a',encoding='utf-8') as out:
            out.write(f"## Data refresh succeeded\n\nLatest prices: {manifest['source_last']}\n\nForecast: {manifest['forecast_today']}\n\nUpdate transfer: {manifest['archive_bytes']/1024**2:.1f} MiB.\n")

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--preflight-only',action='store_true')
    args=parser.parse_args()
    if args.preflight_only:preflight()
    else:main()
