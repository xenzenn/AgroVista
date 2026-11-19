"""Bound and serialize DuckDB work for small cloud instances."""
from pathlib import Path
import os
import threading
import time
import duckdb
QUERY_LOCK=threading.Lock()

def query_dataframe(database,query,params):
    spill=Path(database).resolve().parent/'duckdb_spill'
    spill.mkdir(parents=True,exist_ok=True)
    with QUERY_LOCK:
        started=time.monotonic()
        config={'memory_limit':os.getenv('AGROVISTA_DUCKDB_MEMORY','256MB'),
                'threads':1,'preserve_insertion_order':False,
                'temp_directory':str(spill),'max_temp_directory_size':'4GB'}
        label=__import__('hashlib').sha256(query.encode()).hexdigest()[:10]
        print(f'[AgroVista query {label}] start',flush=True)
        try:
            with duckdb.connect(str(database),read_only=True,config=config) as con:
                result=con.execute(query,list(params)).df()
            print(f'[AgroVista query {label}] done rows={len(result)} seconds={time.monotonic()-started:.1f}',flush=True)
            return result
        except Exception as exc:
            print(f'[AgroVista query {label}] failed type={type(exc).__name__}',flush=True)
            raise
