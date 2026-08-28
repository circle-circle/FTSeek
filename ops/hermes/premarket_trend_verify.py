#!/usr/bin/env python3
"""盘前趋势验证 (no_agent cron script) — v0.64 盘前异动策略的攒样器.

回答两个问题 (B类收益验证):
  1. 延续率: 盘前 |chg|≥4% 的异动股, 当日方向延续盘前方向的比例 (趋势信号质量)
  2. 假设收益: 开盘顺盘前方向入场 → 收盘平仓的影子收益 (不承诺实盘, 仅测信号)

外加 A类避损核查:
  3. DA 指令影子结局: entry band 是否触及, 悲观口径 SL/TP/close 结局
  4. thesis_drift_invalidate 的指令: 若未作废今天会怎样 (量化避损价值)

数据源:
  - data/opportunity_scan_{date}.json: candidates + premarket_movers (v0.64 起落盘)
  - data/ftseek_us.db playbook_instructions: entry band / skip_reason
  - Futu 日K: open/high/low/close (via venv python subprocess)

输出语义 (no_agent): 非交易日/无数据 → 空输出静默; 交易日 → 报告文本.
落盘: data/premarket_trend_outcomes.jsonl (按 date 幂等: 重跑先删当日行).
"""
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone, timedelta

FTSEEK = '/opt/ftseek/FTSeek-US'
DB_PATH = f'{FTSEEK}/data/ftseek_us.db'
DATA_DIR = f'{FTSEEK}/data'
OUT_JSONL = f'{DATA_DIR}/premarket_trend_outcomes.jsonl'
VENV_PYTHON = f'{FTSEEK}/venv/bin/python3.11'
TRADING_DAY_CHECK = f'{FTSEEK}/scripts/trading_day_check.py'
KNIFE = 0.03   # 与 app/playbook/entry_band.py 同源
CHASE = 0.05

# 8/27 时钟统一: 硬编码 -4/月份DST猜测 → zoneinfo 真实 ET (DST 边界精确);
# 极端环境落固定偏移兜底 (无 DST 感知, 与旧行为一致)
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = timezone(timedelta(hours=-4))  # 兜底


def et_now():
    return datetime.now(ET)


