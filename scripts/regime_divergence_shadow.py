#!/usr/bin/env python3
"""regime 判定分歧率 shadow 对比器 (2026-07-26, 减法R2-5c 裁决产物) — 只读, 零 live 影响。

背景: 全仓现存 7 套 regime/市场状态判定模块 (playbook/market_regime 多数派 + 6 套),
"合一"派 vs "语义正交"派辩论无数据 → 修尺子先行: 每交易日取同一时刻各模块输出落一行,
攒 n≥10 交易日后按预注册判据裁决:
  - 分歧率 < 10% → 证明冗余 → 合一到 playbook/market_regime
  - 分歧率高    → 证明语义不同 → 动作改为重命名消歧 (regime_for_entry_gate 等), 不合一

用法: PYTHONPATH=. ./venv/bin/python scripts/regime_divergence_shadow.py [--push]
落盘: data/shadow/regime_divergence.jsonl (每日一行)
cron: 建议盘中 23:00 CST (ET 10-11am, 各模块数据最全) 每交易日一次。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "data" / "shadow" / "regime_divergence.jsonl"


def _safe(fn, tag):
    """每套模块独立 try — 单套失败不影响其他, 失败记 error 供健康观测."""
    try:
        v = fn()
        # 对象归一: 优先取 regime/name/state 属性, 避免落盘对象 repr (0x 地址无信息量)
        if v is not None and not isinstance(v, (str, int, float, bool, dict, list)):
            for attr in ("regime", "name", "state", "label", "value"):
                a = getattr(v, attr, None)
                if a is not None and isinstance(a, (str, int, float)) or hasattr(a, "name"):
                    v = getattr(a, "name", a)
                    break
            else:
                v = type(v).__name__ + "(repr不可读)"
        return {"value": str(v), "ok": True}
    except Exception as e:
        return {"value": None, "ok": False, "error": f"{type(e).__name__}: {e}"[:120]}


def collect() -> dict:
    out = {}

    # 1. playbook/market_regime (多数派, 6 调用方 — 合一候选目标)
    def _pb():
        from app.playbook.market_regime import MarketRegimeGate
        g = MarketRegimeGate()
        r = g.assess()
        return getattr(r, "regime", r if not hasattr(r, "regime") else r.regime)
    out["playbook_market_regime"] = _safe(_pb, "pb")

    # 2. analysis/market_state (SharedKernel VIX 源)
    def _ms():
        from app.analysis.market_state import MarketStateChecker
        c = MarketStateChecker()
        for attr in ("check", "get_state", "current_state"):
            f = getattr(c, attr, None)
            if callable(f):
                return f()
        return "no_api"
    out["analysis_market_state"] = _safe(_ms, "ms")

    # 3. strategy/regime_adapter (RegimeAdapter: SPY/VIX/宽度)
    def _ra():
        from app.strategy.regime_adapter import RegimeAdapter
        a = RegimeAdapter()
        for attr in ("detect", "current_regime", "get_regime"):
            f = getattr(a, attr, None)
            if callable(f):
                return f()
        return "no_api"
    out["strategy_regime_adapter"] = _safe(_ra, "ra")

    # 4. risk/market_regime_detector
    def _rd():
        from app.risk.market_regime_detector import MarketRegimeDetector
        d = MarketRegimeDetector()
        for attr in ("detect", "detect_regime", "get_regime"):
            f = getattr(d, attr, None)
            if callable(f):
                return f()
        return "no_api"
    out["risk_market_regime_detector"] = _safe(_rd, "rd")

    # 5-7. filter 类 (strategy/factors/risk 三套 regime_filter) — 过滤器语义,
    #   输出非 regime 标签而是通过/权重, 仅记录模块可用性 (语义正交的第一证据)
    for name, mod in (("strategy_regime_filter", "app.strategy.regime_filter"),
                      ("factors_regime_filter", "app.factors.regime_filter"),
                      ("risk_market_regime_filter", "app.risk.market_regime_filter")):
        def _imp(m=mod):
            import importlib
            importlib.import_module(m)
            return "importable(filter语义,不产regime标签)"
        out[name] = _safe(_imp, name)

    return out


def main() -> int:
    from app.utils.timezone import now_et
    snap = collect()
    # 分歧率口径: 产出 regime 标签的模块间 (1-4), 标签归一后两两比较
    labels = {}
    for k in ("playbook_market_regime", "analysis_market_state",
              "strategy_regime_adapter", "risk_market_regime_detector"):
        v = snap[k]
        if v.get("ok") and v.get("value") not in (None, "no_api"):
            # 归一: 提取最后一段大写词 (MarketRegime.CAUTIOUS → CAUTIOUS)
            s = str(v["value"]).upper()
            labels[k] = s.split(".")[-1].split()[0][:20]
    n = len(labels)
    uniq = len(set(labels.values())) if labels else 0
    rec = {
        "date": now_et().strftime("%Y-%m-%d"),
        "ts_et": now_et().isoformat(),
        "modules": snap,
        "labels_normalized": labels,
        "n_producing": n,
        "n_unique_labels": uniq,
        "all_agree": (uniq <= 1) if n >= 2 else None,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[regime-shadow] {rec['date']} 产出标签 {n} 套: {labels} | 唯一标签 {uniq} "
          f"| 一致={rec['all_agree']} → {OUT.relative_to(ROOT)}")
    # 判分: 攒 n≥10 交易日后算 all_agree 占比 = 一致率; 分歧率 = 1 - 一致率
    return 0


if __name__ == "__main__":
    rc = main()
    # 2026-07-30 (挂死修复): 采集链加载 futu 行情上下文, SDK network_manager/
    #   callback_executor/lv3 watchdog 均非 daemon — 主线程会在 threading._shutdown
    #   永久等待 (07-28/07-29 两实例各挂 20-44h 实证, 活已干完只是退不掉, 还白占
    #   内存+futu 连接)。落盘已完成, flush 后硬退出。
    import os
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)
