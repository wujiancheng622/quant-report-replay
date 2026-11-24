"""Independent cash, position and NAV reconciliation for an exchange cash account."""

from collections import defaultdict
import math
from bisect import bisect_left

import pandas as pd

from v1_handoff import check_replay_seal, digest, inside, read_json


class AccountMismatch(ValueError):
    """The available evidence contradicts the submitted replay."""


def verify_distributions(job, spec, sealed, dates, holdings, events, supplement=None):
    """Match expected cash rights and payments to independently rebuilt shares."""
    if supplement is None:
        name = spec.get('cash_actions')
        if not isinstance(name, str) or not name:
            raise ValueError('缺少已封存的现金权益全期覆盖检查')
        path = inside(job, name)
        if path.relative_to(job.resolve()).as_posix() not in sealed['files']:
            raise ValueError('现金权益清单未封存')
        supplement = read_json(path)
    if not isinstance(supplement, dict):
        raise ValueError('现金权益清单无效')
    coverage, actions = supplement.get('coverage'), supplement.get('actions')
    if (not isinstance(coverage,list) or not isinstance(actions,list)
            or any(not isinstance(item,dict) for item in (*coverage,*actions))):
        raise ValueError('现金权益需要覆盖回执和逐事件清单')

    def frozen_source(name):
        if not isinstance(name, str):
            raise ValueError('现金权益缺少原始证据路径')
        path = inside(job, name)
        if path.relative_to(job.resolve()).as_posix() not in sealed['files']:
            raise ValueError('现金权益或覆盖回执的原始依据未封存')
        return path.relative_to(job.resolve()).as_posix()

    def iso(value):
        if not isinstance(value, str) or pd.Timestamp(value).date().isoformat() != value:
            raise ValueError('现金权益日期必须使用 YYYY-MM-DD')
        return value

    covered = defaultdict(list)
    for item in coverage:
        source = frozen_source(item.get('source'))
        first, last = iso(item.get('start')), iso(item.get('end'))
        if first > last or not isinstance(item.get('symbol'), str) or not item['symbol']:
            raise ValueError('现金权益覆盖区间或标的无效')
        covered[item['symbol']].append((first, last))
    for symbol, (first,last) in holdings['spans'].items():
        if not any(start <= first and end >= last for start,end in covered[symbol]):
            raise ValueError(f'{symbol} 持仓跨度缺完整现金权益来源回执')

    def session(day):
        idx = bisect_left(dates, day)
        return dates[idx] if idx<len(dates) else None

    expected = {}
    observed_actions = {}
    for item in actions:
        identity, symbol = item.get('id'), item.get('symbol')
        if not isinstance(identity,str) or not identity or not isinstance(symbol,str) or not symbol:
            raise ValueError('现金权益缺事件身份或标的')
        source = frozen_source(item.get('source'))
        record, ex, pay = (iso(item.get(key)) for key in ('record_date','ex_date','pay_date'))
        amount = item.get('cash_per_share')
        if (not record<ex<=pay or type(amount) not in (float,int)
                or not math.isfinite(amount) or amount<=0):
            raise ValueError(f'{identity} 缺有效现金分配口径')
        terms = (symbol,record,ex,pay,amount)
        if identity in observed_actions:
            if observed_actions[identity]!=terms:
                raise ValueError(f'{identity} 的权益条款存在冲突')
            for key in ('distribution','settlement'):
                if (identity,key) in expected:
                    expected[identity,key]['sources'].add(source)
            continue
        observed_actions[identity] = terms
        if dates[0]<=record<=dates[-1] and record not in holdings['on_date'] and symbol in holdings['spans']:
            raise ValueError(f'{identity} 的权益登记日缺持仓依据')
        qty = holdings['on_date'].get(record,{}).get(symbol,0)
        if qty<=0 or session(ex) is None:
            continue
        total = round(qty*amount,2)
        if total==0:
            continue
        for kind,raw_day in (('distribution',ex),('settlement',pay)):
            day = session(raw_day)
            if day is not None:
                expected[identity,kind] = dict(day=day,symbol=symbol,sources={source},total=total)

    for event in events.itertuples(index=False):
        if event.kind not in ('distribution','settlement'):
            continue
        identity = getattr(event,'action_id','')
        if pd.isna(identity):
            identity = ''
        options = [
            (key, action) for key,action in expected.items()
            if key[1]==event.kind and action['day']==event.date and action['symbol']==event.symbol
            and event.source in action['sources'] and (not identity or key[0]==identity)
        ]
        if len(options)>1:
            raise ValueError(f'{event.date} {event.symbol} 现金权益须提供唯一 action_id')
        if not options:
            raise AccountMismatch(f'{event.date} {event.symbol} 无对应权益、重复入账或结算日期错误')
        key, action = options[0]
        total = action['total']
        if event.kind=='distribution':
            payment = expected.get((key[0],'settlement'))
            valid = (agrees(event.cash_delta+event.receivable_delta,total)
                     and (not event.cash_delta or (payment and payment['day']==event.date
                          and agrees(event.cash_delta,total) and not event.receivable_delta)))
        else:
            valid = ((key[0],'distribution') not in expected
                     and agrees(event.cash_delta,total) and agrees(event.receivable_delta,-total))
        if not valid:
            raise AccountMismatch(f'{event.date} {event.symbol} 现金权益金额、确认或结算不符')
        del expected[key]
        if event.kind=='distribution' and event.cash_delta:
            expected.pop((key[0],'settlement'),None)
    if expected:
        identity, kind = next(iter(expected))
        raise AccountMismatch(f'{identity} 缺应有的现金权益流水 {kind}')


