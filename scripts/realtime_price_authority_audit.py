#!/usr/bin/env python3
"""实时价权威入口审计 (只读): 谁把"可能陈旧的快照字段"当实时价用。

背景 (为什么需要这把尺子)
    `get_market_snapshot.last_price` 在**盘前**是 T-1 收盘价 (用户 2026-09 亲纠), 实时价必须走
    LV3 订阅报价 / 券商。仓里已有一条正确的下单期链路 —
    `order_executor._get_current_price`: ①`broker_adapter.get_current_price` → ②
    `data_router.get_quote` 的 **bid/ask 中间价** → ③才降级 `price`/`last_price` 并打 WARNING —
    但**这条链路是否被所有决策点使用**, 从来没有一把尺子量过 (2026-09-20 数据面审计 P0-2:
    "两条路径并存, 没有单一实时价函数 + 断言")。

本审计做什么 (不做任何修改)
    用 `ast` 找出全仓**读取**价格字段 (`last_price` / `price`) 的位置, 给出读取上下文
    (所在文件与函数)、行号, 并按四类归档:

      AUTHORITY   处于已认可的实时价获取链里 (适配器/报价路由器/下单取价函数)
      GUARDED     附近有显式降级/警示 (含"降级"/warning/logger) —— 知情使用
      T1_OK       有 T-1/盘前 口径标注 (知道拿的是昨日收盘, 不是实时价)
      SUSPECT     **无任何说明**: 需人工判定是"实时价读取"还是"历史字段读取"

    判据 (留给裁决时用, 本脚本不自动判死): `SUSPECT` 面必须能被逐条解释。目标是
    "单一实时价入口" —— 若某处确需读快照字段, 就地补一行口径注释 (T-1/历史), 即可从 SUSPECT 出列。

用法: `python scripts/realtime_price_authority_audit.py [--json] [--path app/]`
"""
from __future__ import annotations

import argparse
import ast
import json
import logging
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_log = logging.getLogger(__name__)

PRICE_FIELDS = {"last_price", "price", "price_val", "last_traded_price"}
# 已认可的实时价获取链 (出现即算 AUTHORITY): 文件 → 函数名集合 (空集 = 该文件整体算权威层)
AUTHORITY = {
    "app/execution/broker_adapter.py": set(),
    "app/data/providers/futu_lv3_provider.py": set(),
    "app/data/market_data_router.py": set(),
}
AUTHORITY_FUNCS = {"_get_current_price", "_resolve_price", "get_current_price", "get_quote"}
GUARD_MARKERS = ("降级", "warning", "logger.warn", "degrad", "fallback")
T1_MARKERS = ("T-1", "昨收", "prev_close", "previous_close", "盘前", "历史", "非实时", "上一交易日")
# 成交价/订单价读取 (成交回执、订单明细) —— 本来就不是"实时价"语义, 不属待查面
FILL_TOKENS = ("order", "deal", "fill", "trade", "trim_info", "_md.get")
# 券商快照/K线读取 —— 真正的 T-1 风险面 (见 vix_source 的口径说明)
#   注意: 只认**券商侧变量名**; 本仓自己造的 `snapshot`/`facts` 字典 (含规范化 price 字段) 不算,
#   否则会把 100+ 条规范化读取误报成"T-1 风险" (2026-09-21 首版实测: 111 条 SUSPECT 里 104 条是噪声)。
SNAPSHOT_TOKENS = ("snap", "iloc", "kline", "history_kline")
VERIFIED: dict[str, str] = {
    # 逐条读码后的裁决 (2026-09-21)。有裁决的站点不再进"未判定"面 — 工具从此只报**新增**的无口径读取。
    "app/data/vix_source.py": "口径已声明 (docstring: 盘中用实时报价 / 盘前盘前价 / 盘后与周末回上一交易日收盘, 与 FRED 日频同日) — 非缺陷",
    "app/data/futu_lv3_provider.py": "连接探针 (_ensure_connected 只为校验能取到数), 不参与任何决策 — 非缺陷",
    "app/core/obsidian_writer.py": "读的是 analysis['snapshot'] 本仓自造字典, 非券商快照 — 非缺陷",
    "app/playbook/candidate_eligibility.py": "读的是 facts (自造规范化记录, 且带 quote_age_sec ⇒ 另有 90s 新鲜度门, 缺字段即拒绝) — 非缺陷",
    "app/analysis/market_state.py": "SPY 日线级 regime (last_price vs MA200): 盘前取到的是上一交易日收盘, 语义成立但**未就地注释** — 建议补一行口径说明",
    "app/playbook/premarket_invalidation.py": "实测相位 09:35–09:47 ET 用盘中价 (396 条全在 09 时), 与判据文字'盘前口径增量'不一致 — **待定谳**",
}
SKIP_DIRS = {"__pycache__", "venv", ".git", "tests"}


