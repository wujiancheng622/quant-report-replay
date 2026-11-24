"""Mainland exchange calendar prepared by the Windows supervisor."""

from datetime import date, timedelta
from pathlib import Path
from threading import Lock
from importlib.util import module_from_spec, spec_from_file_location

import pandas as pd

from v1_handoff import read_json, save_json


EXCHANGES = ('SSE', 'SZSE')
FIRST_DATE = date(1992, 1, 1)
CALENDAR_LOCK = Lock()
SOURCE = 'Tushare trade_cal SSE+SZSE'
TUSHARE_CLIENT = Path(__file__).resolve().parents[1]/'ah_premium_20261106/data_access.py'


def mainland_sessions(path, first, last):
    payload = read_json(path)
    start = pd.Timestamp(payload['start'])
    end = pd.Timestamp(payload['end'])
    if payload['source'] != SOURCE or first < start or last > end:
        raise ValueError('后台交易日历未覆盖账本区间，需补交易所日历')
    days = pd.DatetimeIndex(pd.to_datetime(payload['sessions'], format='%Y-%m-%d'))
    if not days.is_unique or not days.is_monotonic_increasing or days.hasnans:
        raise ValueError('后台交易日历无效')
    return days[(days >= first) & (days <= last)]


def code_days(frame, exchange, first, last):
    labels = pd.to_datetime(frame['cal_date'].astype(str), format='%Y%m%d')
    flags = pd.to_numeric(frame['is_open'], errors='raise')
    if (not frame['exchange'].eq(exchange).all() or not flags.isin((0,1)).all()
            or sorted(labels) != list(pd.date_range(first,last))):
        raise ValueError(f'{exchange} 日历数据缺日、重复或交易所不一致')
    return set(labels[flags.eq(1)].dt.strftime('%Y-%m-%d'))


def refresh_calendar(path, today):
    """Refresh only as needed, using the existing authorized read-only client."""
    with CALENDAR_LOCK:
        if path.is_file():
            old = read_json(path)
            if old['source'] != SOURCE:
                raise ValueError('后台日历来源不符，请核对取数配置')
            if date.fromisoformat(old['end']) >= today:
                return path
            first = date.fromisoformat(old['end']) - timedelta(days=30)
        else:
            old = {'start':FIRST_DATE.isoformat(), 'sessions':[]}
            first = FIRST_DATE
        spec = spec_from_file_location('pipeline_calendar_data_access', TUSHARE_CLIENT)
        client_module = module_from_spec(spec)
        spec.loader.exec_module(client_module)
        client = client_module.OfficialData()
        sessions = {day for day in old['sessions'] if day < first.isoformat()}
        while first <= today:
            last = min(date(first.year+4,12,31),today)
            for exchange in EXCHANGES:
                params = dict(exchange=exchange,start_date=first.strftime('%Y%m%d'),end_date=last.strftime('%Y%m%d'))
                rows = client.query('trade_cal',params,'exchange,cal_date,is_open')
                sessions.update(code_days(rows,exchange,first,last))
            first = last + timedelta(days=1)
        save_json(path,dict(source=SOURCE,start=old['start'],end=today.isoformat(),sessions=sorted(sessions)))
        return path
