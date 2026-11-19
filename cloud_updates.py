"""GitHub transport for immutable, checksum-verified update bundles."""
from pathlib import Path
import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from update_bundle import apply_bundle,current_state

REPOSITORY='xenzenn/AgroVista'
TAG='data-updates'
WORKFLOW='refresh-data.yml'

def api(path,token='',method='GET',payload=None):
    headers={'Accept':'application/vnd.github+json','User-Agent':'AgroVista-refresh',
             'X-GitHub-Api-Version':'2026-03-10'}
    if token:headers['Authorization']='Bearer '+token
    data=json.dumps(payload).encode() if payload is not None else None
    if data:headers['Content-Type']='application/json'
    request=urllib.request.Request('https://api.github.com/repos/'+REPOSITORY+path,data=data,headers=headers,method=method)
    try:
        with urllib.request.urlopen(request,timeout=30) as response:
            content=response.read(4*1024**2)
            return json.loads(content) if content else None
    except urllib.error.HTTPError as exc:
        if exc.code==404 and method=='GET' and path.startswith('/releases/tags/'):return None
        raise RuntimeError(f'GitHub request failed (HTTP {exc.code}). Check repository permissions or service status.') from None

def _public_asset(url):
    if not isinstance(url,str) or not url.startswith(f'https://github.com/{REPOSITORY}/releases/download/{TAG}/'):
        raise ValueError('Unexpected update download location')
    return url

def latest_update(token=''):
    release=api('/releases/tags/'+TAG,token)
    if release is None:return None
    assets=[]
    for page in range(1,21):
        batch=api(f'/releases/{int(release["id"])}/assets?per_page=100&page={page}',token)
        assets.extend(batch)
        if len(batch)<100:break
    manifests=[a for a in assets if re.fullmatch(r'manifest-[A-Za-z0-9_-]+\.json',a['name']) and a['state']=='uploaded']
    if not manifests:return None
    selected=max(manifests,key=lambda a:int(a['id']))
    request=urllib.request.Request(_public_asset(selected['browser_download_url']),headers={'User-Agent':'AgroVista-refresh'})
    with urllib.request.urlopen(request,timeout=30) as response:
        raw=response.read(1024**2+1)
    if len(raw)>1024**2:raise ValueError('Update manifest too large')
    manifest=json.loads(raw)
    if not re.fullmatch(r'update-[A-Za-z0-9_-]+\.zip',manifest.get('archive','')):raise ValueError('Invalid update asset name')
    matching=[a for a in assets if a['name']==manifest['archive'] and a['state']=='uploaded']
    if len(matching)!=1:raise ValueError('Update archive is missing or duplicated')
    asset=matching[0]
    if not 0<int(manifest['archive_bytes'])<=512*1024**2 or int(asset['size'])!=int(manifest['archive_bytes']):
        raise ValueError('Update exceeds transfer budget or has the wrong size')
    if not re.fullmatch(r'[0-9a-f]{64}',manifest.get('archive_sha256','')):raise ValueError('Invalid update checksum')
    manifest['download_url']=_public_asset(asset['browser_download_url'])
    return manifest

def sync_database(database,manifest,lock=None,external_dir=None):
    if manifest is None:return False
    # Re-check within lock: multiple users may notice the same update simultaneously.
    from contextlib import nullcontext
    with lock if lock is not None else nullcontext():
        state=current_state(database)
        if state and state['update_id']==manifest['update_id']:return False
    fd,name=tempfile.mkstemp(prefix='cloud_update_',suffix='.zip',dir=Path(database).parent)
    try:
        digest=hashlib.sha256();received=0
        request=urllib.request.Request(_public_asset(manifest['download_url']),headers={'User-Agent':'AgroVista-refresh'})
        with os.fdopen(fd,'wb') as output:
            with urllib.request.urlopen(request,timeout=120) as response:
                while True:
                    block=response.read(4*1024**2)
                    if not block:break
                    received+=len(block)
                    if received>manifest['archive_bytes']:raise ValueError('Update transfer larger than expected')
                    digest.update(block);output.write(block)
        if received!=manifest['archive_bytes'] or digest.hexdigest()!=manifest['archive_sha256']:
            raise ValueError('Incomplete/corrupt update; database unchanged')
        return apply_bundle(database,name,manifest['archive_sha256'],lock=lock,external_dir=external_dir)
    finally:Path(name).unlink(missing_ok=True)

def active_run(token):
    for status in ('in_progress','queued','waiting','pending','requested'):
        result=api(f'/actions/workflows/{WORKFLOW}/runs?status={status}&per_page=5',token)
        if result['workflow_runs']:return result['workflow_runs'][0]
    return None

def start_refresh(token):
    running=active_run(token)
    if running:return {'already_running':True,'html_url':running['html_url']}
    response=api(f'/actions/workflows/{WORKFLOW}/dispatches',token,method='POST',payload={'ref':'main'})
    return {'already_running':False,'html_url':(response or {}).get('html_url',f'https://github.com/{REPOSITORY}/actions/workflows/{WORKFLOW}')}

def run_status(token):
    result=api(f'/actions/workflows/{WORKFLOW}/runs?per_page=1',token)
    return result['workflow_runs'][0] if result['workflow_runs'] else None
