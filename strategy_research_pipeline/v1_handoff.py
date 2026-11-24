"""Shared checks and pre-replay freeze for research and V1."""

import argparse
from datetime import datetime, timezone
from hashlib import file_digest
import json
from pathlib import Path
import re
import pandas as pd


HANDOFF_METADATA = {
    'protocol.json', 'proxy_approval.json', 'freeze_receipt.json',
    'prepare.jsonl', 'prepare_handoff.jsonl', 'replay.jsonl',
    'research.jsonl', 'research_decision.json', 'submission_draft.json',
    'official_submission.json', 'v1_result.json', 'v1_result_verified.json', 'v1_diagnostic.json',
}
PENDING_DATA = {'rate_limited', 'timeout', 'network_error', 'permission_denied', 'unavailable'}
TEMPORARY_DATA = {'rate_limited', 'timeout', 'network_error'}
RULE_FIELDS = ('universe','signal','rebalance','allocation','execution','data_availability')


class DataPending(ValueError):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def atomic_text(path, content):
    tmp = path.with_name(f'.{path.name}.tmp')
    tmp.write_text(content, encoding='utf-8')
    tmp.replace(path)


def save_json(path, value):
    atomic_text(path, json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def digest(path):
    with path.open('rb') as stream:
        return file_digest(stream, 'sha256').hexdigest()


def inside(directory, relative):
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory.resolve()) or not path.is_file():
        raise ValueError(f'证据文件缺失或超出研究目录：{Path(relative).name}')
    return path


def history_problem(first, last):
    years = (last-first).days/365.25
    if first>pd.Timestamp('2020-01-02') or last<first+pd.DateOffset(years=6):
        return (f'正式主账本需从 2020 年首个交易日或更早开始且覆盖至少六年，'
                f'实际 {first.date()} 起，{years:.2f} 年')
    return None


def planned_history_problem(protocol):
    sections = ((protocol.get('v1_preflight'),'planned_start','planned_end'),
                (protocol.get('sample'),'planned_start','planned_end'),
                (protocol.get('sample'),'start','end'),(protocol.get('period'),'start','end'))
    for section, start_key, end_key in sections:
        if not isinstance(section,dict):
            continue
        values = (section.get(start_key),section.get(end_key))
        if not all(isinstance(day,str) and len(day)==10 for day in values):
            continue
        first,last = (pd.to_datetime(day,format='%Y-%m-%d',errors='coerce') for day in values)
        if pd.notna(first) and pd.notna(last):
            return history_problem(first,last)
    return None


def validate_short_history(job):
    coverage = read_json(inside(job,'history_coverage.json'))
    if not isinstance(coverage,dict):
        raise ValueError('历史覆盖记录须为 JSON 对象')
    evidence = coverage.get('files')
    if (not isinstance(evidence,list) or not evidence
            or any(not isinstance(name,str) or not name.strip() for name in evidence)
            or not isinstance(coverage.get('missing_input'),str) or not coverage['missing_input'].strip()):
        raise ValueError('历史不足需要实际取数或代理核验文件和缺失字段')
    checks = coverage.get('observations')
    if not isinstance(checks,list) or not checks:
        raise ValueError('取数状态未经核对，不能判定历史不足')
    for item in checks:
        if (not isinstance(item,dict) or not isinstance(item.get('field'),str) or not item['field'].strip()
                or not isinstance(item.get('source'),str) or not item['source'].strip()
                or item.get('file') not in evidence):
            raise ValueError('每条覆盖观察需记录来源、字段及原始证据文件')
        status = item.get('status')
        if status in PENDING_DATA:
            raise DataPending(f'{item["field"]} 暂未取得：{status}',status in TEMPORARY_DATA)
        if status not in ('confirmed_short','confirmed_empty','proxy_rejected'):
            raise ValueError('覆盖结论需有实际成功取数或已完成的代理对照')
    for name in evidence:
        path = inside(job,name)
        if path.name in ('history_coverage.json','evidence.md','replay.md') or not path.stat().st_size:
            raise ValueError('历史不足需要实际取数或代理核验原始证据')
        recorded = evidence_status(path)
        if recorded in PENDING_DATA:
            raise DataPending(f'{path.name} 记录的是取数异常，暂不能判定历史不足',recorded in TEMPORARY_DATA)
    first,last = (coverage.get(name) for name in ('start','end'))
    if first is None and last is None and 'start' in coverage and 'end' in coverage:
        return
    if any(not isinstance(day,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day) for day in (first,last)):
        raise ValueError('历史覆盖首末日须为 ISO 日期，完全不可用时同时填 null')
    start,end = pd.Timestamp(first),pd.Timestamp(last)
    if start>end or not history_problem(start,end):
        raise ValueError('报告的有效历史区间与“历史不足”矛盾')


