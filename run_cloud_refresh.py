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
            model=joblib.load(folder/'selected_direct_model.joblib')
            manifest=json.loads((folder/'manifest.json').read_text())
            validate_contract(model)
            for key in ('level','columns','algorithm','architecture','window_size','configuration','supported_horizons'):
                if model[key]!=manifest[key]:raise ValueError(f'{level} model/manifest mismatch: {key}')
            if model['level']!=level or model['algorithm']!=algorithm:raise ValueError('Wrong model placed in folder')
            print(f'Validated {level}: {algorithm}, sklearn {sklearn.__version__}',flush=True)

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
    validate_models()
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

if __name__=='__main__':main()
