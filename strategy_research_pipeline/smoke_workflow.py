"""Isolated synthetic probes. A mock SMTP relay prevents external mail."""

from datetime import date, timedelta
from contextlib import closing
from pathlib import Path
import json
import os
import tempfile
from types import SimpleNamespace
from threading import Barrier, Lock
from unittest.mock import patch

import pandas as pd

import runner
from market_calendar import SOURCE
from v1_handoff import replay_fingerprints, seal_replay, seal_research


def write_account_fixture(job, days, balances, opening_fee, impossible_turnover=False):
    prices = [nav/1000 for nav in balances]
    stamp = [day.isoformat() for day in days]
    quotes = pd.DataFrame(dict(date=stamp,symbol='fixture.SH',open=prices,high=prices,low=prices,close=prices,
                               volume=1000000,upper=[p*1.1 for p in prices],lower=[p*.9 for p in prices],
                               suspended=0,buy_min=100,buy_step=100,t_plus=1))
    if impossible_turnover:
        quotes.loc[0,'high'] += 10
        quotes['amount'] = quotes['high']*quotes['volume']
    quotes.to_csv(job/'prices.csv',index=False)
    pd.DataFrame([dict(symbol='*',side='buy',start=stamp[0],end=stamp[-1],commission_rate=0,
                       min_commission=opening_fee,tax_rate=0,other_rate=0,slippage_rate=0)]
                 ).to_csv(job/'fee_schedule.csv',index=False)
    runner.atomic_text(job/'account.py','# isolated account fixture\n')
    runner.atomic_text(job/'cash_source.txt','synthetic distribution feed, no events')
    runner.save_json(job/'cash_actions.json',dict(coverage=[dict(symbol='fixture.SH',start=stamp[0],
                    end=stamp[-1],source='cash_source.txt')],actions=[]))
    runner.save_json(job/'replay_inputs.json',dict(entrypoint='account.py',
                                                  files=['account.py','prices.csv','fee_schedule.csv','cash_source.txt','cash_actions.json']))
    seal_replay(job,'fixture-ready')
    pd.DataFrame([dict(date=stamp[0],symbol='fixture.SH',signal_date=(days[0]-timedelta(days=1)).isoformat(),
                       seq=1,quantity=1000,price=prices[0],commission=opening_fee,tax=0,other_fee=0,slippage=0)]
                 ).to_csv(job/'fills.csv',index=False)
    pd.DataFrame(dict(date=stamp,symbol='fixture.SH',quantity=1000,mark=prices)).to_csv(job/'positions.csv',index=False)
    runner.atomic_text(job/'events.csv','date,symbol,seq,kind,source,cash_delta,receivable_delta,quantity\n')
    runner.save_json(job/'account_evidence.json',dict(quotes='prices.csv',fills='fills.csv',positions='positions.csv',
                                                       events='events.csv',fee_schedule='fee_schedule.csv',cash_actions='cash_actions.json'))