def declared_proxy_use(protocol):
    sections = (protocol,protocol.get('v1_preflight'),protocol.get('data_and_evidence'))
    flags = [section['proxy_used'] for section in sections if isinstance(section,dict) and 'proxy_used' in section]
    if any(type(flag) is not bool for flag in flags) or len(set(flags))>1:
        raise ValueError('冻结协议中的代理使用声明无效或互相矛盾')
    return flags[0] if flags else None


def proxy_approval(protocol):
    nested = protocol.get('data_and_evidence')
    top = protocol.get('approved_proxy_evidence')
    inner = nested.get('approved_proxy_evidence') if isinstance(nested,dict) else None
    if top is not None and inner is not None and top!=inner:
        raise ValueError('冻结协议中的代理验收记录互相矛盾')
    return top if top is not None else inner


def proxy_receipts(approval, resolve_file, fingerprint):
    """Check the handoff format and seal the evidence files, without scoring proxies."""
    if (not isinstance(approval,dict) or approval.get('status')!='approved' or
            not isinstance(approval.get('basis'),str) or not approval['basis'].strip() or
            not isinstance(approval.get('files'),list) or not approval['files']):
        raise ValueError('缺少明确的代理验收结论、核验依据或证据文件')
    receipts = {}
    for name in approval['files']:
        if not isinstance(name,str) or not name.strip():
            raise ValueError('代理证据路径无效')
        path = resolve_file(name)
        if path.name.lower() in HANDOFF_METADATA or path.name.lower().endswith('.answer.json'):
            raise ValueError(f'代理审批元数据不能作为原始证据：{path.name}')
        if not path.stat().st_size:
            raise ValueError(f'代理核验证据文件为空：{path.name}')
        if evidence_status(path) in PENDING_DATA | {'rejected','unverified','fail','failed'}:
            raise ValueError(f'代理批准与原始核验状态冲突：{path.name}')
        receipts[name] = fingerprint(path)
    return receipts


def evidence_status(path):
    if path.suffix.lower()=='.json':
        raw = read_json(path)
        if isinstance(raw,dict) and isinstance(raw.get('status'),str):
            return raw['status'].strip().lower()
    return None


