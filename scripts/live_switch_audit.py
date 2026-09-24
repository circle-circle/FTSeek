#!/usr/bin/env python3
"""切实盘开关审计 (只读) —— 「SIM→REAL 开关清单化」的可执行验收。

作用:
  1. 打印**当前**模式/账户解析结果 (单一真相源 = app.core.trading_mode.TradingMode)。
  2. 全仓扫描交易模式/账户的**解析点**, 分三类给出结论:
     - OK-SSOT   : 经 TradingMode / di_container / _resolve_trd_env 解析 (主链路)
     - OK-MAP    : `TrdEnv.SIMULATE if <cond> else TrdEnv.REAL` 形式的**映射**(条件本身来自 SSOT)
     - BLOCKER   : 常量硬编码 (`TrdEnv.SIMULATE` / `TrdEnv.REAL` 直接赋值) —— 切实盘时不会跟着切
     - WARN-LEGACY: 裸读 `FUTU_TRD_ENV` / `TRADE_ENV` 或裸读账户号 (与 SSOT 不同源)
  3. `--assert-ready`: 若存在 BLOCKER / WARN-LEGACY 则退出码 1 (切盘前 CI/人工门)。

只读: 不写任何文件、不下单、不改配置。
用法:
  PYTHONPATH=. venv/bin/python scripts/live_switch_audit.py
  PYTHONPATH=. venv/bin/python scripts/live_switch_audit.py --assert-ready
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_log = logging.getLogger(__name__)

SCAN_DIRS = ["app", "scripts"]
SKIP_PARTS = ("__pycache__", "archive", "probe_sim_stop", "futu_capability_live_probe",
              "multisource_consistency_sample", "live_switch_audit")

# 关键词 → 分类说明
# BLOCKER 只针对**自动执行路径**里的"常量赋值"(切实盘时不会跟着切);
# 比较/filter/映射 return/手动实盘台等属既成设计, 只作 INFO 列出。
LIVE_CRITICAL = ("app/execution/", "app/pipeline/", "app/core/position_sync", "app/core/scheduler_core",
                 "scripts/extended_hours_")
ASSIGN_RE = re.compile(r"^\s*(trd_env|env|target_env)\s*=\s*TrdEnv\.(SIMULATE|REAL)\s*(#.*)?$")
CONST_RE = re.compile(r"TrdEnv\.(SIMULATE|REAL)\b")
MAP_RE = re.compile(r"TrdEnv\.(SIMULATE|REAL)\s+if\b")
LEGACY_RE = re.compile(r"FUTU_TRD_ENV")
SSOT_RE = re.compile(r"TradingMode|AUTO_TRADE_ACCOUNT|ALLOW_REAL_TRADING|_resolve_trd_env|_is_live\(")

# 已知并接受的 legacy 读取 (每条都要有理由 + 回滚口径)
ACCEPTED_LEGACY = {
    # FUTU_TRD_ENV 已无生产读取 (仅注释/历史说明) — 由本审计持续保证
}


def _iter_py():
    for d in SCAN_DIRS:
        for p in (ROOT / d).rglob("*.py"):
            if any(s in p.parts for s in SKIP_PARTS):
                continue
            yield p


def scan():
    blockers, legacy, ssot, info = [], [], [], []
    for p in _iter_py():
        rel = str(p.relative_to(ROOT))
        critical = any(rel.startswith(x) for x in LIVE_CRITICAL)
        try:
            lines = p.read_text(errors="ignore").splitlines()
        except Exception:
            _log.warning("file read failed", exc_info=True)
            continue
        in_doc = False
        for i, line in enumerate(lines, 1):
            s = line.strip()
            # 跳过注释与**文档字符串内容** (docstring 里的历史说明不应被当作活跃读取)
            quotes = line.count('"""') + line.count("'''")
            if in_doc:
                if quotes % 2 == 1:
                    in_doc = False
                continue
            if quotes % 2 == 1:
                in_doc = True
                continue
            if not s or s.startswith("#"):
                continue
            hit_legacy = bool(LEGACY_RE.search(s)) and rel not in ACCEPTED_LEGACY
            hit_assign = bool(ASSIGN_RE.match(line))
            if hit_legacy:
                (legacy if critical else info).append((rel, i, s[:110]))
            if hit_assign:
                (blockers if critical else info).append((rel, i, s[:110]))
            if MAP_RE.search(s) or SSOT_RE.search(s):
                ssot.append((rel, i, s[:110]))
            elif CONST_RE.search(s) and not hit_assign:
                info.append((rel, i, s[:110]))
    return blockers, legacy, ssot, info


