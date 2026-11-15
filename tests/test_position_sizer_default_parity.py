"""仓位分层 fallback 与 YAML SSOT 的漂移守卫 (2026-11-15 根因批)。

病灶: unified_config.yaml `risk.capital_safety.tiers.medium.max_position_pct` 在
DEB-20260815 P0-A 从 0.25 收紧到 0.08 (实盘 MU 单名 86% 集中 + SNDK 扛单 -$65.9k
的教训), 但 `app/risk/position_sizer.DEFAULT_TIERS` 这份**降级兜底**没同步 ⇒
YAML 读取失败时单名帽被静默放宽 3 倍。本用例把两侧钉在一起: 只要有人改了一边而
不改另一边, 立即变红。

为什么守的是"值快照"而不是"能算": 降级路径没有别的可观测出口 —— 只有在 YAML
不可用时才生效, 平时永远不跑, 任何"跑通了"的测试都覆盖不到它。
"""
from pathlib import Path

import pytest
import yaml

from app.risk.position_sizer import DEFAULT_TIERS

ROOT = Path(__file__).resolve().parents[1]
YAML_PATH = ROOT / "config" / "unified_config.yaml"

_TIER_KEYS = ("min_equity", "max_equity", "risk_pct", "total_pct",
              "max_positions", "min_trade_usd", "max_position_pct")


def _yaml_tiers() -> dict:
    data = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    tiers = data["risk"]["capital_safety"]["tiers"]
    assert set(tiers) == set(DEFAULT_TIERS), "分层名两边必须一致"
    return tiers


def test_default_tiers_mirror_yaml_ssot():
    """除 inst/large 等量级差异外, 每个键都必须逐字等于 YAML 现役值。"""
    tiers = _yaml_tiers()
    drift = []
    for name, cfg in DEFAULT_TIERS.items():
        for key in _TIER_KEYS:
            want = tiers[name].get(key)
            got = cfg.get(key)
            if want != got:
                drift.append(f"{name}.{key}: DEFAULT_TIERS={got!r} vs yaml={want!r}")
    assert not drift, (
        "DEFAULT_TIERS 与 unified_config.yaml risk.capital_safety.tiers 漂移 "
        "(降级路径会用代码侧值, 必须与现役一致):\n" + "\n".join(drift))


def test_medium_single_name_cap_is_tight():
    """承重点: medium 桶单名帽 = 0.08 (DEB-20260815), 不是老值 0.25。"""
    assert DEFAULT_TIERS["medium"]["max_position_pct"] == pytest.approx(0.08)


def test_missing_key_falls_back_to_conservative_cap():
    """tier 字典缺 max_position_pct 时不得放宽到 0.25 (原缺陷), 取 0.08。"""
    from app.risk.position_sizer import calc_position_size

    tiers = {name: dict(cfg) for name, cfg in DEFAULT_TIERS.items()}
    tiers["medium"].pop("max_position_pct")
    result = calc_position_size(
        equity=120_000.0,
        buying_power=120_000.0,
        entry_price=100.0,
        stop_loss_price=97.0,
        tiers=tiers,
        drawdown_pct=0.0,
    )
    # 0.08 * 120000 / 100 = 96 股上限
    assert result.shares <= 96, f"缺键时单名帽被放宽: {result.shares} 股"


def test_atr_multiplier_flip_is_sizing_invariant():
    """预注册判据的静态版: 现配置帽结构下 4×ATR 与 2×ATR 给出**相同股数**。

    依据 (2026-11-15 实算): 股数 = min(风险驱动, 分层总额 20%, 单名 8%, 单笔 5%),
    最紧的是单笔 5%。风险驱动要低于它需 ATR% × 倍数 > 60% (倍数4 ⇒ ATR%>15%,
    倍数2 ⇒ ATR%>30%); 库内 400 笔有 ATR 的成交 ATR% 中位 4.99% / max 7.03%
    ⇒ 0/400 受影响。所以 ATR 倍数 4.0→2.0 是**口径统一**, 不是尺寸变更。

    这条断言一旦变红, 说明有人在"帽结构/风险比例"上动了刀子 ——
    那才是真正会改变仓位的地方, 必须重新预注册而非顺手放过。
    """
    from app.risk.position_sizer import DEFAULT_GUARDS, calc_position_size

    equity, price, atr = 82_000.0, 100.0, 5.0  # ATR% = 5% (库内中位数)
    common = dict(equity=equity, buying_power=equity, entry_price=price,
                  drawdown_pct=0.0, guards=DEFAULT_GUARDS)
    four = calc_position_size(stop_loss_price=price - atr * 4, **common)
    two = calc_position_size(stop_loss_price=price - atr * 2, **common)
    assert four.shares == two.shares, (four.shares, two.shares)
    # 两种倍数下都由"单笔 5%"截断 (82000×5%/100 = 41 股), 风险驱动分别是 123 / 246 股
    assert four.shares == 41 and two.shares == 41