def smoke(run_live_model):
    original_start_research = runner.start_research
    original_audit_run = runner.model_run
    with tempfile.TemporaryDirectory(prefix='codex-pipeline-fixture-') as folder:
        base=Path(folder)
        inbox, archive, jobs, summary = [base/x for x in ('inbox','archive','jobs','summary')]
        for path in (inbox,archive,jobs,summary):
            path.mkdir()
        settings=dict(INBOX=inbox,ARCHIVE=archive,JOBS=jobs,SUMMARY=summary,RUNTIME=base,
                      STATE=base/'queue.sqlite3',PIPELINE_MAIL=base/'mail.json',DATA_RETRY_DELAY=0)
        sends=[]
        journal=[]
        local_calendar = base/'market_calendar.json'
        runner.save_json(local_calendar,dict(source=SOURCE,start='2016-10-31',end='2026-11-23',
                                             sessions=pd.bdate_range('2016-10-31','2026-11-23').strftime('%Y-%m-%d').tolist()))
        fixture_refs = [dict(file='account.py',location='line 1')]
        artifacts = {'account.py':dict(lines=1)}
        audit_answer={
            'checks':[dict(id=rule,status='pass',severity='none',evidence='synthetic fixture',
                           references=fixture_refs,actual_violation='no')
                      for rule in sorted(runner.RULE_IDS)],
            'validation_timeline':dict(evaluation_use='no_selection',target='none',references=fixture_refs),
            'numbers':{key:dict(value=1,basis='synthetic fixture') for key in runner.AUDIT_NUMBERS},
            'key_evidence_complete':True,
            'summary':'软件测试夹具；此结论不适用于真实策略。',
        }

        class DummySMTP:
            def __init__(self,host,port,**_kwargs):
                assert (host,port)==('smtp.163.com',465)
            def __enter__(self):
                return self
            def __exit__(self,*_args):
                return False
            def send_message(self,msg):
                sends.append(msg['To'])
                return {}

        def model(job, phase, instruction, name, audit=False):
            title = (job/'source.txt').read_text(encoding='utf-8')
            journal.append(dict(phase=phase,model=name,audit=audit,title=title))
            if audit:
                assert '原文逐页文本 （文件 source.txt）' in instruction
                assert 'V1_ONLY_PRIVATE_SENTINEL' not in instruction
                if title=='audit-timeout':
                    raise runner.ModelError('独立审计超时，未收到结论')
                if title=='audit-invalid':
                    answer = {**audit_answer,'checks':[]}
                elif title=='audit-hold':
                    checks = [{**check,'status':'unverified','severity':'none','evidence':'历史标的池缺证'}
                              if check['id']=='QG-06' else check for check in audit_answer['checks']]
                    answer = {**audit_answer,'checks':checks,'key_evidence_complete':False}
                else:
                    answer = audit_answer
                runner.save_json(job/'audit_decision.json',answer)
                return answer
            if phase=='evidence_review':
                assert (job/'v1_diagnostic.json').is_file()
                runner.atomic_text(job/'evidence_review.md','测试补证结果。')
                if title in ('promoted','late-evidence-error'):
                    (job/'evidence_draft.json').replace(job/'submission_draft.json')
                    if title=='late-evidence-error':
                        return dict(status='invalid_replay',reason='样例确认信号使用了次日输入')
                    return dict(status='evidence_missing',reason='补证回执的文字与已完成产物不一致')
                return dict(status='evidence_missing',reason='成交证据尚不足')
            assert phase in ('research','coverage_review','rule_gap_review','data_retry') and '--source-id fixture-' in instruction
            runner.atomic_text(job/'evidence.md','测试证据。')
            if title=='missing':
                runner.save_json(job/'rule_gap.json',dict(field='signal',missing='训练目标缺失',source_pages=[1],
                    alternatives=[dict(proposal='以价格方向作目标',basis='原文第1页',mechanism_conflict='原文只含排序偏好')]))
                return dict(status='rules_missing',reason='测试缺证')
            if title=='unverified-rules' or title=='premature-rules' and phase=='research':
                return dict(status='rules_missing',reason='回执只有结论，具体缺口尚未核对')
            if title=='pending-evidence':
                return dict(status='evidence_missing',reason='行情覆盖够长，关键公告未核实')
            if title=='throttled':
                runner.save_json(job/'data_probe.json',{'status':'rate_limited'})
                runner.save_json(job/'history_coverage.json',
                                 dict(start=None,end=None,missing_input='信号历史',files=['data_probe.json'],
                                      observations=[dict(field='信号',source='fixture',file='data_probe.json',
                                                         status='rate_limited')]))
                return dict(status='history_short',reason='限流不是历史不足')
            if title=='ambiguous-short' and phase=='research':
                runner.save_json(job/'data_probe.json',{'checked':True})
                runner.save_json(job/'history_coverage.json',
                                 dict(start='2019-01-01',end='2026-11-20',missing_input='暂无',
                                      files=['data_probe.json'],observations=[dict(field='信号',source='fixture',
                                          file='data_probe.json',status='confirmed_short')]))
                return dict(status='history_short',reason='误标有效历史不足')
            start = date(2016,10,31) if title=='long-diagnostic' else date(2019,1,1)
            days = [d.date() for d in pd.bdate_range(start, '2026-11-20')]
            if title=='sparse-calendar':
                days = [days[0],days[-1]]
            runner.save_json(job/'calendar.json',[d.isoformat() for d in days])
            plan = dict(planned_start=start.isoformat(),planned_end=days[-1].isoformat(),
                        proxy_used=title in ('ready','rejected-proxy'),
                        ledger_path='ledger.csv',date_column='date',equity_column='equity',calendar_path='calendar.json',
                        initial_capital=1e6,cost_profile='fixture')
            protocol = dict(strategy_id='fixture-ready',strategy_version=f'fixture-{title}',
                            source_kind='reconstruction' if title in ('long-diagnostic','v1-fail','promoted') else 'original',
                            source_pages=[1],
                            screening_scope='diagnostic' if title in
                            ('long-diagnostic','rejected-proxy','promoted','weak-complete','ambiguous-short') else 'official',
                            v1_preflight=plan)
            protocol['rule_contract'] = {key:dict(kind='assumption',rule=f'synthetic {key}',basis='fixture')
                                         for key in ('universe','signal','rebalance','allocation','execution',
                                                     'data_availability')}
            if title=='ready':
                runner.save_json(job/'check_proxy.json',{'synthetic':True})
                protocol['approved_proxy_evidence'] = {
                    'status':'approved','basis':'测试夹具','files':['check_proxy.json'],
                }
            if title=='history-short':
                plan['planned_start'] = '2023-09-01'
            runner.save_json(job/'protocol.json',protocol)
            if title in ('history-short','rejected-proxy'):
                expected = '停止详细回测' if title=='history-short' else '缺少明确的代理验收'
                try:
                    seal_research(job,'fixture-ready')
                except ValueError as exc:
                    assert expected in str(exc)
                    runner.save_json(job/'data_probe.json',{'checked':True})
                    runner.save_json(job/'history_coverage.json',
                                     dict(start='2023-09-01',end='2026-11-20',missing_input='信号历史',
                                          files=['data_probe.json'],observations=[dict(field='信号',source='fixture',
                                              file='data_probe.json',status='confirmed_short')]))
                    return dict(status='history_short',reason=str(exc))
                raise AssertionError('有效历史不足时事前冻结应失败')
            seal_research(job,'fixture-ready',require_calendar=True)
            assert seal_research(job,'fixture-ready')==runner.read_json(job/'freeze_receipt.json')
            if title=='late-start':
                days = [d for d in days if d >= date(2020,2,1)]
            if title=='missing-middle':
                days.pop(5)
            rate = 0.999 if title in ('v1-fail','weak-complete','ambiguous-short','premature-rules') else 1.00045
            opening_fee = 100 if title in ('weak-complete','v1-fail') else 0
            values = [(1e6-opening_fee)*rate**i for i in range(len(days))]
            write_account_fixture(job,days,values,opening_fee,title=='impossible-turnover')
            pd.DataFrame({'date':[int(d.strftime('%Y%m%d')) for d in days],
                          'cash':0.0,'receivable':0.0,'equity':values}).to_csv(job/'ledger.csv',index=False)
            runner.atomic_text(job/'replay.md','V1_ONLY_PRIVATE_SENTINEL')
            runner.save_json(job/'audit_inputs.json',['account.py','evidence.md','replay.md'])
            if title=='wrong-cost':
                fills = pd.read_csv(job/'fills.csv')
                fills.loc[0,'commission'] = 50
                fills.to_csv(job/'fills.csv',index=False)
            if title=='changed-implementation':
                runner.atomic_text(job/'account.py','# changed after input seal\n')
            if title=='long-diagnostic':
                return dict(status='diagnostic',reason='十年历史可回放，仍有成交证据待核')
            if title in ('weak-complete','ambiguous-short'):
                if title=='weak-complete':
                    (job/'account_evidence.json').unlink()
                return dict(status='evidence_missing',reason='成分时间尚待核实，但长账本已完成')
            if title=='invalid-full':
                return dict(status='invalid_replay',reason='样例复盘确认使用未来数据')
            if title=='changed-after-freeze':
                protocol['strategy_version'] = 'unfrozen-change'
                runner.save_json(job/'protocol.json',protocol)
            manifest = {k:plan[k] for k in ('initial_capital','cost_profile','ledger_path',
                        'date_column','equity_column','proxy_used')}
            runner.save_json(job/'submission_evidence.json',
                             {kind:['evidence.md'] for kind in
                              ('signal_timing','execution_and_cost','ledger_reconciliation')})
            draft = job/('evidence_draft.json' if title in ('promoted','late-evidence-error') else 'submission_draft.json')
            runner.save_json(draft,dict(strategy_id='fixture-ready',
                strategy_version=f'fixture-{title}',strategy_type='stock',run_role='OFFICIAL_MAIN',
                cost_scenario='NORMAL',**manifest))
            runner.save_json(job/'strategy_branches.json',{'branches':[
                dict(branch_id='main',name='fixture',source_pages=[1],role='strategy',
                     disposition='selected',reason='primary'),
                dict(branch_id='other',name='alternative',source_pages=[2],role='strategy',
                     disposition='pending',reason='different mechanism'),
            ]})
            if title=='promoted':
                (job/'audit_inputs.json').unlink()
                return dict(status='diagnostic',reason='需要补证才能送检')
            if title=='ready':
                return dict(status='diagnostic',reason='已有完整核账和正式提交材料，研究回执标注错误')
            return dict(status='official',reason='测试正式账本')

        def source(job,row):
            (job/'source.pdf').write_bytes(Path(row['original']).read_bytes())
            runner.atomic_text(job/'source.txt',row['title'])

        def record(db,job,row):
            return dict(id='fixture-missing' if row['title']=='missing' else 'fixture-ready',
                        url='https://example.invalid/fixture',title=row['title'],platform='test')

        def catalog(*args):
            if args[0]=='find':
                return [dict(id=args[-1],title='重建测试',url='https://example.invalid/fixture',evidence_paths=[])]
            if args[0]=='upsert':
                return {'ids':['fixture-reconstructed']}
            return {}

        runner.save_json(settings['PIPELINE_MAIL'],dict(sender='a@example.invalid',recipient='b@example.invalid'))
        for index,(name,payload) in enumerate([('missing',b'b'),('audit-timeout',b'c'),
                                               ('audit-invalid',b'd'),('audit-hold',b'e'),
                                               ('late-start',b'f'),('ready',b'a'),('duplicate',b'a'),
                                               ('history-short',b'g'),('long-diagnostic',b'h'),('v1-fail',b'i'),
                                               ('pending-evidence',b'j'),('changed-after-freeze',b'k'),
                                               ('rejected-proxy',b'l'),('weak-complete',b'm'),
                                               ('promoted',b'n'),('missing-middle',b'o'),('invalid-full',b'p'),
                                               ('ambiguous-short',b'q'),('throttled',b'r'),('sparse-calendar',b's'),
                                               ('wrong-cost',b't'),('changed-implementation',b'u'),
                                               ('impossible-turnover',b'v'),('unverified-rules',b'w'),
                                               ('premature-rules',b'x'),('late-evidence-error',b'y')]):
            file=inbox/f'{name}.pdf'
            file.write_bytes(b'%PDF-1.4\n'+payload*200+b'\n%%EOF')
            os.utime(file,(1_700_000_000+index,)*2)
        with patch.multiple(runner,**settings), patch.object(runner,'records',return_value=[]), \
             patch.object(runner,'load_source',side_effect=source), patch.object(runner,'catalog_id',side_effect=record), \
             patch.object(runner,'catalog',side_effect=catalog), patch.object(runner,'start_research'), \
             patch.object(runner,'refresh_calendar',return_value=local_calendar), \
             patch.object(runner,'model_run',side_effect=model), patch.object(runner.smtplib,'SMTP_SSL',DummySMTP):
            runner.run(watch=False,workers=1)
            runner.run(watch=False,workers=1)
            with closing(runner.open_queue()) as db:
                result=db.execute('select * from queue order by arrived').fetchall()
                assert [r['title'] for r in result]==['missing','audit-timeout','audit-invalid','audit-hold',
                                                     'late-start','ready','history-short','long-diagnostic','v1-fail',
                                                     'pending-evidence','changed-after-freeze','rejected-proxy',
                                                     'weak-complete','promoted','missing-middle','invalid-full',
                                                     'ambiguous-short','throttled','sparse-calendar','wrong-cost',
                                                     'changed-implementation','impossible-turnover',
                                                     'unverified-rules','premature-rules','late-evidence-error']
                assert [r['status'] for r in result]==['rules_missing','audit_uncertain','audit_uncertain','audit_hold',
                                                      'evidence_missing','passed','history_short','v1_pending_evidence',
                                                      'v1_fail','evidence_missing','run_failed','history_short',
                                                      'v1_pending_evidence','passed','evidence_missing','invalid_replay',
                                                      'v1_diagnostic_fail','data_pending','evidence_missing',
                                                      'invalid_replay','evidence_missing','invalid_replay',
                                                      'rules_pending','v1_fail','invalid_replay'], [
                    (r['title'],r['status'],r['detail'][:120]) for r in result]
                for row in result:
                    phases = ['research']
                    if row['title'] in ('long-diagnostic','promoted','late-evidence-error'):
                        phases.append('evidence_review')
                    if row['title']=='ambiguous-short':
                        phases.append('coverage_review')
                    if row['title']=='throttled':
                        phases.append('data_retry')
                    if row['title'] in ('unverified-rules','premature-rules'):
                        phases.append('rule_gap_review')
                    assert [event['phase'] for event in journal if event['title']==row['title'] and not event['audit']]==phases
                short = result[6]
                assert '停止详细回测' in short['detail'] and not (jobs/short['sha']/'ledger.csv').exists()
                assert not (jobs/short['sha']/'v1_diagnostic.json').exists()
                assert not (jobs/result[4]['sha']/'v1_diagnostic.json').exists()
                diagnostic = jobs/result[7]['sha']
                assert (diagnostic/'ledger.csv').is_file() and not (diagnostic/'v1_result.json').exists()
                assert not (diagnostic/'official_submission.json').exists() and result[7]['v1'] is None
                diagnosis = runner.read_json(diagnostic/'v1_diagnostic.json')
                assert diagnosis['run_role']=='DIAGNOSTIC_ONLY' and diagnosis['screening_status']=='PASS'
                assert Path(diagnosis['source'])==(diagnostic/'ledger.csv').resolve()
                assert diagnosis['inputs']['ledger_sha256']==runner.digest(diagnostic/'ledger.csv')
                assert result[7]['mail']=='not_required' and not (diagnostic/'audit.md').exists()
                failed = jobs/result[8]['sha']
                assert result[8]['v1']=='FAIL' and result[8]['candidate']=='fixture-reconstructed'
                assert result[13]['v1']=='PASS' and result[13]['candidate']=='fixture-reconstructed'
                assert runner.read_json(failed/'official_submission.json')['strategy_id']=='fixture-reconstructed'
                assert not (jobs/result[10]['sha']/'official_submission.json').exists()
                assert '缺少明确的代理验收' in result[11]['detail'] and not (jobs/result[11]['sha']/'ledger.csv').exists()
                assert [event['title'] for event in journal if event['audit']]==['audit-timeout','audit-invalid','audit-hold','ready','promoted']
                assert result[3]['mail']=='SMTP_ACCEPTED' and result[4]['v1'] is None
                for row in result[1:3]:
                    unresolved = jobs/row['sha']
                    assert row['v1']=='PASS' and row['retry_after']==0 and row['mail']=='SMTP_ACCEPTED'
                    receipt = runner.read_json(unresolved/'audit_uncertain.json')
                    assert len(receipt['checks'])==22 and not runner.audit_decision(receipt)
                    assert (unresolved/'audit.md').is_file() and (unresolved/'mail_state.json').is_file()
                passed = result[5]
                assert passed['mail']=='SMTP_ACCEPTED'
                job=jobs/passed['sha']
                scored=runner.read_json(job/'v1_result.json')
                frozen_proofs=runner.read_json(job/'freeze_receipt.json')['proxy_files']
                assert frozen_proofs['check_proxy.json']==runner.digest(job/'check_proxy.json')
                assert scored['start']=='2019-01-01' and scored['years']>7
                assert runner.relay(record(db,job,passed),scored,runner.read_json(job/'protocol.json'),
                                    job,'可进入模拟观察，人工安排')=='SMTP_ACCEPTED'
                assert sends==['b@example.invalid']*5
                ready_events = [event for event in journal if event['title']=='ready']
                assert [(event['phase'],event['model'],event['audit']) for event in ready_events] == [
                    ('research',runner.RESEARCH_MODEL,False), ('audit',runner.AUDIT_MODEL,True),
                ]
                assert passed['archived'] and Path(passed['archived']).is_file()
                runner.scan(db)
                assert db.execute('select count(*) from queue').fetchone()[0]==25
                assert (jobs/result[12]['sha']/'v1_diagnostic.json').is_file()
                assert runner.read_json(jobs/result[12]['sha']/'v1_diagnostic.json')['start']=='2018-12-31'
                assert runner.read_json(failed/'v1_result.json')['start']=='2018-12-31'
                assert (jobs/result[16]['sha']/'v1_diagnostic.json').is_file()
                for row in [*result[14:16],*result[17:-1]]:
                    assert not (jobs/row['sha']/'v1_diagnostic.json').exists()
                actual_error = jobs/result[-1]['sha']
                assert (actual_error/'v1_diagnostic.json').is_file() and not (actual_error/'v1_result.json').exists()
                bad=dict(scored,start='1970-01-01',cagr=float('inf'))
                try:
                    runner.validate_benchmark_result(bad,scored['start'],pd.Timestamp(scored['end']))
                except ValueError:
                    pass
                else:
                    raise AssertionError('非有限值未被拒绝')
                assert runner.audit_decision(audit_answer,artifacts)
                assert {item['name'] for item in runner.read_json(job/'audit_inputs_excluded.json')}=={
                    'evidence.md','replay.md'}
                numbers = {key:{'value':None,'basis':'不是模拟前必需的扩展数字'}
                           for key in runner.AUDIT_NUMBERS}
                assert runner.audit_decision({**audit_answer,'numbers':numbers,'key_evidence_complete':False},
                                              artifacts)
                opaque_trials = [{**check,'status':'unverified'} if check['id']=='QG-10' else check
                                 for check in audit_answer['checks']]
                assert runner.audit_decision({**audit_answer,'checks':opaque_trials}, artifacts)
                known_trials = [{**check,'status':'hit','severity':'P0'} if check['id']=='QG-10' else check
                                for check in audit_answer['checks']]
                assert not runner.audit_decision({**audit_answer,'checks':known_trials}, artifacts)
                red = [{**check,'status':'hit','severity':'P0'} if check['id'] in ('QG-12','QG-14') else check
                       for check in audit_answer['checks']]
                boundary = dict(evaluation_use='separate',first_evaluation_signal='2020-01-02',
                                last_selection_signal='2019-11-29',target='same_snapshot',
                                last_training_label_observation='2019-11-29',references=fixture_refs)
                assert runner.audit_decision({**audit_answer,'checks':red,'validation_timeline':boundary},
                                              artifacts)
                conflict = [{**c,'actual_violation':'yes'} if c['id']=='QG-12' else c for c in red]
                assert runner.decide_simulation({**audit_answer,'checks':conflict,'validation_timeline':boundary},
                                                artifacts)['outcome']=='pending'
                bad_ref = [{**c,'references':[dict(file='account.py',location='line 999999')]} if c['id']=='QG-01' else c
                           for c in audit_answer['checks']]
                assert not runner.audit_decision({**audit_answer,'checks':bad_ref},artifacts)
                leaked = {**boundary,'last_training_label_observation':'2020-01-03','target':'forward'}
                assert not runner.audit_decision({**audit_answer,'validation_timeline':leaked},artifacts)
                reused = {**boundary,'evaluation_use':'used_for_selection'}
                assert not runner.audit_decision({**audit_answer,'validation_timeline':reused},artifacts)
                try:
                    runner.full_research_ledger(job,{**runner.read_json(job/'protocol.json'),'currency':'USD'})
                except runner.UnsupportedLedgerMarket:
                    pass
                else:
                    raise AssertionError('海外美元账本不能把沪深日历当作原市场日历')
                p0=[*audit_answer['checks']]
                p0[0]={**p0[0],'status':'hit','severity':'P0'}
                assert not runner.audit_decision({**audit_answer,'checks':p0},artifacts)
                costs = [{**check,'status':'hit','severity':'P1'} if check['id']=='QG-15' else check
                         for check in audit_answer['checks']]
                assert runner.audit_decision({**audit_answer,'checks':costs},artifacts)
                hard_costs = [{**check,'severity':'P0'} if check['id']=='QG-15' else check for check in costs]
                assert not runner.audit_decision({**audit_answer,'checks':hard_costs},artifacts)
                assert runner.strategy_branch_count(job)==(1,1)
                na = [{**check,'status':'not_applicable'} for check in audit_answer['checks']]
                assert not runner.audit_decision({**audit_answer,'checks':na},artifacts)
                large = job/'large_orders.csv'
                large.write_text('date,volume\n'+'2020-01-02,100\n'*65000,encoding='utf-8')
                assert large.stat().st_size>800_000
                runner.save_json(job/'audit_inputs.json',['account.py',large.name])
                assert '全表数值统计' in runner.extract_audit_packet(job,job/'ledger.csv')
                original_model_run = original_audit_run
                def recover_model(work,phase,instruction,name,audit):
                    if (work/'audit_decision.json').is_file():
                        return original_model_run(work,phase,instruction,name,audit=True)
                    fresh.append(work)
                    runner.save_json(work/'audit_dispatch.json',dict(sha256=runner.sha256(instruction.encode()).hexdigest()))
                    runner.save_json(work/'audit_decision.json',audit_answer)
                    return audit_answer

                fresh=[]
                score_hash=runner.digest(job/'v1_result.json')
                account_hash=runner.digest(job/'account_validation.json')
                with patch.object(runner,'model_run',side_effect=recover_model), \
                     patch.object(runner,'evaluate_verified',side_effect=AssertionError('只补审重新执行 V1')):
                    with patch.object(runner,'relay',side_effect=OSError('审计写回后中断')):
                        try:
                            runner.reaudit(db,passed['sha'],'隔离审计恢复')
                        except OSError:
                            pass
                        else:
                            raise AssertionError('未触发恢复测试')
                    work = runner.reaudit(db,passed['sha'],'隔离审计恢复')
                assert len(fresh)==1 and runner.read_json(work/'reaudit_outcome.json')['status']=='passed'
                assert runner.digest(job/'v1_result.json')==score_hash
                assert runner.digest(job/'account_validation.json')==account_hash
                (failed/'account_evidence.json').rename(failed/'account_evidence_before_legacy.json')
                with patch.object(runner,'research_result',return_value=(dict(status='official',reason='旧账本待核'),True)):
                    runner.process(db,result[8])
                pending = db.execute('select status,detail from queue where sha=?',(result[8]['sha'],)).fetchone()
                assert pending['status']=='v1_pending_evidence' and '参考分 FAIL' in pending['detail']
                ledger = pd.read_csv(job/'ledger.csv')
                ledger.loc[len(ledger)-1,'equity'] = 650000
                ledger.to_csv(job/'ledger.csv',index=False)
                try:
                    runner.evaluate_verified(job,job/'official_submission.json',job/'v1_result.json')
                except ValueError as exc:
                    assert '不匹配' in str(exc)
                else:
                    raise AssertionError('正式旧成绩不能用于改动后的账本')
                assert runner.read_json(job/'v1_result.json')==scored
                skipped_before = inbox/'desktop-incomplete.pdf'
                skipped_before.write_bytes(b'%PDF-1.4\n'+b'model-blocked-old'*20+b'\n%%EOF')
                old_sha = runner.digest(skipped_before)
                old = dict(id='desktop-incomplete',title='未走完的旧研究',pdf_sha256=old_sha,
                           research_status='stopped',research_runs=[dict(run_id='desktop',
                              checkpoints={'data':dict(outcome='blocked')})])
                with patch.object(runner,'records',return_value=[old]):
                    runner.scan(db)
                assert db.execute('select status from queue where sha=?',(old_sha,)).fetchone()[0]=='queued'
                tally_job = base/'test-tally'
                tally_job.mkdir()
                runner.save_json(tally_job/'v1_result.json',dict(run_role='DIAGNOSTIC_ONLY',
                                     cost_scenario='NORMAL',screening_status='PASS'))
                runner.save_json(base/'20261122-all-51-v1-recheck.json',
                                 [dict(sha=passed['sha'],job=str(tally_job),retest=None)])
                assert '已核正式 V1 0 份' in runner.v1_batch_progress([passed])[2]
                prior = result[0]
                prior_job = runner.job_directory(prior)
                (prior_job/'source.pdf').write_bytes(Path(prior['original']).read_bytes())
                retest = runner.enqueue_retest(db,prior['sha'],'验证有据重开',None)
                now = db.execute('select * from queue where sha=?',(prior['sha'],)).fetchone()
                assert now['status']=='queued' and runner.job_directory(now)==retest
                assert runner.research_run_id(now)!=runner.research_run_id(prior)
                assert (prior_job/'evidence.md').is_file() and not (retest/'evidence.md').exists()
                assert runner.digest(retest/'source.pdf')==prior['sha']
                with patch.object(runner,'start_research',new=original_start_research), \
                     patch.object(runner,'catalog') as catalog_start:
                    runner.start_research(dict(id='fixture-missing',pdf_sha256=prior['sha']),
                                          runner.research_run_id(now),retest)
                    assert catalog_start.call_args.args[-2:]==('--reopen-reason','验证有据重开')
                    runner.start_research(dict(id='desktop-incomplete',pdf_sha256=old_sha,
                                              research_runs=[dict(run_id='desktop')]),'watch-default',base)
                    assert catalog_start.call_args.args[-2]=='--reopen-reason'
                    assert 'desktop' in catalog_start.call_args.args[-1]
        print(json.dumps(dict(dedup=True,blocked_continues=True,official_v1_fixture=scored['screening_status'],
                          date_guard=True,isolated_dispatch=True,mail_once_mock=True,archive=True,
                          retest_preserves_previous=True,audit_uncertain_continues_without_retry=True,
                          short_history_skips_replay=True, one_research_session_per_pdf=True,
                          long_diagnostic_completes=True, reconstructed_official_fail=True,
                          unverified_data_not_short=True, changed_freeze_rejected=True,
                          changed_implementation_rejected=True, false_calendar_rejected=True,
                          incompatible_fees_cannot_formal_score=True, wrong_cached_score_rejected=True,
                          confirmed_bad_ledger_not_scored=True,
                          unfinished_desktop_run_not_skipped=True, diagnostic_result_not_counted_as_formal=True,
                          audit_large_table_summary=True, audit_rejects_unsubstantiated_pass=True,
                          simulation_ignores_optional_numbers=True, past_lookback_not_called_future_label=True,
                          confirmed_reuse_and_forward_label_block=True, foreign_calendar_not_substituted=True,
                          diagnostic_v1_pass_does_not_trigger_audit_or_mail=True,
                          rejected_proxy_does_not_fill_diagnostic_history=True),
                         ensure_ascii=False),flush=True)
        if run_live_model:
            job=base/'independent_probe'
            job.mkdir()
            text=runner.AUDIT+"\n这是专门的隔离探针。没有真实资产或研究证据。"
            text+="请把所有 QG 条目标为 unverified、key_evidence_complete=false，并在摘要写 AUDIT_ISOLATION_PROBE。"
            answer=runner.model_run(job,'audit',text,runner.AUDIT_MODEL,audit=True)
            raw=(job/'audit.jsonl').read_text(encoding='utf-8',errors='replace')
            dispatch=runner.read_json(job/'audit_dispatch.json')
            assert (dispatch['model'],dispatch['effort'],dispatch['context_window'],dispatch['returncode'])==(
                runner.AUDIT_MODEL,runner.AUDIT_EFFORT,872_000,0)
            assert not answer['key_evidence_complete'] and len(answer['checks'])==22
            assert 'AUDIT_ISOLATION_PROBE' in answer['summary'] and '"command_execution"' not in raw
            assert not runner.audit_decision(answer)
            print(f'真实新审计会话已验证：{dispatch["model"]}/{dispatch["effort"]}，22 项返回，无命令调用。',flush=True)