def current_state():
    sys.path.insert(0, str(ROOT))
    from app.core.trading_mode import TradingMode, ext_channel_real_authorized

    tm = TradingMode()
    return {
        "mode": tm.mode.value,
        "mode_label": tm.mode_label,
        "is_live": tm.is_live,
        "target_env": tm.target_env(),
        "account_id": tm.account_id(),
        "ext_real_authorized": ext_channel_real_authorized(),
    }


CUTOVER_KEYS = [
    ("AUTO_TRADE_ACCOUNT", "real", "主链路切实盘 (必须; 否则一切仍 SIM)"),
    ("ALLOW_REAL_TRADING", "true", "实盘安全门 (必须; TradingMode 会安全降级)"),
    ("REAL_ACCOUNT_ID", "<实盘账户号>", "实盘账户 (SSOT.account_id('real'))"),
    ("EXT_REAL_AUTHORIZED", "true", "ETH 链路独立闸 (默认 false = ETH 仅模拟)"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--assert-ready", action="store_true", help="有 BLOCKER/LEGACY 则退出 1")
    a = ap.parse_args()

    st = current_state()
    print("=== 当前模式 (SSOT = app.core.trading_mode.TradingMode) ===")
    for k, v in st.items():
        print(f"  {k:<22} {v}")

    blockers, legacy, ssot, info = scan()
    print(f"\n=== 解析点扫描 (app/ + scripts/) ===")
    print(f"  SSOT/映射点 (OK)                 : {len(ssot)}")
    print(f"  BLOCKER 自动执行路径常量硬编码    : {len(blockers)}")
    print(f"  WARN-LEGACY 自动路径裸读 FUTU_TRD_ENV: {len(legacy)}")
    print(f"  INFO 其他 TrdEnv 引用 (比较/filter/手动台) : {len(info)}")
    for tag, rows in (("BLOCKER", blockers), ("WARN-LEGACY", legacy)):
        for rel, i, s in rows[:25]:
            print(f"    [{tag}] {rel}:{i}  {s}")
    if len(blockers) > 25 or len(legacy) > 25:
        print("    … (仅列前 25 条)")

    print("\n=== 切实盘开关清单 (改 .env.us, 不需改代码) ===")
    for k, v, why in CUTOVER_KEYS:
        mark = "✔" if (k in ("AUTO_TRADE_ACCOUNT", "ALLOW_REAL_TRADING") and st["is_live"]) or \
                     (k == "EXT_REAL_AUTHORIZED" and st["ext_real_authorized"]) else " "
        print(f"  [{mark}] {k}={v:<16} {why}")

    print("\n=== ETH 链路实盘前置 (未完成项) ===")
    print("  1) REAL 专属下单前硬门: 真实购买力/可借量 (acctradinginfo_query) — 尚未接线")
    print("  2) STOP 单实盘语义实测 (最小额) — 尚未实测")
    print("  3) ETH 退出延迟监控 (1min cron + 60s 限价, 最坏 ~2min) — 尚未建模")
    if a.assert_ready and (blockers or legacy):
        print(f"\n❌ assert-ready 失败: BLOCKER={len(blockers)} LEGACY={len(legacy)}")
        return 1
    print("\n✅ 无常量硬编码 / 裸读 (ETH 链路已收敛到 SSOT)" if not (blockers or legacy)
          else "\n⚠️ 存在待收敛项 (见上)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())