def research_protocol(job, source_id):
    protocol = read_json(inside(job, 'protocol.json'))
    if not isinstance(protocol, dict):
        raise ValueError('冻结协议须是单个 JSON 对象')
    version = protocol.get('strategy_version')
    if (protocol.get('strategy_id') != source_id
            or protocol.get('source_kind') not in ('original', 'reconstruction')
            or not isinstance(version, str) or not re.fullmatch(r'[\w.-]{1,80}', version)
            or protocol.get('screening_scope') not in ('official', 'diagnostic')):
        raise ValueError('冻结协议需要正确的原文 ID、策略版本、版本类型和送检范围')
    plan = protocol.get('v1_preflight', {})
    if not isinstance(plan, dict):
        raise ValueError('冻结协议的 v1_preflight 须是一个 JSON 对象')
    for field in ('planned_start', 'planned_end'):
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(plan.get(field, ''))):
            raise ValueError(f'冻结协议缺少 ISO 日期 {field}')
    issue = history_problem(pd.Timestamp(plan['planned_start']), pd.Timestamp(plan['planned_end']))
    if issue:
        raise ValueError(f'历史预检未达标，停止详细回测：{issue}')
    declared_proxy_use(protocol)
    if not isinstance(plan.get('proxy_used'), bool):
        raise ValueError('冻结协议需要明确是否使用资产或信号代理')
    ledger_name = plan.get('ledger_path', '')
    if not isinstance(ledger_name, str) or not re.fullmatch(r'[\w.-]+\.(?:csv|parquet|jsonl)', ledger_name):
        raise ValueError('请在冻结前指定工作目录内的唯一主账本文件名')
    for field in ('date_column', 'equity_column'):
        if not isinstance(plan.get(field), str) or not plan[field].strip():
            raise ValueError(f'冻结协议缺少账本列名 {field}')
    capital = plan.get('initial_capital')
    if type(capital) not in (int, float) or not 0 < capital < float('inf') or not plan.get('cost_profile'):
        raise ValueError('冻结协议缺少有效本金或正常成本口径')
    return protocol


def expected_sessions(job, plan):
    name = plan.get('calendar_path')
    if not name:
        return None
    raw = read_json(inside(job,name))
    if (not isinstance(raw,list) or not raw
            or any(not isinstance(day,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day) for day in raw)):
        raise ValueError('交易日历必须是交易所开市日的 ISO 日期数组')
    days = pd.DatetimeIndex(pd.to_datetime(raw,format='%Y-%m-%d'))
    if (not days.is_unique or not days.is_monotonic_increasing
            or days[0]!=pd.Timestamp(plan['planned_start']) or days[-1]!=pd.Timestamp(plan['planned_end'])):
        raise ValueError('交易日历有重复、乱序或首末日与冻结区间不一致')
    return days


def check_research_freeze(job, source_id):
    protocol = research_protocol(job, source_id)
    receipt = read_json(inside(job, 'freeze_receipt.json'))
    if (not isinstance(receipt, dict) or receipt.get('format') != 'single-session-v1'
            or receipt.get('sha256') != digest(job/'protocol.json')):
        raise ValueError('缺少同会话的事前冻结回执，或冻结协议已变动')
    if receipt.get('source_sha256') != digest(inside(job, 'source.pdf')):
        raise ValueError('研究期间原文指纹发生变化')
    if 'source_text_sha256' in receipt and receipt['source_text_sha256'] != digest(inside(job,'source.txt')):
        raise ValueError('研究期间原文提取文本发生变化')
    plan = protocol['v1_preflight']
    if expected_sessions(job,plan) is not None:
        if receipt.get('calendar_sha256')!=digest(inside(job,plan['calendar_path'])):
            raise ValueError('冻结后交易日历发生变化')
    if declared_proxy_use(protocol):
        current = proxy_receipts(proxy_approval(protocol), lambda name: inside(job, name), digest)
        if current != receipt.get('proxy_files'):
            raise ValueError('冻结后代理证据指纹发生变化')
    return protocol