def parallel_smoke():
    with tempfile.TemporaryDirectory(prefix='pipeline-parallel-') as folder:
        base = Path(folder)
        inbox, jobs, summary = [base/x for x in ('inbox','jobs','summary')]
        for directory in (inbox,jobs,summary):
            directory.mkdir()
        settings = dict(INBOX=inbox,JOBS=jobs,SUMMARY=summary,RUNTIME=base,STATE=base/'queue.sqlite3')
        workers = runner.DEFAULT_WORKERS
        first_wave = Barrier(workers,timeout=10)
        lock = Lock()
        seen = []
        in_flight = peak = 0

        def process(db,row):
            nonlocal in_flight, peak
            with lock:
                assert row['sha'] not in seen
                seen.append(row['sha'])
                position = len(seen)
                in_flight += 1
                peak = max(peak,in_flight)
            runner.update(db,row['sha'],status='prepare',detail='并行验收')
            runner.summarize(db)
            if position<=workers:
                first_wave.wait()
            runner.update(db,row['sha'],status='diagnostic',detail='并行验收完成')
            runner.summarize(db)
            with lock:
                in_flight -= 1

        for index in range(workers+2):
            source = inbox/f'{index}.pdf'
            source.write_bytes(b'%PDF-1.4\n'+bytes([65+index])*200+b'\n%%EOF')
            os.utime(source,(1_700_000_000+index,)*2)
        priority = runner.digest(source)
        with patch.multiple(runner,**settings),patch.object(runner,'records',return_value=[]), \
             patch.object(runner,'process',side_effect=process):
            runner.run(watch=False,priority_sha=priority)
            with closing(runner.open_queue()) as db:
                assert dict(db.execute('select status,count(*) from queue group by status'))=={'diagnostic':workers+2}
        assert peak==workers and len(seen)==workers+2 and in_flight==0 and priority in seen[:workers]
        print(f'并行验证通过：峰值 {workers} 篇，优先研报进入首批，每篇各派发一次。',flush=True)