def is_trading_day(date_str):
    try:
        r = subprocess.run(
            [VENV_PYTHON, TRADING_DAY_CHECK, '--market', 'US'],
            capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return True  # 检查失败不静默吞掉报告


def fetch_daily_bars(symbols, date_str):
    """单日日K (open/high/low/close/volume) + 真昨收 — venv futu, 单进程批量.

    [口径修复 2026-08-29] 不再信 scan 文件的 prev_close (62% 是两天前收盘, 见
    2026-08-29 审计): 拉 date 前 14 天窗口, 取 date 之前最后一根日K收盘作真昨收。
    """
    if not symbols:
        return {}
    try:
        _dt = datetime.strptime(date_str, '%Y-%m-%d')
        _start = (_dt - timedelta(days=14)).strftime('%Y-%m-%d')
    except Exception:
        _start = date_str
    code = f"""
import json, sys, time
from futu import OpenQuoteContext, KLType, RET_OK
syms = sys.argv[1].split(',')
date = sys.argv[2]
start = sys.argv[3]
ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
out = {{}}
for i, s in enumerate(syms):
    if i > 0:
        time.sleep(1.2)  # [2026-08-29] 历史K线配额限流 (~30次/30s), 批量回填必加
    try:
        ret, data, _ = ctx.request_history_kline(s, start=start, end=date, ktype=KLType.K_DAY, max_count=12)
        if ret == RET_OK and data is not None and len(data) > 0:
            row = data.iloc[-1]
            prev = 0.0
            for i in range(len(data) - 2, -1, -1):
                if str(data.iloc[i]['time_key'])[:10] < date:
                    prev = float(data.iloc[i]['close'])
                    break
            out[s] = {{'open': float(row['open']), 'high': float(row['high']),
                       'low': float(row['low']), 'close': float(row['close']),
                       'volume': float(row.get('volume', 0)), 'prev_close': prev}}
    except Exception:
        pass
ctx.close()
print(json.dumps(out))
"""
    try:
        r = subprocess.run([VENV_PYTHON, '-c', code, ','.join(symbols), date_str, _start],
                           capture_output=True, text=True, timeout=300)
        if r.returncode == 0 and r.stdout.strip():
            # futu 断连日志可能在 JSON 之后打到 stdout — 逐行找可解析的 JSON
            for ln in reversed(r.stdout.strip().splitlines()):
                ln = ln.strip()
                if ln.startswith('{'):
                    try:
                        return json.loads(ln)
                    except Exception:
                        continue
    except Exception as e:
        print(f'[warn] daily bar fetch failed: {e}', file=sys.stderr)
    return {}


def load_scan(date_str):
    path = f'{DATA_DIR}/opportunity_scan_{date_str}.json'
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def load_instructions(date_str):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT symbol, direction, entry_price, stop_loss, take_profit, "
        "entry_low, entry_high, skip_reason, triggered, decision_id, confidence "
        "FROM playbook_instructions WHERE date=?", (date_str,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def shadow_outcome(inst, bar):
    """悲观口径影子结局: band触及→入场; 同日 low 触 SL 优先于 high 触 TP."""
    d = inst['direction']
    e_low, e_high = inst.get('entry_low'), inst.get('entry_high')
    if not e_low or not e_high or not bar:
        return None
    o, h, l, c = bar['open'], bar['high'], bar['low'], bar['close']
    if l > e_high or h < e_low:
        return {'triggered': False}  # 全天未触及 band
    entry = min(max(o, e_low), e_high)  # 开盘在band内→open; 否则band边缘
    sl, tp = inst.get('stop_loss'), inst.get('take_profit')
    if d == 'BUY':
        if sl and l <= sl:
            exit_p, why = sl, 'SL'
        elif tp and h >= tp:
            exit_p, why = tp, 'TP'
        else:
            exit_p, why = c, 'CLOSE'
        pnl = (exit_p - entry) / entry * 100
    else:  # SELL_SHORT
        if sl and h >= sl:
            exit_p, why = sl, 'SL'
        elif tp and l <= tp:
            exit_p, why = tp, 'TP'
        else:
            exit_p, why = c, 'CLOSE'
        pnl = (entry - exit_p) / entry * 100
    return {'triggered': True, 'entry': round(entry, 3), 'exit': round(exit_p, 3),
            'exit_why': why, 'pnl_pct': round(pnl, 3)}


def main():
    backfill_date = sys.argv[1] if len(sys.argv) > 1 else None  # --date 回填: YYYY-MM-DD
    now = et_now()
    date_str = backfill_date or now.strftime('%Y-%m-%d')
    if not backfill_date and (now.weekday() >= 5 or not is_trading_day(date_str)):
        return
    scan = load_scan(date_str)
    insts = load_instructions(date_str)
    if not scan and not insts:
        return

    candidates = (scan or {}).get('candidates', [])
    movers = (scan or {}).get('premarket_movers', [])
    symbols = sorted({c.get('symbol') for c in candidates if c.get('symbol')}
                     | {m.get('symbol') for m in movers if m.get('symbol')}
                     | {i['symbol'] for i in insts})
    bars = fetch_daily_bars(symbols, date_str)
    if not bars:
        return

    # ── ① DA 指令影子结局 ──
    inst_rows, lines = [], []
    trig_n, trig_pnl = 0, 0.0
    for i in insts:
        if i['direction'] not in ('BUY', 'SELL_SHORT'):
            continue
        bar = bars.get(i['symbol'])
        so = shadow_outcome(i, bar)
        rec = {'date': date_str, 'type': 'instruction', 'symbol': i['symbol'],
               'direction': i['direction'], 'skip_reason': i.get('skip_reason'),
               'shadow': so, 'bar': bar}
        inst_rows.append(rec)
        if i.get('skip_reason'):
            tag = '作废' if 'drift' in str(i['skip_reason']) else '跳过'
            if so and so['triggered']:
                lines.append(f"  ⚠️ {i['symbol']} 被{tag}但band今日触及 — 若执行影子结局 "
                             f"{so['exit_why']} {so['pnl_pct']:+.2f}% (避损核查)")
            else:
                lines.append(f"  ✅ {i['symbol']} 被{tag}, 今日未触band — 作废无机会成本")
        elif so:
            if so['triggered']:
                trig_n += 1
                trig_pnl += so['pnl_pct']
                lines.append(f"  {'🟢' if so['pnl_pct']>=0 else '🔴'} {i['symbol']} {i['direction']} "
                             f"触band@{so['entry']} → {so['exit_why']} {so['pnl_pct']:+.2f}%")
            else:
                lines.append(f"  ⚪ {i['symbol']} {i['direction']} 未触band (全天未入)")

    # ── ② 盘前异动延续率 (B类) ──
    mover_lines, mover_recs = [], []
    cont_n, cont_hit, trend_ret_sum = 0, 0, 0.0
    inst_syms = {i['symbol'] for i in insts if not i.get('skip_reason')}
    for m in movers:
        sym = m.get('symbol')
        bar = bars.get(sym)
        # [口径修复 2026-08-29] 真昨收优先取日K自算值 (bar['prev_close']);
        #   scan 文件的 m['prev_close'] 仅作兜底 (历史 62% stale)。
        prev_close = (bar or {}).get('prev_close') or m.get('prev_close') or 0
        if not bar or prev_close <= 0:
            continue
        pm_chg = m.get('premarket_chg_pct') or 0
        # [口径修复 2026-08-29] 有盘前价时按真昨收重算涨跌幅 (双保险; 历史审计
        #   显示 chg 字段本身对真昨收计算正确, 但源头字段已证伪 62% 不可信)。
        if m.get('premarket_price'):
            pm_chg = (m['premarket_price'] / prev_close - 1) * 100
        day_chg = (bar['close'] - prev_close) / prev_close * 100
        cont = (pm_chg > 0) == (day_chg > 0)
        direction = 1 if pm_chg > 0 else -1
        trend_ret = direction * (bar['close'] - bar['open']) / bar['open'] * 100 if bar['open'] > 0 else 0.0
        cont_n += 1
        cont_hit += int(cont)
        trend_ret_sum += trend_ret
        in_da = '📋DA' if sym in inst_syms else '    '
        mover_lines.append(f"  {'↗' if pm_chg>0 else '↘'} {sym} 盘前{pm_chg:+.1f}% → 当日{day_chg:+.1f}% "
                           f"{'延续✓' if cont else '反转✗'} | 顺势影子 {trend_ret:+.2f}% {in_da}")
        mover_recs.append({'date': date_str, 'type': 'mover', 'symbol': sym,
                           'pm_chg_pct': pm_chg, 'day_chg_pct': round(day_chg, 3),
                           'continued': cont, 'trend_shadow_ret': round(trend_ret, 3),
                           'in_da_instructions': sym in inst_syms})

    # ── ③ 落盘 (幂等: 删当日行再写) ──
    keep = []
    if os.path.exists(OUT_JSONL):
        with open(OUT_JSONL) as f:
            for ln in f:
                try:
                    if json.loads(ln).get('date') != date_str:
                        keep.append(ln)
                except Exception:
                    pass
    with open(OUT_JSONL, 'w') as f:
        f.writelines(keep)
        for r in inst_rows + mover_recs:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + '\n')

    # ── ④ 累计统计 ──
    cum_cont_n, cum_cont_hit, cum_ret, cum_ret_n = 0, 0, 0.0, 0
    try:
        with open(OUT_JSONL) as f:
            for ln in f:
                r = json.loads(ln)
                if r.get('type') == 'mover':
                    cum_cont_n += 1
                    cum_cont_hit += int(r.get('continued'))
                    cum_ret += r.get('trend_shadow_ret', 0)
                    cum_ret_n += 1
    except Exception:
        pass

    # ── 报告 ──
    out = [f"📈 盘前趋势验证 [{date_str}]"]
    if movers:
        rate = cont_hit / cont_n * 100 if cont_n else 0
        avg = trend_ret_sum / cont_n if cont_n else 0
        out.append(f"\n盘前异动 (|盘前涨跌|≥4%): {len(movers)}只, 有K线 {cont_n}只")
        out.append(f"延续率 {cont_hit}/{cont_n} = {rate:.0f}% | 顺势影子均收益 {avg:+.2f}%/笔 (开盘→收盘)")
        out.extend(mover_lines[:12])
    else:
        out.append("\n盘前异动: 今日 bridge 无 premarket_movers 记录 (v0.64 前生成的文件)")
    if lines:
        out.append(f"\nDA指令影子结局: {len([i for i in insts if i['direction'] in ('BUY','SELL_SHORT')])}条, "
                   f"触band {trig_n}条" + (f", 影子合计 {trig_pnl:+.2f}%" if trig_n else ""))
        out.extend(lines[:15])
    if cum_cont_n:
        out.append(f"\n累计 (n={cum_cont_n}): 延续率 {cum_cont_hit/cum_cont_n*100:.0f}% | "
                   f"顺势影子均 {cum_ret/cum_ret_n:+.2f}%/笔 — n≥30 后再评估是否转实盘信号")
    print('\n'.join(out))


if __name__ == '__main__':
    main()