def _enclosing(tree: ast.AST) -> dict[int, str]:
    """行号 → 所属函数名 (最内层)。"""
    out: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                ln = getattr(sub, "lineno", None)
                if ln is not None:
                    out[ln] = node.name
    return out


def _price_reads(tree: ast.AST) -> list[tuple[int, str]]:
    """找 `x["last_price"]` / `x.get("last_price")` 这类读取 (返回 (行号, 字段))。"""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            sl = node.slice
            val = getattr(sl, "value", sl)
            if isinstance(val, ast.Constant) and val.value in PRICE_FIELDS:
                hits.append((node.lineno, str(val.value)))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value in PRICE_FIELDS:
                hits.append((node.lineno, str(node.args[0].value)))
    return hits


def audit(base: Path) -> list[dict]:
    rows: list[dict] = []
    for py in sorted(base.rglob("*.py")):
        if any(part in SKIP_DIRS for part in py.parts):
            continue
        rel = str(py.relative_to(base.parent))
        try:
            src = py.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src)
        except SyntaxError:
            _log.warning("syntax error", exc_info=True)
            continue
        lines = src.splitlines()
        fmap = _enclosing(tree)
        auth_file = rel in AUTHORITY
        for lineno, field in _price_reads(tree):
            fname = fmap.get(lineno, "")
            window = "\n".join(lines[max(0, lineno - 7):lineno + 3])
            code = (lines[lineno - 1] if 0 < lineno <= len(lines) else "")
            code_low = code.lower()
            win_low = window.lower()
            if auth_file or fname in AUTHORITY_FUNCS:
                cls = "AUTHORITY"
            elif any(t in code_low for t in SNAPSHOT_TOKENS):
                # 代码行本身就在读券商快照/K线/DataFrame ⇒ 真正的 T-1 风险面
                if any(m in win_low for m in T1_MARKERS):
                    cls = "T1_OK"
                elif rel in VERIFIED:
                    cls = "VERIFIED"       # 已逐条读码裁决 ⇒ 不再进"未判定"面
                else:
                    cls = "SNAPSHOT_SUSPECT"
            elif any(m in win_low for m in GUARD_MARKERS):
                cls = "GUARDED"
            elif any(t in code_low for t in FILL_TOKENS):
                cls = "FILL"          # 成交/订单价: 非"实时价"语义
            elif any(m in win_low for m in T1_MARKERS):
                cls = "T1_OK"
            else:
                cls = "NORMALIZED"    # 读的是本仓规范化 context/quote 字段 (price), 不是券商快照字段
            rows.append({"file": rel, "line": lineno, "field": field,
                         "func": fname, "class": cls,
                         "text": (lines[lineno - 1].strip()[:110] if 0 < lineno <= len(lines) else "")})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--path", default="app/", help="扫描根 (默认 app/)")
    a = ap.parse_args()
    base = (REPO / a.path).resolve()
    rows = audit(base)
    if a.json:
        print(json.dumps({"total": len(rows), "rows": rows}, ensure_ascii=False, indent=1))
        return 0
    by = {}
    for r in rows:
        by.setdefault(r["class"], []).append(r)
    print("═══ 实时价权威入口审计 (只读) ═══")
    print(f"扫描根: {a.path} | 价格字段读取点: {len(rows)}")
    for k in ("AUTHORITY", "GUARDED", "T1_OK", "FILL", "NORMALIZED", "VERIFIED", "SNAPSHOT_SUSPECT"):
        print(f"  {k:17s} {len(by.get(k, []))}")
    if by.get("VERIFIED"):
        print("\n── VERIFIED (已逐条读码裁决, 记录在案) ──")
        for rel_, why in VERIFIED.items():
            n = len([r for r in by["VERIFIED"] if r["file"] == rel_])
            print(f"  {rel_}  ({n} 处) — {why}")
    if by.get("SNAPSHOT_SUSPECT"):
        print("\n── SNAPSHOT_SUSPECT (在读券商快照/K线字段且无 T-1 口径说明 ⇒ 需逐条判定) ──")
        for r in by["SNAPSHOT_SUSPECT"]:
            print(f"  {r['file']}:{r['line']} [{r['func'] or '-'}] .{r['field']}  | {r['text']}")
    for k in ("GUARDED", "T1_OK"):
        if by.get(k):
            print(f"\n── {k} ({len(by[k])}) ──")
            for r in by[k]:
                print(f"  {r['file']}:{r['line']} [{r['func'] or '-'}] .{r['field']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())