def resume_smoke():
    with tempfile.TemporaryDirectory(prefix='pipeline-resume-') as folder:
        job = Path(folder)
        session_id = '00000000-0000-4000-8000-000000000001'

        def interrupted(command, **kwargs):
            assert command[-1]=='-' and 'resume' not in command and '--ephemeral' not in command
            assert kwargs['stdout'] is not kwargs['stderr']
            kwargs['stderr'].write('测试异常输出\n')
            kwargs['stdout'].write(json.dumps({'type':'thread.started','thread_id':session_id},separators=(',', ':'))+'\n')
            return SimpleNamespace(returncode=1)

        def finish(command, **kwargs):
            assert command[-3:]==['resume',session_id,'-']
            assert '已冻结的规则' in kwargs['input']
            runner.atomic_text(job/'evidence.md','已从同会话完成证据。')
            runner.save_json(job/'.research.answer.json',dict(status='rules_missing',reason='续跑夹具'))
            return SimpleNamespace(returncode=0)

        with patch.object(runner.subprocess,'run',side_effect=interrupted) as launch:
            try:
                runner.model_run(job,'research','开始研究',runner.RESEARCH_MODEL)
            except runner.ModelError:
                pass
            else:
                raise AssertionError('中断被误当成成功')
            launch.side_effect = finish
            assert runner.model_run(job,'research','开始研究',runner.RESEARCH_MODEL)['reason']=='续跑夹具'
            assert runner.model_run(job,'research','开始研究',runner.RESEARCH_MODEL)['reason']=='续跑夹具'
            assert launch.call_count==2
            def evidence_review(command, **kwargs):
                assert command[-3:]==['resume',session_id,'-']
                runner.save_json(job/'.evidence_review.answer.json',dict(status='evidence_missing',reason='补证未齐'))
                return SimpleNamespace(returncode=0)

            launch.side_effect = evidence_review
            assert runner.model_run(job,'evidence_review','复核账本',runner.RESEARCH_MODEL)['reason']=='补证未齐'
            assert launch.call_count==3

            coverage_answer = dict(status='data_pending',reason='历史取数待补')
            answer_path = job/'.coverage_review.answer.json'
            coverage_log = job/'coverage_review.jsonl'
            decision_path = job/'coverage_review_decision.json'
            persisted = runner.save_json

            def coverage_review(command, **kwargs):
                assert command[-3:]==['resume',session_id,'-']
                persisted(answer_path,coverage_answer)
                return SimpleNamespace(returncode=0)

            def interrupted_before_handoff(path, value):
                if path==decision_path:
                    raise OSError('模拟完成模型调用后、写入结论前中断')
                persisted(path,value)

            launch.side_effect = coverage_review
            with patch.object(runner,'save_json',side_effect=interrupted_before_handoff):
                try:
                    runner.model_run(job,'coverage_review','复核历史',runner.RESEARCH_MODEL)
                except OSError as exc:
                    assert '写入结论前' in str(exc)
                else:
                    raise AssertionError('测试未触发结论交接中断')
            assert launch.call_count==4 and not decision_path.exists()
            assert runner.recover_research_answer(coverage_log,answer_path,'不同请求') is None
            receipt = json.loads(coverage_log.read_text(encoding='utf-8').splitlines()[-1])
            persisted(answer_path,{**coverage_answer,'reason':'输出发生变动'})
            assert runner.recover_research_answer(coverage_log,answer_path,receipt['request_sha256']) is None
            persisted(answer_path,coverage_answer)
            assert runner.model_run(job,'coverage_review','复核历史',runner.RESEARCH_MODEL)==coverage_answer
            assert runner.read_json(decision_path)==coverage_answer and launch.call_count==4
    print('中断接续验证通过：未完成的恢复原会话，已成功返回的核对指纹后直接交接。',flush=True)


