"""SL/TP 验证影子 (L1, 2026-07-12) — record-only, 不改任何交易动作。

在 reprice 决定最终 SL/TP 的瞬间, 记录 "LLM 原始 SL/TP" 与 "代码波动率分桶算出的
SL/TP" 之间的分歧, 连同 regime/bucket/atr_pct/entry 一起落 JSONL, 供日后与 pnl_pct
联合校准 —— 回答 "分歧时该信 LLM 还是信代码"。

安全不变量: 纯记录。所有入口自带 try/except, 任何异常都吞掉, 绝不影响 reprice 或下单。
由 feature flag `sltp_verify_shadow` 控制 (默认 on, 可随时关)。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from app.llm.features.feature_flags import is_enabled
except Exception:  # pragma: no cover - 保险: flag 模块不可用时默认开启记录
    def is_enabled(_feature: str) -> bool:
        return True

# 分歧容差 (占 entry 的百分点): 差异 ≤ 容差视为 "一致"
SL_TOL_PCT = 0.2
TP_TOL_PCT = 0.3

# app/playbook/sltp_verify_shadow.py → parents[2] = 仓库根
_SHADOW_PATH = Path(__file__).resolve().parents[2] / "data" / "shadow" / "sltp_verify_shadow.jsonl"


def compute_sltp_divergence(entry, llm_sl, llm_tp, code_sl, code_tp) -> Dict[str, Any]:
    """纯函数: 计算 LLM 与代码 SL/TP 的分歧 (占 entry 百分点) 与是否一致。

    缺失 (0/None) 的一侧标记 *_missing, 对应 div/agree 置 None。
    """
    entry = float(entry or 0)
    llm_sl = float(llm_sl or 0)
    llm_tp = float(llm_tp or 0)
    code_sl = float(code_sl or 0)
    code_tp = float(code_tp or 0)
    out: Dict[str, Any] = {
        "llm_sl": round(llm_sl, 4), "llm_tp": round(llm_tp, 4),
        "code_sl": round(code_sl, 4), "code_tp": round(code_tp, 4),
        "llm_sl_missing": llm_sl <= 0, "llm_tp_missing": llm_tp <= 0,
        "sl_div_pct": None, "sl_agree": None,
        "tp_div_pct": None, "tp_agree": None,
    }
    if entry > 0 and llm_sl > 0 and code_sl > 0:
        out["sl_div_pct"] = round(abs(llm_sl - code_sl) / entry * 100, 3)
        out["sl_agree"] = out["sl_div_pct"] <= SL_TOL_PCT
    if entry > 0 and llm_tp > 0 and code_tp > 0:
        out["tp_div_pct"] = round(abs(llm_tp - code_tp) / entry * 100, 3)
        out["tp_agree"] = out["tp_div_pct"] <= TP_TOL_PCT
    return out


def _append_jsonl(record: Dict[str, Any]) -> None:
    """追加一行 JSONL (best-effort, 目录自动创建)。"""
    _SHADOW_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_SHADOW_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def record_sltp_shadow(*, inst_id=None, symbol="", direction="", entry=0.0,
                       llm_sl=0.0, llm_tp=0.0, code_sl=0.0, code_tp=0.0,
                       sl_pct=None, bucket="", atr_pct=None, regime="",
                       force: bool = False) -> Optional[Dict[str, Any]]:
    """记录一次 SL/TP 验证影子。record-only, 永不抛异常。

    返回落盘的 record dict; 未启用或异常时返回 None。
    """
    try:
        if not force and not is_enabled("sltp_verify_shadow"):
            return None
        rec: Dict[str, Any] = {
            "ts": datetime.utcnow().isoformat(),
            "inst_id": inst_id,
            "symbol": symbol,
            "direction": direction,
            "entry": round(float(entry or 0), 4),
            "sl_pct": round(float(sl_pct), 3) if sl_pct is not None else None,
            "bucket": bucket,
            "atr_pct": round(float(atr_pct), 5) if atr_pct is not None else None,
            "regime": regime,
        }
        rec.update(compute_sltp_divergence(entry, llm_sl, llm_tp, code_sl, code_tp))
        _append_jsonl(rec)
        return rec
    except Exception:
        return None
