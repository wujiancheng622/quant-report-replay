"""Read-only official Tushare transport. Never log credentials."""
from __future__ import annotations
from pathlib import Path
import datetime as dt
import hashlib
import http.client
import json
import os
import threading
import time
import sys
import pandas as pd

ROOT = Path(__file__).resolve().parent


class TushareRequestError(RuntimeError):
    def __init__(self, message, *, http_status=None, retry_after=None):
        super().__init__(message)
        self.http_status = http_status
        self.retry_after = retry_after


class OfficialData:
    def __init__(self):
        self._token = os.environ.get('TUSHARE_OFFICIAL_TOKEN') or os.environ.get('TUSHARE_TOKEN')
        if not self._token:
            runtime=ROOT.parent.parent/'etf/etf_data_fetcher'
            sys.path.insert(0,str(runtime))
            from tushare_runtime import load_tushare_runtime_settings
            self._token=load_tushare_runtime_settings(runtime/'config.yaml').token
        self.lock = threading.Lock()
        self.next = 0.

    def query(self, api, params, fields='', *, attempts=3):
        body = json.dumps(dict(api_name=api, token=self._token, params=params, fields=fields)).encode()
        error = None
        http_status = retry_after = None
        for attempt in range(attempts):
            http_status = retry_after = None
            with self.lock:
                time.sleep(max(0, self.next-time.monotonic()))
                self.next = time.monotonic()+.4
            conn = http.client.HTTPSConnection('api.tushare.pro',timeout=25)
            try:
                conn.request('POST','/',body,{'Content-Type':'application/json'})
                response = conn.getresponse()
                raw = response.read()
                if response.status != 200:
                    http_status = response.status
                    retry_after = response.getheader('Retry-After') if http_status == 429 else None
                    raise RuntimeError('HTTP '+str(response.status))
                result = json.loads(raw)
                if result['code'] != 0:
                    raise RuntimeError(str(result.get('msg','')).replace(self._token,'<redacted>'))
                return pd.DataFrame(result['data']['items'],columns=result['data']['fields'])
            except Exception as exc:
                error = type(exc).__name__+': '+str(exc).replace(self._token,'<redacted>')[:180]
                if any(term in str(exc) for term in ['频率超限','没有接口','访问权限']):
                    raise TushareRequestError(api+': '+error, http_status=http_status,
                                              retry_after=retry_after) from None
            finally:
                conn.close()
            if attempt + 1 < attempts:
                time.sleep(1+attempt)
        raise TushareRequestError(api+': '+str(error), http_status=http_status,
                                  retry_after=retry_after) from None

    def fetch(self, api, name, params, fields=''):
        path = ROOT/'data/official'/api/(name+'.parquet')
        if path.exists():
            return pd.read_parquet(path)
        frame = self.query(api,params,fields)
        limit = {'stk_ah_comparison':1000,'daily':6000,'adj_factor':6000,'stk_limit':6000,'dividend':2000,'hk_daily':5000,'index_daily':6000}.get(api)
        if limit and len(frame)>=limit:
            raise RuntimeError('Possible truncated '+api)
        path.parent.mkdir(parents=True,exist_ok=True)
        frame.to_parquet(path,index=False)
        path.with_suffix('.json').write_text(json.dumps(dict(api=api,params=params,fields=fields,rows=len(frame),columns=list(frame),fetched_utc=dt.datetime.now(dt.timezone.utc).isoformat(),sha256=hashlib.sha256(path.read_bytes()).hexdigest()),ensure_ascii=False,indent=2),encoding='utf-8')
        print('FETCH',api,name,len(frame),flush=True)
        return frame