def closing_guards_smoke():
    with tempfile.TemporaryDirectory(prefix='pipeline-closing-guards-') as folder:
        root = Path(folder)
        job = root/'case'
        job.mkdir()
        days = pd.bdate_range('2020-01-02',periods=6)
        ds = days.strftime('%Y-%m-%d').tolist()
        symbol = 'fixture.SH'
        protocol = dict(v1_preflight=dict(ledger_path='ledger.csv',equity_column='equity',initial_capital=10000))
        runner.save_json(job/'protocol.json',protocol)
        runner.atomic_text(job/'replay.py','from signals import pick\n')
        runner.atomic_text(job/'signals.py','def pick(): pass\n')
        runner.atomic_text(job/'rights.txt','synthetic complete exchange notice feed\n')
        action = dict(id='div-one',symbol=symbol,record_date=ds[0],ex_date=ds[1],pay_date=ds[3],
                      cash_per_share=.1,source='rights.txt')
        cover = [dict(symbol=symbol,start=ds[0],end=ds[-1],source='rights.txt')]
        fees = [dict(symbol='*',side=side,start=ds[0],end=ds[-1],commission_rate=0,min_commission=0,
                     tax_rate=0,other_rate=0,slippage_rate=0) for side in ('buy','sell')]
        pd.DataFrame(fees).to_csv(job/'fees.csv',index=False)
        runner.save_json(job/'account_evidence.json',dict(quotes='quotes.csv',fills='fills.csv',positions='positions.csv',
                        events='events.csv',fee_schedule='fees.csv',cash_actions='cash.json'))
        runner.save_json(job/'replay_inputs.json',dict(entrypoint='replay.py',
                     files=['replay.py','signals.py','rights.txt','quotes.csv','fees.csv','cash.json']))

        def run_account(mode, sell=False):
            fund = pd.DataFrame(dict(date=ds,symbol=symbol,open=10,close=10,high=10,low=10,volume=50000,
                                     lower=9,upper=11,suspended=0,buy_min=100,buy_step=100,t_plus=1))
            if mode=='halt':
                fund.loc[2,['close','suspended']] = [0,1]
            if mode=='missing_quote':
                fund = fund.drop(index=2)
            fund.to_csv(job/'quotes.csv',index=False)
            actions = [action]
            if mode in ('two_valid','wrong_settlement'):
                actions.append({**action,'id':'div-two','cash_per_share':.2})
            runner.save_json(job/'cash.json',dict(coverage=cover,actions=actions))
            runner.save_json(job/'replay_seal.json',replay_fingerprints(job,protocol))
            dates = [ds[0],ds[2]] if sell else [ds[0]]
            tx = pd.DataFrame(dict(date=dates,symbol=symbol,signal_date=[ds[0],ds[1]] if sell else [ds[0]],
                                  seq=0,quantity=[1000,-1000] if sell else [1000],price=10,commission=0,
                                  tax=0,other_fee=0,slippage=0))
            tx.loc[0,'signal_date'] = '2019-12-31'
            tx.to_csv(job/'fills.csv',index=False)
            pd.DataFrame(dict(date=ds[:2] if sell else ds,symbol=symbol,quantity=1000,mark=10)
                         ).to_csv(job/'positions.csv',index=False)
            postings=[]
            def post(day,kind,id,paid,due):
                postings.append(dict(date=ds[day],seq=len(postings)+1,symbol=symbol,kind=kind,source='rights.txt',
                                     cash_delta=paid,receivable_delta=due,quantity=0,action_id=id))
            if mode!='missing_dividend':
                for a in actions:
                    post(1,'distribution',a['id'],0,a['cash_per_share']*1000)
                if mode=='duplicate_dividend':
                    post(1,'distribution','div-one',0,100)
                for a in actions:
                    identity = 'div-one' if mode=='wrong_settlement' else a['id']
                    post(3,'settlement',identity,a['cash_per_share']*1000,-a['cash_per_share']*1000)
            pd.DataFrame(postings,columns=['date','seq','symbol','kind','source','cash_delta','receivable_delta',
                                          'quantity','action_id']).to_csv(job/'events.csv',index=False)
            cash = due = 0
            nav = []
            for i,day in enumerate(ds):
                if sell and i==2:
                    cash += 10000
                cash += sum(p['cash_delta'] for p in postings if p['date']==day)
                due += sum(p['receivable_delta'] for p in postings if p['date']==day)
                nav.append(dict(date=day,cash=cash,receivable=due,equity=cash+due+(0 if sell and i>=2 else 10000)))
            frame = pd.DataFrame(nav)
            frame.to_csv(job/'ledger.csv',index=False)
            return runner.reconcile_account(job,protocol,frame,days,job/'ledger.csv')

        assert run_account('halt')['carried_suspended_marks']==1
        assert run_account('two_valid',sell=True)['events']==4
        for scenario,expected in (('missing_quote',ValueError),('missing_dividend',runner.AccountMismatch),
                                  ('duplicate_dividend',runner.AccountMismatch),('wrong_settlement',runner.AccountMismatch)):
            try:
                run_account(scenario)
            except expected as exc:
                assert isinstance(exc,runner.AccountMismatch)==(scenario!='missing_quote')
            else:
                raise AssertionError(f'{scenario} 错误通过')

        runner.atomic_text(job/'source.txt','\n--- PDF 第 1 页 ---\nfixture\n')
        runner.save_json(job/'official_submission.json',dict(date_column='date',equity_column='equity'))
        runner.save_json(job/'audit_inputs.json',[])
        runner.extract_audit_packet(job,job/'ledger.csv')
        refs = {r['artifact']:r['coverage'] for r in runner.read_json(job/'audit_packet_receipt.json')}
        assert 'signals.py' in refs
        assert runner.decide_simulation(dict(numbers={}, checks=[
            dict(id='QG-03',status='pass',severity='none',evidence='test',references=[
                dict(file='source.txt',location='page 1'),dict(file='signals.py',location='line 1')])]),refs)['outcome']=='approved'

        with patch.multiple(runner,RUNTIME=root,JOBS=root,STATE=root/'queue.sqlite3'), closing(runner.open_queue()) as db:
            runner.atomic_text(job/'audit.md','测试独立审计\n')
            with patch.object(runner.pipeline_mail,'send',side_effect=[runner.pipeline_mail.BeforeSubmission(),None]) as send:
                rec = dict(id='candidate',title='fixture',url='local')
                result = dict(screening_status='PASS',cagr=.1,mdd=.1)
                cfg = dict(strategy_version='fixture',source_kind='original')
                assert runner.relay(rec,result,cfg,job)=='RETRY_PENDING'
                db.execute("insert into queue(sha,title,original,arrived,status,mail) values('case','fixture','x',0,'passed','RETRY_PENDING')")
                db.commit()
                event = next((root/'mail_outbox').glob('*.json'))
                runner.save_json(event,{**runner.read_json(event),'retry_after':0})
                runner.recover_notifications(db)
                assert db.execute("select mail from queue where sha='case'").fetchone()[0]=='SMTP_ACCEPTED'
                assert runner.relay(rec,result,cfg,job)=='SMTP_ACCEPTED' and send.call_count==2
    print('收尾验证通过：持仓缺价和错误权益被挡，正常停牌及合法两笔分红通过，封存依赖进入审计，终态邮件独立恢复。',flush=True)


if __name__=='__main__':
    import sys
    smoke('--live-audit' in sys.argv)
    parallel_smoke()
    resume_smoke()
    closing_guards_smoke()