def table(path, columns, numbers=()):
    if path.suffix.lower()=='.parquet':
        frame = pd.read_parquet(path)
    elif path.suffix.lower()=='.csv':
        frame = pd.read_csv(path,dtype={'date':str,'symbol':str,'signal_date':str,'action_id':str},float_precision='round_trip')
    else:
        raise ValueError('核账原始表须为 CSV 或 Parquet')
    if set(columns)-set(frame):
        raise ValueError(f'{path.name} 缺核账列：{", ".join(sorted(set(columns)-set(frame)))}')
    if 'date' in columns:
        frame['date'] = pd.to_datetime(frame['date'].astype(str),format='mixed').dt.strftime('%Y-%m-%d')
    if 'symbol' in columns:
        frame['symbol'] = frame['symbol'].fillna('').astype(str).str.strip()
    for name in numbers:
        frame[name] = pd.to_numeric(frame[name],errors='raise')
        if not frame[name].map(math.isfinite).all():
            raise ValueError(f'{path.name} 含无效数值：{name}')
    return frame


def agrees(left,right,tolerance=0.011):
    return abs(left-right) <= tolerance


def check_charges(fills, path, money):
    rates = ('commission_rate','min_commission','tax_rate','other_rate','slippage_rate')
    schedule = table(path,('symbol','side','start','end',*rates),rates)
    # Some older markets charged the transfer component per share or by face value.
    extra = ('other_per_share','minimum_other')
    for field in extra:
        if field not in schedule:
            schedule[field] = 0.0
        else:
            schedule[field] = pd.to_numeric(schedule[field],errors='raise')
            if not schedule[field].map(math.isfinite).all():
                raise ValueError(f'冻结费率表含无效的 {field}')
    for name in ('start','end'):
        schedule[name] = pd.to_datetime(schedule[name].astype(str),format='mixed').dt.strftime('%Y-%m-%d')
    if (schedule[['start','end']].isna().any().any() or (schedule['start']>schedule['end']).any()
            or (schedule[list(rates+extra)]<0).any().any() or not schedule['side'].isin(('buy','sell')).all()):
        raise ValueError('冻结费率表的生效区间、方向或费率无效')
    by_symbol = {symbol:list(group.itertuples(index=False)) for symbol,group in schedule.groupby('symbol')}
    for fill in fills.itertuples(index=False):
        side = 'buy' if fill.quantity>0 else 'sell'
        def matches(symbol):
            return [rate for rate in by_symbol.get(symbol,())
                    if rate.side==side and rate.start<=fill.date<=rate.end]
        rules = matches(fill.symbol) or matches('*')
        if len(rules)!=1:
            raise ValueError(f'{fill.date} {fill.symbol} 没有唯一事前费用规则')
        rate = rules[0]
        quantity = abs(fill.quantity)
        amount = quantity*fill.price
        other = max(rate.minimum_other,amount*rate.other_rate+quantity*rate.other_per_share)
        expected = (max(rate.min_commission,amount*rate.commission_rate),amount*rate.tax_rate,
                    other,amount*rate.slippage_rate)
        if not all(agrees(getattr(fill,key),value) for key,value in zip(money,expected)):
            raise AccountMismatch(f'{fill.date} {fill.symbol} 实际扣费与冻结费率不一致')


