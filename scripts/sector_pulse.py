#!/usr/bin/env python3
"""盘中板块脉搏探针 (2026-07-28, 板块级联批 — 用户问"开盘后是否有板块/主题维度输入")。

背景: 07-27 芯片/存储大跌日系统满舵做多 — 盘前轮动数据 (deep_sector_rotation) 有人
生成没人吃, 盘中板块维度输入为零。本探针补盘中一环: 11 个 SPDR 板块 ETF + SMH(半导体)
的当日实时涨跌 → data/sector_pulse.json (原子写), 每 30min 刷新。

消费方 (防"没人吃"重蹈覆辙, 上线即两个):
  1. llm_universe_scan._gather_market_corroboration → UE 材料 [市场佐证] 盘中脉搏行
  2. 数据积累供 #3 盘中事件重推理 (未裁决) 的判据设计
零 LLM / 走 MarketDataRouter (Futu 实时价, ETF 已在 P1 订阅) / 失败静默降级留旧文件。
cron (ET 盘中拆双段窗, CST×ET 家族教训):
  */30 22-23 * * 1-5  (CST 周一至五晚 = ET 盘中前半场)
  */30 0-3 * * 2-6    (CST 周二至六凌晨 = ET 盘中后半场)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("sector_pulse")

from app.utils.timezone import now_et  # noqa: E402

OUT = REPO / "data" / "sector_pulse.json"

# SPDR 11 板块 + SMH 半导体 (07-27 教训: 芯片板块需独立粒度)
SECTOR_ETFS = {
    "US.XLK": "Technology", "US.SMH": "Semis", "US.XLF": "Financials",
    "US.XLE": "Energy", "US.XLV": "Healthcare", "US.XLI": "Industrials",
    "US.XLY": "ConsDiscretionary", "US.XLP": "ConsStaples", "US.XLC": "Communication",
    "US.XLU": "Utilities", "US.XLB": "Materials", "US.XLRE": "RealEstate",
}


def main() -> int:
    from app.data.market_data_router import MarketDataRouter
    router = MarketDataRouter()
    changes = {}
    for etf, name in SECTOR_ETFS.items():
        try:
            q = router.get_quote(etf) or {}
            # router 真实字段: change_percent (现成) / price + previous_close (兜底)
            cp = q.get("change_percent")
            if cp is None:
                px = float(q.get("price", 0) or 0)
                prev = float(q.get("previous_close", 0) or 0)
                cp = (px - prev) / prev * 100 if (px > 0 and prev > 0) else None
            if cp is not None:
                changes[name] = round(float(cp), 2)
        except Exception as e:
            logger.warning(f"{etf} 取价失败: {e}")
    if len(changes) < 6:  # 半数以上失败 → 不覆盖旧文件 (宁旧勿假)
        logger.warning(f"仅 {len(changes)}/12 成功, 保留旧 pulse 不写")
        return 1
    ranked = sorted(changes.items(), key=lambda x: x[1])
    result = {
        "date": now_et().strftime("%Y-%m-%d"),
        "asof_et": now_et().strftime("%H:%M"),
        "changes": changes,
        # ranked: 弱→强, 供 LLM 直读 "Semis(-2.1%)" 形式
        "ranked": [f"{k}({v:+.1f}%)" for k, v in ranked],
        "weakest3": [k for k, _ in ranked[:3]],
        "strongest3": [k for k, _ in ranked[-3:]],
    }
    # 原子写 (脚本集惯例 os.replace)
    fd, tmp = tempfile.mkstemp(dir=str(OUT.parent), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    os.replace(tmp, str(OUT))
    print(f"[sector_pulse] {result['asof_et']} ET | 弱: {', '.join(result['ranked'][:3])} "
          f"| 强: {', '.join(result['ranked'][-3:])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
