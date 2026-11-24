"""Record and replay an exact read-only Tushare request."""

import argparse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import importlib.util
import json
import math
from pathlib import Path
import re

from v1_handoff import TEMPORARY_DATA, digest, inside, read_json, save_json


CLIENT = Path(__file__).resolve().parents[1] / 'ah_premium_20261106' / 'data_access.py'
CHINA_TIME = timezone(timedelta(hours=8))


def request_spec(api, params, fields):
    if isinstance(fields,list) and all(isinstance(name,str) for name in fields):
        fields = ','.join(fields)
    if (not isinstance(api,str) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]*',api)
            or not isinstance(params,dict)
            or not isinstance(fields,str)
            or not re.fullmatch(r'(?:[\w]+(?:\s*,\s*[\w]+)*)?',fields,re.ASCII)):
        raise ValueError('重放请求须有接口、明确的参数及请求字段')
    for key,value in params.items():
        if (not isinstance(key,str) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]*',key)
                or re.search(r'token|secret|password|api_key|credential',key,re.I)
                or type(value) not in (str,int,float,bool,type(None))
                or (isinstance(value,float) and not math.isfinite(value))):
            raise ValueError('取数参数须为非凭据的标量')
    return dict(api=api,params=params,fields=fields)


def pending_request(job):
    """Read recorded request fields only. Older non-replayable evidence stays readable."""
    try:
        coverage = read_json(inside(job,'history_coverage.json'))
        observations,evidence = coverage.get('observations'),coverage.get('files')
    except (OSError,ValueError,AttributeError):
        return None
    if not isinstance(observations,list) or not isinstance(evidence,list):
        return None
    for observation in observations:
        if (not isinstance(observation,dict) or observation.get('status') not in TEMPORARY_DATA
                or observation.get('file') not in evidence
                or not re.match(r'^Tushare\b',str(observation.get('source','')),re.I)):
            continue
        try:
            path = inside(job,observation['file'])
            receipt = read_json(path)
            if receipt.get('status') != observation['status']:
                continue
            if receipt.get('provider') == 'Tushare':
                raw = receipt['request']
                request = request_spec(raw['api'],raw['params'],raw['fields'])
            else:
                source = re.fullmatch(r'Tushare(?: Pro)?\s+(\w+)',receipt.get('source',''),re.I)
                if not source:
                    continue
                fields = receipt.get('fields',receipt.get('requested_columns'))
                request = request_spec(source[1],receipt['params'],fields)
        except (OSError,ValueError,TypeError,KeyError,AttributeError):
            continue
        return request,path.relative_to(job.resolve()).as_posix()
    return None


def failure_status(error):
    detail = str(error)
    status_code = getattr(error,'http_status',None)
    if status_code == 429 or re.search(r'HTTP 429|限流|频率|频次|限频|每天最多|每小时最多|rate limit',detail,re.I):
        return 'rate_limited'
    if status_code in (401,403) or re.search(r'HTTP 40[13]|权限|没有接口|token.*(?:不正确|无效)',detail,re.I):
        return 'permission_denied'
    if re.search(r'timeout|timed out|超时',detail,re.I):
        return 'timeout'
    if (isinstance(error,OSError) or status_code is not None and status_code>=500
            or re.search(r'Connection|gaierror|RemoteDisconnected|URLError|SSLError',detail,re.I)):
        return 'network_error'
    return 'unavailable'


def retry_time(receipt, now, delay):
    if receipt['status'] == 'permission_denied':
        return None
    if receipt['status'] == 'rate_limited':
        header = receipt.get('retry_after')
        if isinstance(header,str):
            try:
                timestamp = now + int(header) if header.isdecimal() else parsedate_to_datetime(header).timestamp()
                return max(timestamp,now+1)
            except (ValueError,OverflowError,TypeError):
                pass
        if re.search(r'每日|每天|每自然日|\bdaily\b',receipt.get('detail',''),re.I):
            tomorrow = datetime.fromtimestamp(now,CHINA_TIME).date() + timedelta(days=1)
            return datetime.combine(tomorrow,datetime.min.time(),CHINA_TIME).timestamp() + 60
    return now + delay


def fetch_request(request):
    spec = importlib.util.spec_from_file_location('pipeline_tushare_probe',CLIENT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.OfficialData().query(request['api'],request['params'],request['fields'],attempts=1)


def run_probe(job, destination, request):
    receipt_path = (job/destination).resolve()
    if not receipt_path.is_relative_to(job.resolve()):
        raise ValueError('取数回执须保存在当前研究目录')
    receipt_path.parent.mkdir(parents=True,exist_ok=True)
    data_path = receipt_path.with_suffix('.parquet')
    if receipt_path.exists() or data_path.exists():
        raise ValueError('此取数回执位置已有记录，请选择新路径')
    result = dict(provider='Tushare',request=request,receipt=receipt_path.relative_to(job.resolve()).as_posix())
    try:
        frame = fetch_request(request)
    except (OSError,RuntimeError) as exc:
        result.update(status=failure_status(exc), detail=str(exc)[:240],
                      http_status=getattr(exc,'http_status',None),retry_after=getattr(exc,'retry_after',None))
    else:
        data_file = data_path.with_suffix('.parquet.part')
        frame.to_parquet(data_file,index=False)
        data_file.replace(data_path)
        result.update(status='ok' if len(frame) else 'empty', rows=len(frame),
                      columns=list(frame), data=data_path.relative_to(job.resolve()).as_posix(),
                      sha256=digest(data_path))
    result['at'] = datetime.now(timezone.utc).isoformat()
    save_json(receipt_path,result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api',required=True)
    parser.add_argument('--params',required=True)
    parser.add_argument('--fields',required=True,help='未指定字段时显式传空字符串')
    parser.add_argument('--receipt',type=Path,required=True)
    args = parser.parse_args()
    try:
        request = request_spec(args.api,json.loads(args.params),args.fields)
        result = run_probe(Path.cwd(),args.receipt,request)
    except (OSError,ValueError,TypeError) as exc:
        parser.exit(1,f'{exc}\n')
    print(json.dumps({key:value for key,value in result.items() if key not in ('provider','request')},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
