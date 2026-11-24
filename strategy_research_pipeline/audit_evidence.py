"""Render narrow audit evidence for large tabular artifacts without changing their raw bytes."""

import json

import pandas as pd


TABLE_INLINE_CHARS = 24_000
TIMING_PAIRS = (
    ('known', 'date'),
    ('signal_date', 'trade_date'),
    ('signal_at', 'simulated_open_at'),
    ('data_cutoff_at', 'signal_at'),
    ('last_return', 'known'),
    ('daily_signal_cutoff', 'audit_session'),
    ('risk_return_last', 'cutoff_session'),
)


def timing_evidence(frame):
    results = []
    exceptions = set()
    for first, second in TIMING_PAIRS:
        if first not in frame or second not in frame:
            continue
        a,b = (frame[field].astype(str).str.strip() for field in (first,second))
        day = r'(?:\d{4}-\d{2}-\d{2}|\d{8})'
        zoned = r'(?:Z|[+-]\d{2}:?\d{2})$'
        same_precision = ((a.str.fullmatch(day) & b.str.fullmatch(day))
                          | (a.str.contains(zoned) & b.str.contains(zoned)))
        before = pd.to_datetime(a.where(same_precision),format='mixed',errors='coerce',utc=True)
        after = pd.to_datetime(b.where(same_precision),format='mixed',errors='coerce',utc=True)
        comparable = before.notna() & after.notna()
        later = comparable & (before > after)
        results.append(dict(first=first, second=second, earlier=int((comparable & (before < after)).sum()),
                            equal=int((comparable & (before == after)).sum()), later=int(later.sum()),
                            unparseable_or_missing=int((~comparable).sum())))
        exceptions.update(position for position in (later | ~comparable).to_numpy().nonzero()[0][:3])
    return results, exceptions


def render_table(frame, body, source_hash):
    scope = dict(fields=list(map(str,frame.columns)), rows=len(frame), sample_rows=None, statistics=[])
    if body is not None and len(body) <= TABLE_INLINE_CHARS:
        return body, 'full_table', scope

    timing, exceptions = timing_evidence(frame)
    numeric = frame.select_dtypes(include='number')
    finite = numeric.replace([float('-inf'), float('inf')], float('nan'))
    summary = dict(rows=len(frame), source_sha256=source_hash,
                   missing=frame.isna().sum().to_dict(),
                   nonfinite=(numeric.notna().sum()-finite.notna().sum()).to_dict(),
                   timing=timing)
    for col in ('audit_session', 'date', 'trade_date'):
        if col in frame:
            dates = pd.to_datetime(frame[col].astype(str), format='mixed', errors='coerce', utc=True)
            first, last = dates.min(), dates.max()
            summary['dates'] = dict(column=col, first=str(first), last=str(last),
                                    missing_or_invalid=int(dates.isna().sum()),
                                    repeated=int(dates.dropna().duplicated().sum()),
                                    nondecreasing=bool(dates.dropna().is_monotonic_increasing))
            break

    count = len(frame)
    positions = {i for point in (0, count//4, count//2, 3*count//4, count-1)
                 for i in (point, point+1) if 0 <= i < count}
    for col in ('nav', 'equity', 'cash', 'cost', 'total_cost'):
        if col in finite and finite[col].notna().any():
            values = finite[col].reset_index(drop=True)
            positions.update((values.idxmin(), values.idxmax()))
    positions = sorted(positions | exceptions)
    excerpt = frame.iloc[positions].copy()
    excerpt.index = [position+1 for position in positions]
    aggregates = finite.agg(['count','min','max','sum']).to_json(double_precision=10) if len(finite.columns) else '{}'
    scope['sample_rows'] = [i+1 for i in positions]
    scope['statistics'] = ['统计缺失','数值汇总'] + (['日期覆盖'] if 'dates' in summary else [])
    scope['statistics'] += [f'{item["first"]}/{item["second"]} 时点核验' for item in timing]
    return ('以下统计覆盖原表的全部数据行，行样例按位置、关键数值极值及时点例外选取。'
            '样例不是全表，缺证的规则标为未查。原表保留，可用 SHA-256 比对。\n'
            +json.dumps(summary,ensure_ascii=False)+'\n全表数值统计（各列独立聚合）\n'
            +aggregates+'\n原表样例（数据行号从 1 开始）\n'
            +excerpt.to_csv(index_label='数据行号',float_format='%.10g')), 'full_table_summary', scope