def seal_research(job, source_id, require_calendar=False):
    receipt_path = job/'freeze_receipt.json'
    if receipt_path.exists():
        protocol = check_research_freeze(job, source_id)
        if require_calendar and expected_sessions(job,protocol['v1_preflight']) is None:
            raise ValueError('已有冻结缺交易日历，请保留旧产物并重新登记研究')
        return read_json(receipt_path)
    protocol = research_protocol(job, source_id)
    plan = protocol['v1_preflight']
    calendar = expected_sessions(job,plan)
    if require_calendar and calendar is None:
        raise ValueError('冻结前须提供独立取数的开市日历 calendar_path')
    outputs = (plan['ledger_path'], 'submission_draft.json', 'official_submission.json')
    if any((job/name).exists() or (job/name).is_symlink() for name in outputs):
        raise ValueError('主账本或送检产物已存在，须在回测前冻结')
    receipt = dict(format='single-session-v1', sha256=digest(job/'protocol.json'),
                   source_sha256=digest(inside(job, 'source.pdf')),
                   at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
    if (job/'source.txt').is_file():
        receipt['source_text_sha256'] = digest(inside(job,'source.txt'))
    if calendar is not None:
        receipt['calendar_sha256'] = digest(inside(job,plan['calendar_path']))
    if declared_proxy_use(protocol):
        receipt['proxy_files'] = proxy_receipts(proxy_approval(protocol), lambda name: inside(job, name), digest)
    save_json(receipt_path, receipt)
    return receipt


def replay_fingerprints(job, protocol):
    manifest_path = inside(job,'replay_inputs.json')
    manifest = read_json(manifest_path)
    if not isinstance(manifest,dict) or not isinstance(manifest.get('files'),list) or not manifest['files']:
        raise ValueError('缺少回放代码和输入快照清单 replay_inputs.json')
    entry = manifest.get('entrypoint')
    if not isinstance(entry,str) or not entry.endswith('.py') or entry not in manifest['files']:
        raise ValueError('回放入口须是清单内的 Python 文件')
    files = {}
    for name in manifest['files']:
        if not isinstance(name,str) or not name.strip():
            raise ValueError('回放输入清单路径无效')
        path = inside(job,name)
        if (path in (job/protocol['v1_preflight']['ledger_path'],job/'replay_seal.json')
                or not path.stat().st_size):
            raise ValueError('回放输入只能包含已落盘的代码和原始输入')
        files[path.relative_to(job.resolve()).as_posix()] = digest(path)
    if not any(Path(name).suffix.lower() in ('.csv','.parquet','.json','.jsonl','.npy','.npz') for name in files):
        raise ValueError('回放清单缺可复算的数据快照')
    return dict(protocol_sha256=digest(job/'protocol.json'), manifest_sha256=digest(manifest_path), files=files)


def check_replay_seal(job, protocol):
    inputs = replay_fingerprints(job,protocol)
    if read_json(inside(job,'replay_seal.json')) != inputs:
        raise ValueError('回测代码或输入快照在锁定后发生变化')
    return inputs


def check_rule_contract(protocol):
    contract = protocol.get('rule_contract')
    if not isinstance(contract,dict):
        raise ValueError('正式送检需要回测前冻结的结构化策略规则')
    for key in RULE_FIELDS:
        item = contract.get(key)
        if (not isinstance(item,dict) or item.get('kind') not in ('paper','assumption','mixed')
                or any(not isinstance(item.get(field),str) or not item[field].strip()
                       for field in ('rule','basis'))):
            raise ValueError(f'冻结规则缺少 {key} 的可执行定义、来源类别或依据')


def validate_rule_gap(job):
    gap = read_json(inside(job,'rule_gap.json'))
    if (not isinstance(gap,dict) or gap.get('field') not in RULE_FIELDS
            or not isinstance(gap.get('missing'),str) or not gap['missing'].strip()):
        raise ValueError('规则缺口须明确到不可执行的定义')
    pages = gap.get('source_pages')
    if not isinstance(pages,list) or not pages or any(type(p) is not int or p<1 for p in pages):
        raise ValueError('规则缺口缺少可定位原文页码')
    coverage = job/'source_extract.json'
    if coverage.is_file() and max(pages)>len(read_json(coverage)['pages']):
        raise ValueError('规则缺口页码超出原文范围')
    alternatives = gap.get('alternatives')
    if not isinstance(alternatives,list) or not alternatives or any(
            not isinstance(item,dict) or any(not isinstance(item.get(key),str) or not item[key].strip()
                for key in ('proposal','basis','mechanism_conflict')) for item in alternatives):
        raise ValueError('规则缺口须写明试过的补法、依据及其为何会改变原策略机制')


def seal_replay(job, source_id):
    protocol = check_research_freeze(job,source_id)
    path = job/'replay_seal.json'
    if path.exists():
        return check_replay_seal(job,protocol)
    if (job/protocol['v1_preflight']['ledger_path']).exists():
        raise ValueError('主账本已存在，请在运行主账本前锁定实现和输入')
    values = replay_fingerprints(job,protocol)
    save_json(path,values)
    return values


def corrected_replay(job, protocol, relative_ledger, ledger_hash):
    ledger = inside(job, relative_ledger)
    book = ledger.parent
    if book == job.resolve() or not book.is_relative_to(job.resolve()):
        raise ValueError('修正版必须放在原研究目录下的独立子目录')
    revised = check_research_freeze(book,protocol['strategy_id'])
    if revised != protocol or ledger.name != revised['v1_preflight']['ledger_path']:
        raise ValueError('修正回放与原冻结的规则、成本、区间或账本定义不一致')
    if digest(inside(book,'source.pdf')) != digest(inside(job,'source.pdf')):
        raise ValueError('修正回放的原文与主任务不一致')
    check_replay_seal(book,revised)
    if digest(ledger) != ledger_hash:
        raise ValueError('修正版账本与交接指纹不一致')
    return book


def select_scoring_book(job, protocol):
    selected = job/'v1_ledger_revision.json'
    if selected.is_file():
        data = read_json(selected)
        return corrected_replay(job,protocol,data['ledger_path'],data['ledger_sha256']), selected
    receipts = [p for p in (job/'compatible_replay_receipt.json',job/'compatible_replay_pointer.json') if p.is_file()]
    if len(receipts)>1:
        raise ValueError('发现多份旧修正回执，请明确登记唯一评分账本')
    if not receipts:
        return job,None
    receipt = receipts[0]
    data = read_json(receipt)
    if 'compatible_directory' in data:
        name = protocol['v1_preflight']['ledger_path']
        ledger = (Path(data['compatible_directory'])/name).as_posix()
        fingerprint = data['root_files'][name]
        if digest(inside(job,name)) != fingerprint:
            raise ValueError('旧兼容核账回执与原账本指纹不一致')
    elif 'compatible_verified_ledger' in data:
        ledger,fingerprint = data['compatible_verified_ledger'],data['compatible_ledger_sha256']
    else:
        ledger,fingerprint = data['ledger_path'],data['ledger_sha256']
    return corrected_replay(job,protocol,ledger,fingerprint),receipt


def register_correction(job, source_id, relative_ledger):
    protocol = check_research_freeze(job,source_id)
    ledger = inside(job,relative_ledger)
    receipt = dict(ledger_path=ledger.relative_to(job.resolve()).as_posix(),ledger_sha256=digest(ledger))
    corrected_replay(job,protocol,receipt['ledger_path'],receipt['ledger_sha256'])
    path = job/'v1_ledger_revision.json'
    if (job/'v1_result.json').exists() or (job/'official_submission.json').exists():
        if not path.is_file() or read_json(path) != receipt:
            raise ValueError('已经生成评分交接，须另开复验保留本轮记录')
    else:
        save_json(path,receipt)
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='在同一研究会话中核验并冻结回测协议')
    parser.add_argument('--source-id', required=True)
    parser.add_argument('--require-calendar', action='store_true')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--seal-replay', action='store_true')
    modes.add_argument('--select-correction', metavar='LEDGER')
    args = parser.parse_args()
    try:
        if args.select_correction:
            result = register_correction(Path.cwd(),args.source_id,args.select_correction)
        elif args.seal_replay:
            result = seal_replay(Path.cwd(),args.source_id)
        else:
            result = seal_research(Path.cwd(),args.source_id,args.require_calendar)
        print(json.dumps(result))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        parser.exit(1, f'冻结未完成：{exc}\n')