def reconcile_account(job, protocol, daily, dates, ledger_path, cash_supplement=None):
    """Rebuild balances from transaction quantities, charges and corporate actions."""
    sealed = check_replay_seal(job,protocol)
    spec = read_json(inside(job,'account_evidence.json'))
    if not isinstance(spec,dict):
        raise ValueError('缺少结构化的成交与核账清单')
    keys = ('quotes','fills','positions','events','fee_schedule')
    if any(not isinstance(spec.get(key),str) or not spec[key].strip() for key in keys):
        raise ValueError('核账需要行情、成交、持仓、权益和冻结费率文件')
    paths = {key:inside(job,spec[key]) for key in keys}
    if len(set(paths.values()) | {ledger_path}) != 6:
        raise ValueError('行情、成交、持仓、权益流水、费率与净值须为独立表')
    for key in ('quotes','fee_schedule'):
        if paths[key].relative_to(job.resolve()).as_posix() not in sealed['files']:
            raise ValueError('逐日行情或费率未在正式回放前锁定')
    quote_fields = ('open','high','low','close','volume','upper','lower','suspended','buy_min','buy_step','t_plus')
    quotes = table(paths['quotes'],('date','symbol',*quote_fields),quote_fields)
    if quotes.duplicated(['date','symbol']).any() or quotes['symbol'].eq('').any():
        raise ValueError('估值行情的日期与标的有重复或缺失')
    money = ('commission','tax','other_fee','slippage')
    fill_numbers = ('seq','quantity','price',*money)
    fills = table(paths['fills'],('date','symbol','signal_date',*fill_numbers),fill_numbers)
    fills['signal_date'] = pd.to_datetime(fills['signal_date'].astype(str),format='mixed').dt.strftime('%Y-%m-%d')
    if (fills['signal_date'] > fills['date']).any():
        raise AccountMismatch('存在先成交、后形成信号的记录')
    if ((fills['signal_date'] == fills['date']).any() or fills['signal_date'].isna().any()
            or fills['symbol'].eq('').any() or fills['quantity'].eq(0).any()
            or (fills['price']<=0).any() or (fills[list(money)]<0).any().any()):
        raise ValueError('核账成交存在无标的、无有效信号或数据时点、价格、费用错误')
    check_charges(fills,paths['fee_schedule'],money)
    positions = table(paths['positions'],('date','symbol','quantity','mark'),('quantity','mark'))
    if (positions.duplicated(['date','symbol']).any() or positions['symbol'].eq('').any()
            or (positions[['quantity','mark']]<=0).any().any()):
        raise ValueError('持仓明细存在重复、负持仓或无效价格')
    event_numbers = ('seq','cash_delta','receivable_delta','quantity')
    events = table(paths['events'],('date','symbol','kind','source',*event_numbers),event_numbers)
    days = dates.strftime('%Y-%m-%d')
    for frame in (fills,positions,events):
        if not frame['date'].isin(days).all():
            raise ValueError('成交、持仓或账户事件跨出冻结交易日历')
    dq = quotes.set_index(['date','symbol'])
    actual = fills.join(dq,on=['date','symbol'])
    daily_volume_used = fills.assign(abs_quantity=fills['quantity'].abs()).groupby(['date','symbol'])['abs_quantity'].sum()
    volume = dq['volume'].reindex(daily_volume_used.index)
    if (actual[list(quote_fields)].isna().any().any() or volume.isna().any()
            or (actual['lower']<=0).any() or (actual['upper']<actual['lower']).any()):
        raise ValueError('成交行情、价格边界或日成交量证据不足')
    if (actual['suspended'].ne(0).any()
            or (actual['price'] < actual['lower']-1e-8).any() or (actual['price'] > actual['upper']+1e-8).any()
            or (actual['price'] < actual['low']-1e-8).any() or (actual['price'] > actual['high']+1e-8).any()
            or (daily_volume_used > volume+1e-8).any()):
        raise AccountMismatch('存在停牌、价格区间或全日真实成交量不能支持的成交')
    if 'amount' in dq:
        # Even if every remaining market share trades at the daily low/high,
        # the simulated participation must be consistent with total daily turnover.
        gross = (fills.assign(gross=fills['quantity'].abs()*fills['price'])
                 .groupby(['date','symbol'])['gross'].sum())
        observed = pd.to_numeric(dq['amount'].reindex(daily_volume_used.index),errors='coerce')
        # Precision is given in actual CNY and shares by the source adapter.
        amount_error = (pd.to_numeric(dq['amount_precision'].reindex(observed.index),errors='coerce')
                        if 'amount_precision' in dq else 0.01)
        volume_error = (pd.to_numeric(dq['volume_precision'].reindex(observed.index),errors='coerce')
                        if 'volume_precision' in dq else 0.0)
        low = dq['low'].reindex(observed.index)
        high = dq['high'].reindex(observed.index)
        residual = volume-daily_volume_used
        if (not observed.map(math.isfinite).all() or pd.Series(amount_error).lt(0).any()
                or pd.Series(volume_error).lt(0).any() or pd.Series(amount_error).isna().any()
                or pd.Series(volume_error).isna().any()):
            raise ValueError('日行情的成交额或发布精度缺失')
        if ((observed < gross+(residual-volume_error).clip(lower=0)*low-amount_error)
                | (observed > gross+(residual+volume_error)*high+amount_error)).any():
            raise AccountMismatch('实际日成交额、成交量和价格区间无法同时支持所报成交')
    buys = actual[actual['quantity']>0]
    if ((buys['buy_min']<=0).any() or (buys['buy_step']<=0).any() or
            (buys['quantity']<buys['buy_min']).any() or
            ((buys['quantity']-buys['buy_min'])%buys['buy_step']).gt(1e-8).any()
            or not actual['t_plus'].isin((0,1)).all()):
        raise ValueError('成交未满足最小申报数量或结算口径')
    check_fields = (spec.get('cash_column','cash'),spec.get('receivable_column','receivable'),
                    protocol['v1_preflight']['equity_column'])
    if len(set(check_fields))!=3 or any(name not in daily for name in check_fields):
        raise ValueError('主账本需独立列出现金、应收与权益')
    balances = daily[list(check_fields)].apply(pd.to_numeric,errors='raise').to_numpy(dtype=float)
    event_sources = {}
    for name in events['source'].astype(str).unique():
        path = inside(job,name)
        rel = path.relative_to(job.resolve()).as_posix()
        if not path.stat().st_size or rel not in sealed['files']:
            raise ValueError('权益事件缺落盘并事前锁定的原始依据')
        event_sources[rel] = sealed['files'][rel]
    posting = defaultdict(list)
    for row in actual.itertuples(index=False):
        charges = sum(getattr(row,name) for name in money)
        posting[row.date].append((row.seq,row.symbol,row.quantity,-row.quantity*row.price-charges,0.0,row.t_plus))
    for row in events.itertuples(index=False):
        if row.kind not in ('distribution','settlement','share_change','tax','liquidation'):
            raise ValueError('存在无法独立核对的外来现金或权益变更')
        if (row.quantity and not row.symbol
                or row.kind=='settlement' and (row.quantity or not agrees(row.cash_delta,-row.receivable_delta))
                or row.kind=='share_change' and (row.cash_delta or row.receivable_delta)
                or row.kind=='distribution' and (row.quantity or row.cash_delta<0 or row.receivable_delta<0)
                or row.kind=='liquidation' and (row.quantity>=0 or row.cash_delta<0 or row.receivable_delta<0)
                or row.kind=='tax' and (row.quantity or row.cash_delta>0 or row.receivable_delta)):
            raise ValueError('权益事件不符合其现金、应收和股数口径')
        posting[row.date].append((row.seq,row.symbol,row.quantity,row.cash_delta,row.receivable_delta,None))
    by_day = {day:group for day,group in positions.groupby('date')}
    marks_by_day = {day:group for day,group in quotes.groupby('date')}
    stock_actions = {(e.date,e.symbol) for e in events.itertuples(index=False)
                     if e.kind in ('distribution','share_change','liquidation')}
    marks = {}
    shares = defaultdict(float)
    cash = float(protocol['v1_preflight']['initial_capital'])
    receivable = 0.0
    holdings = dict(on_date={},spans={})
    carried_marks = 0
    for day,reported in zip(days,balances,strict=True):
        entries = posting[day]
        if (len({row[0] for row in entries})!=len(entries)
                or any(seq<0 or seq!=int(seq) for seq,*unused in entries)):
            raise ValueError(f'{day} 核账次序缺失或重复')
        sellable = dict(shares)
        for _,symbol,qty,paid,due,t_plus in sorted(entries):
            cash += paid
            receivable += due
            if qty:
                sellable_before = sellable.get(symbol,0.0)
                if t_plus==1 and qty<0 and sellable_before+qty < -1e-8:
                    raise AccountMismatch(f'{day} {symbol} 卖出超过 T+1 可卖数量')
                if t_plus!=1 or qty<0:
                    sellable[symbol] = sellable_before+qty
                shares[symbol] += qty
                if shares[symbol] < -1e-8:
                    raise AccountMismatch(f'{day} 出现超持仓卖出')
                if abs(shares[symbol])<=1e-8:
                    del shares[symbol]
            if cash < -0.011 or receivable < -0.011:
                raise AccountMismatch(f'{day} 成交流水或权益事件产生负可用现金或应收')
        day_quotes = marks_by_day.get(day)
        observed = {row.symbol:row for row in day_quotes.itertuples(index=False)} if day_quotes is not None else {}
        marks.update({symbol:row.close for symbol,row in observed.items() if row.close>0})
        held = by_day.get(day)
        recorded = {} if held is None else {row.symbol:(row.quantity,row.mark) for row in held.itertuples(index=False)}
        if shares.keys()!=recorded.keys():
            raise AccountMismatch(f'{day} 成交重算出的持仓名单与原账不同')
        market_value = 0.0
        holdings['on_date'][day] = dict(shares)
        for symbol,qty in shares.items():
            quantity,mark = recorded[symbol]
            quote = observed.get(symbol)
            if quote is not None and quote.suspended==1 and (day,symbol) in stock_actions:
                source = getattr(quote,'reference_source',None)
                if (not isinstance(source,str) or not source or
                        inside(job,source).relative_to(job.resolve()).as_posix() not in sealed['files']):
                    raise ValueError(f'{day} {symbol} 停牌跨权益事件缺封存的参考估值')
            if quote is None or quote.close<=0:
                if quote is None or quote.suspended!=1 or symbol not in marks or (day,symbol) in stock_actions:
                    raise ValueError(f'{day} {symbol} 持仓缺当日行情或可核实的停牌估值')
                carried_marks += 1
            first = holdings['spans'].get(symbol,(day,day))[0]
            holdings['spans'][symbol] = first,day
            if not agrees(qty,quantity,1e-7) or not agrees(mark,marks[symbol],1e-7):
                raise AccountMismatch(f'{day} {symbol} 持仓数量或原始行情估值对不上')
            market_value += qty*mark
        if not all(agrees(left,right) for left,right in zip((cash,receivable,cash+receivable+market_value),reported)):
            raise AccountMismatch(f'{day} 独立重算的现金、应收或净值与主账不符')
    verify_distributions(job,spec,sealed,list(days),holdings,events,cash_supplement)
    exposure = float((daily_volume_used/volume).max()) if len(daily_volume_used) else 0.0
    return dict(sessions=len(dates),fills=len(fills),events=len(events),max_daily_volume_fraction=exposure,
                carried_suspended_marks=carried_marks, cash_actions_verified=True,
                ledger_sha256=digest(ledger_path),inputs=sealed,
                book_files={name:digest(path) for name,path in paths.items()},event_sources=event_sources)
