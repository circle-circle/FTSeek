#!/usr/bin/env python3
"""P1-2 invalidation 盘中自动核销 (2026-08-23, DEB-20260823-phase1 知情破例)。

DA/UE 每条指令带 `invalidation` (一句话失效条件, 近30日覆盖 76%), 此前只 record-only
落库 + 退出侧影子 (app/playbook/invalidation_shadow.py), 入场侧完全不消费 ——
"thesis 已死但价格恰好回到触发带" 的单照样成交。本脚本盘中定时把已失效的未执行指令
标 skip_reason='TRIPWIRE_INVALIDATED', 下游 auto_execute 循环头检与
_check_intraday_trigger 头检自动跳过。

【设计说明与 DEB 原设计的偏差】
原设计假设指令带结构化 `tripwires` 字段 [{metric,threshold,deadline,source}] —— 实测
该字段不存在, 真实字段是自然语言 `invalidation`。故复用既有 invalidation_shadow 的
价格/百分比解析器, 外加本文件的合取/析取分解 + 时间限定词守卫。

判定语义 (保守, 宁漏不误杀):
  文本按 "或/or" 拆成析取组, 每组按 "且/and" 拆成合取子句;
  一组算命中 ⟺ 该组【全部】子句都机器可解析【且】全部命中;
  任一组命中 ⟹ 指令失效。
  含未解析子句的组 (如 "量能>200万股" / "公告被拒") 一律不命中 —— 合取条件无法验证
  就不能作废, 否则 "跌破X 且 放量" 会被价格单腿误杀。

时间限定词守卫 (防"条件窗口已过"误杀):
  - "收盘跌破 X": 收盘价口径, 仅在 ET ≥ CLOSE_QUALIFIER_HHMM 时才评估
  - "开盘后 N 分钟/小时 跌破 X": 窗口条件, 仅在 [开盘, 开盘+N] 内评估

用法:
    PYTHONPATH=. venv/bin/python scripts/tripwire_checker.py          # 实际核销
    PYTHONPATH=. venv/bin/python scripts/tripwire_checker.py --dry    # 只报不写
    PYTHONPATH=. venv/bin/python scripts/tripwire_checker.py --force  # 绕过盘中时段守卫
挂载: crontab 按北京时间挂宽窗口每小时一次, 脚本自守卫 ET 09:30-16:00 (DST 换季零改动)。
回滚: 删 crontab 行; 或 playbook.execution_parameters.tripwire_checker_enforce=false
      (仍落审计 jsonl, 不写库); 已核销行可 UPDATE skip_reason='' 恢复。
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

from app.utils.direction_utils import to_trade_direction  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env.us")
except Exception:
    pass

OUTPUT = ROOT / "data" / "tripwire_checker.jsonl"
SKIP_REASON = "TRIPWIRE_INVALIDATED"

# 收盘口径子句的生效时刻 (ET HHMM): 15:50 起视为"接近收盘", 之前不评估
CLOSE_QUALIFIER_HHMM = 1550
# 析取/合取分隔符 (中英文)
_OR_SPLIT = re.compile(r"或者|或|\bor\b", re.I)
_AND_SPLIT = re.compile(r"并且|且|\band\b", re.I)
# "开盘后 30 分钟" / "开盘后1小时" / "开盘 30 分钟内"
_WINDOW_PAT = re.compile(r"开盘后?\s*(\d+)\s*(分钟|小时|min|hour)", re.I)
_CLOSE_PAT = re.compile(r"收盘|收市|\bclose\b", re.I)


def _now_et():
    from app.utils.timezone import now_et
    return now_et()


def _in_regular_session(now_et_dt) -> bool:
    """ET 常规交易时段判定 (09:30-16:00)。

    自守卫而非靠 crontab 排时刻: 宿主时区是 Asia/Shanghai, 按北京时刻硬编码 ET 时段
    会在夏/冬令时切换时整体错 1 小时 (R-10/R-11 已被同类 cron DST 错时咬过)。
    crontab 挂宽窗口 + 本守卫收口 → 换季零改动。
    """
    _hm = now_et_dt.hour * 100 + now_et_dt.minute
    return 930 <= _hm <= 1600


def _minutes_since_open(now_et_dt) -> int:
    return (now_et_dt.hour * 60 + now_et_dt.minute) - (9 * 60 + 30)


def _clause_time_gate(clause: str, now_et_dt) -> bool:
    """时间限定词守卫: 该子句此刻是否可评估。"""
    if _CLOSE_PAT.search(clause):
        if now_et_dt.hour * 100 + now_et_dt.minute < CLOSE_QUALIFIER_HHMM:
            return False
    m = _WINDOW_PAT.search(clause)
    if m:
        n = int(m.group(1))
        span = n * 60 if m.group(2) in ("小时", "hour") else n
        if not (0 <= _minutes_since_open(now_et_dt) <= span):
            return False
    return True


def evaluate_invalidation_text(*, invalidation, direction, entry_price,
                               current_price, now_et_dt):
    """按析取/合取语义判定 invalidation 文本是否已失效。

    返回 (invalidated: bool, detail: str)。
    """
    from app.playbook.invalidation_shadow import evaluate_invalidation_shadow

    text = (invalidation or "").strip()
    if not text:
        return False, "no_invalidation"
    # 2026-08-24 词表修复: 库内指令词表是 BUY/SELL_SHORT/SKIP (无裸 "SHORT"),
    #   原 == "SHORT" 判定使 SELL_SHORT 全部落 LONG → 空头失效条件被反号评估.
    #   方向归一走 direction_utils SSOT (2026-08-23 UE 冷却门死门同款教训).
    dir_norm = to_trade_direction(direction) or "LONG"

    details = []
    for group in [g for g in _OR_SPLIT.split(text) if g and g.strip()]:
        clauses = [c for c in _AND_SPLIT.split(group) if c and c.strip()]
        group_hit = True
        group_notes = []
        for clause in clauses:
            if not _clause_time_gate(clause, now_et_dt):
                group_hit = False
                group_notes.append("time_gated")
                break
            r = evaluate_invalidation_shadow(
                invalidation=clause, direction=dir_norm,
                entry_price=entry_price, current_price=current_price)
            group_notes.append(r["reason"])
            if not (r["parseable"] and r["would_trigger"]):
                group_hit = False
                break
        details.append(f"[{'HIT' if group_hit else 'miss'}] " + " & ".join(group_notes))
        if group_hit:
            return True, " | ".join(details)
    return False, " | ".join(details) or "unparseable"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只报不写库")
    ap.add_argument("--force", action="store_true", help="绕过 ET 盘中时段守卫 (人工/回放)")
    args = ap.parse_args()

    from app.core.config_container import get_config
    from app.playbook.db import PlaybookDB
    from app.playbook.pipeline import INVALIDATED_SKIP_REASONS

    enforce = bool(get_config().get_dotted(
        "playbook.execution_parameters.tripwire_checker_enforce", True)) and not args.dry

    now_et_dt = _now_et()
    if not args.force and not _in_regular_session(now_et_dt):
        return 0  # cron 宽窗口挂载, 非盘中静默退出
    today = now_et_dt.strftime("%Y-%m-%d")
    db = PlaybookDB()
    pending = [
        i for i in db.get_instructions_by_date(today)
        # 2026-08-24 词表修复: 原过滤 ("BUY","SHORT") 里 "SHORT" 在库内不存在
        #   (真实做空方向是 SELL_SHORT) → 全部空头指令被静默排除,
        #   做空侧失效核销从未执行. 改走 direction_utils SSOT 判定可交易方向.
        if to_trade_direction(i.get("direction")) is not None
        and not i.get("executed")
        and (i.get("skip_reason") or "") not in INVALIDATED_SKIP_REASONS
        and (i.get("invalidation") or "").strip()
    ]
    print(f"=== tripwire 核销 {today} {now_et_dt.strftime('%H:%M')} ET · "
          f"待检 {len(pending)} 条 · enforce={enforce} ===")
    if not pending:
        return 0

    from scripts.llm_universe_scan import _batch_quotes, _build_router, _quote_price
    quotes = _batch_quotes(_build_router(), [i["symbol"] for i in pending])

    rows, n_inv = [], 0
    for inst in pending:
        symbol = inst["symbol"]
        price = _quote_price(quotes.get(symbol.replace("US.", "").upper()))
        if not price or float(price) <= 0:
            print(f"  [skip] {symbol} 无实时价")
            continue
        invalidated, detail = evaluate_invalidation_text(
            invalidation=inst.get("invalidation"),
            direction=inst.get("direction"),
            entry_price=float(inst.get("entry_price") or 0) or None,
            current_price=float(price),
            now_et_dt=now_et_dt,
        )
        rows.append({
            "ts": now_et_dt.isoformat(timespec="seconds"), "date": today, "tz": "ET",
            "inst_id": inst.get("id"), "symbol": symbol,
            "direction": inst.get("direction"),
            "entry_price": inst.get("entry_price"), "price": round(float(price), 4),
            "invalidation": inst.get("invalidation"),
            "invalidated": invalidated, "detail": detail, "enforced": bool(enforce and invalidated),
        })
        if not invalidated:
            continue
        n_inv += 1
        print(f"  [失效] {symbol} {inst.get('direction')} price=${float(price):.2f} → {detail}")
        if enforce:
            db.update_instruction_tracking(inst["id"], {"skip_reason": SKIP_REASON})

    if rows:
        with open(OUTPUT, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"  判定 {len(rows)} 条 · 失效 {n_inv} 条 · "
          f"{'已核销落库' if enforce else '未写库 (dry/enforce=false)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
