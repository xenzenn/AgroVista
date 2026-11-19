"""Download the initial published database once per server filesystem."""
from pathlib import Path
import hashlib
import os
import tempfile
import threading
import urllib.request

DATABASE_URL='https://github.com/xenzenn/AgroVista/releases/download/data-initial/agrovista.duckdb'
DATABASE_SHA256='e6d471f44b39b179a4cde5683f9929ca56c98f30ea463ad7afff46fdb8f5376e'
_LOCK=threading.Lock()

def ensure_database(destination, progress=None, *, url=DATABASE_URL, expected_sha256=DATABASE_SHA256):
    destination=Path(destination)
    with _LOCK:
        if destination.is_file(): return destination
        destination.parent.mkdir(parents=True,exist_ok=True)
        fd,temporary=tempfile.mkstemp(prefix='agrovista_',suffix='.part',dir=destination.parent)
        try:
            digest=hashlib.sha256(); received=0
            request=urllib.request.Request(url,headers={'User-Agent':'AgroVista-dashboard'})
            with os.fdopen(fd,'wb') as output:
                with urllib.request.urlopen(request,timeout=120) as response:
                    total=int(response.headers.get('Content-Length',0))
                    if total and __import__('shutil').disk_usage(destination.parent).free < total+128*1024**2:
                        raise OSError('Insufficient disk space for the database download.')
                    while True:
                        chunk=response.read(8*1024**2)
                        if not chunk: break
                        output.write(chunk);digest.update(chunk);received+=len(chunk)
                        if progress: progress(received,total)
                    if total and received!=total:raise OSError('Incomplete database download. Please retry.')
            if digest.hexdigest()!=expected_sha256:raise OSError('Database checksum mismatch. Please retry; published file may have changed.')
            os.replace(temporary,destination)
            return destination
        finally:
            Path(temporary).unlink(missing_ok=True)
