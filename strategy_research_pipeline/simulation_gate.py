"""Map independent audit evidence to a recommendation for paper observation."""

from datetime import date
import re


POLICY = 'paper-observation-v2'
FIXED_HARD = frozenset({
    'QG-01','QG-02','QG-03','QG-04','QG-05','QG-06','QG-07',
    'QG-10','QG-11','QG-13','QG-17','QG-21',
})
MIXED = frozenset({'QG-08','QG-15','QG-22'})
TIMELINE_RULES = frozenset({'QG-12','QG-14'})
CRITICAL = (FIXED_HARD - {'QG-10'}) | MIXED | TIMELINE_RULES
ALWAYS_APPLIES = frozenset({
    'QG-01','QG-02','QG-07','QG-08','QG-15','QG-17','QG-18','QG-19','QG-20','QG-21','QG-22',
})


def reference_in_scope(ref, artifacts):
    if (not isinstance(ref,dict) or not isinstance(ref.get('file'),str)
            or not isinstance(ref.get('location'),str) or ref['file'] not in artifacts):
        return False
    scope = artifacts[ref['file']]
    where = ref['location'].strip()
    location = re.fullmatch(r'(line|row|page)\s+(\d{1,9})(?:\s*[-–]\s*(\d{1,9}))?',where,re.I)
    if location:
        kind,first,last = location.groups()
        first,last = int(first),int(last or first)
        if first<1 or last<first:
            return False
        if kind.lower()=='line':
            return last<=scope.get('lines',0)
        if kind.lower()=='page':
            pages = set(scope.get('pages',()))
            return last<=max(pages,default=0) and all(n in pages for n in range(first,last+1))
        rows = scope.get('sample_rows')
        return last<=scope.get('rows',0) and (rows is None or all(n in rows for n in range(first,last+1)))
    for prefix,key in (('field:','fields'),('stat:','statistics')):
        if where.lower().startswith(prefix):
            return where[len(prefix):].strip() in scope.get(key,())
    return False


def has_reference(check, artifacts):
    refs = check.get('references')
    return bool(isinstance(refs,list) and refs and all(reference_in_scope(ref,artifacts) for ref in refs))


def day(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def validation_boundary(timeline, artifacts):
    """Check confirmed evaluation reuse and actual label observations, not lookback length."""
    if not isinstance(timeline,dict) or not has_reference(timeline,artifacts):
        return {}
    use = timeline.get('evaluation_use')
    first_test = day(timeline.get('first_evaluation_signal'))
    last_select = day(timeline.get('last_selection_signal'))
    last_label = day(timeline.get('last_training_label_observation'))
    target = timeline.get('target')
    result = {}
    if use=='used_for_selection':
        result['QG-12'] = ('hard','用于报告确认成绩的数据明确参与了选型或规则调整')
    elif use=='separate' and first_test and last_select:
        if last_select<first_test:
            result['QG-12'] = ('clear','已证实只在开发期选型，开发期重拟合不等于复用后续确认期')
        else:
            result['QG-12'] = ('pending','选择期与确认期日期相交，实际复用情况需核实')
    elif use=='no_selection':
        result['QG-12'] = (('pending','同时声称未选型且给出最后选择日，需要核实日期含义') if last_select else
                           ('clear','有证据表明此分支没有用报告区间选型或改规则'))
    if target=='none':
        result['QG-14'] = (('pending','同时声明没有标签且给出标签实际观测日，需要核实') if last_label else
                           ('clear','有证据表明此分支无训练或选型标签'))
    elif target in ('same_snapshot','forward') and first_test and last_label:
        if last_label>first_test:
            result['QG-14'] = ('hard','训练标签的实际观测已越过首个测试信号日')
        elif last_label==first_test:
            result['QG-14'] = ('pending','训练标签与首个测试信号同日，需核实盘中先后顺序')
        else:
            result['QG-14'] = ('clear','标签在测试前已观测；向后读取已知历史行情本身不等于标签穿越')
    return result


def decide(audit, artifacts):
    hard = []
    pending = []
    notes = []
    timeline = validation_boundary(audit.get('validation_timeline'),artifacts)
    for check in audit['checks']:
        rule,status,severity = (check[key] for key in ('id','status','severity'))
        fact = timeline.get(rule)
        if rule in TIMELINE_RULES and status!='not_applicable':
            claim = check.get('actual_violation')
            if (not fact or fact[0]=='pending' or not has_reference(check,artifacts)
                    or not check['evidence'].strip() or status=='unverified'
                    or claim != {'hard':'yes','clear':'no'}[fact[0]]):
                pending.append((rule,'逐项事实、可定位证据与时间线未能互证'))
                continue
            flag,reason = fact
            if flag=='hard':
                hard.append((rule,reason))
            elif status=='hit':
                notes.append((rule,reason))
            continue
        if rule in TIMELINE_RULES and (check.get('actual_violation')=='yes' or fact and fact[0]!='clear'):
            pending.append((rule,'不适用声明与验证时间线冲突'))
            continue
        if not check['evidence'].strip() or (status!='unverified' and not has_reference(check,artifacts)):
            if rule in CRITICAL or status=='hit' and severity=='P0':
                pending.append((rule,'缺可定位的事实依据'))
            else:
                notes.append((rule,'检查项缺可定位依据'))
            continue
        if status=='not_applicable' and rule in ALWAYS_APPLIES:
            pending.append((rule,'适用于该账本的检查被标为不适用'))
        elif status=='unverified' and rule in CRITICAL:
            pending.append((rule,check['evidence']))
        elif status=='hit' and (rule in FIXED_HARD or rule in MIXED and severity=='P0'):
            hard.append((rule,check['evidence']))
        elif status in ('hit','unverified'):
            notes.append((rule,check['evidence']))
    result = 'rejected' if hard else 'pending' if pending else 'approved'
    return dict(policy=POLICY,outcome=result,hard=hard,pending=pending,notes=notes,
                missing_numbers=[key for key,point in audit['numbers'].items() if point['value'] is None])
