"""期权质量守门模块（纯函数，可单测）

P0-4 解决 FTSeek-US 期权选择的四个核心漏洞：

1. **IV rank 守门**：买方在高 IV 环境自动输 vega。
   52 周 IV 百分位 > max_iv_rank（默认 70）直接拒绝买 put/call。
   做卖方（covered call / cash-secured put）反之：< min_iv_rank 拒绝。

2. **Bid-Ask Spread% 硬门**：期权点差 > cap（默认 5%）拒绝。
   与 cost_model 保持一致。

3. **OI/Volume 流动性门**：
   - open_interest < min_oi（默认 100）拒绝
   - volume < min_volume（默认 10）拒绝
   避免临近到期的合约"进去出不来"。

4. **真实 Theta/Premium 门**：
   用实时 theta/last_price（若有），否则用保守经验估算。
   阈值默认 4%/天（放宽 selector.py 原本过于严格的 3%）。

模块严格无状态，便于回测和单测。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("ftseek.options_quality_gate")


# ============================================================
#  配置 / 结果数据类
# ============================================================

@dataclass
class QualityGateConfig:
    # IV rank 守门
    iv_rank_enabled: bool = True
    max_iv_rank_buyer: float = 70.0     # 买方：IV rank > 70 拒绝（高波动环境）
    min_iv_rank_seller: float = 30.0    # 卖方：IV rank < 30 拒绝（低波动环境）

    # Spread% 守门
    spread_pct_enabled: bool = True
    max_spread_pct: float = 0.05        # 5%

    # 流动性守门
    liquidity_enabled: bool = True
    min_open_interest: int = 100
    min_volume: int = 10

    # Theta 守门（每日时间衰减占期权价格的比例）
    theta_enabled: bool = True
    max_theta_premium_ratio: float = 0.04  # 4%/天

    # DTE 守门（过短的合约 theta 过激）
    dte_enabled: bool = True
    min_dte: int = 5
    max_dte: int = 45

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "QualityGateConfig":
        if not d:
            return cls()
        return cls(
            iv_rank_enabled=bool(d.get("iv_rank_enabled", True)),
            max_iv_rank_buyer=float(d.get("max_iv_rank_buyer", 70.0)),
            min_iv_rank_seller=float(d.get("min_iv_rank_seller", 30.0)),
            spread_pct_enabled=bool(d.get("spread_pct_enabled", True)),
            max_spread_pct=float(d.get("max_spread_pct", 0.05)),
            liquidity_enabled=bool(d.get("liquidity_enabled", True)),
            min_open_interest=int(d.get("min_open_interest", 100)),
            min_volume=int(d.get("min_volume", 10)),
            theta_enabled=bool(d.get("theta_enabled", True)),
            max_theta_premium_ratio=float(d.get("max_theta_premium_ratio", 0.04)),
            dte_enabled=bool(d.get("dte_enabled", True)),
            min_dte=int(d.get("min_dte", 5)),
            max_dte=int(d.get("max_dte", 45)),
        )


@dataclass
class GateResult:
    passed: bool
    reject_code: str = ""        # IV_RANK / SPREAD / OI / VOLUME / THETA / DTE
    reject_reason: str = ""
    scores: Dict[str, float] = field(default_factory=dict)  # 审计明细

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "reject_code": self.reject_code,
            "reject_reason": self.reject_reason,
            "scores": {k: round(v, 4) for k, v in self.scores.items()},
        }


# ============================================================
#  核心纯函数
# ============================================================

def compute_iv_rank(current_iv: float, iv_history: Sequence[float]) -> float:
    """52 周 IV rank（百分位）。

    IV rank = (current_iv - min) / (max - min) × 100
    返回 [0, 100]；history 为空时退化为 IV percentile 估算：
      - current_iv > 0.60 → rank 75（保守认为偏高）
      - current_iv > 0.40 → rank 50（中性）
      - current_iv > 0.25 → rank 35
      - 其他         → rank 25（偏低）
    """
    if current_iv <= 0:
        return 50.0
    clean = [float(x) for x in iv_history if x and x > 0]
    if len(clean) < 5:
        # 无历史数据时，用当前 IV 绝对值保守估算 rank
        if current_iv > 0.60:
            return 75.0
        elif current_iv > 0.40:
            return 50.0
        elif current_iv > 0.25:
            return 35.0
        else:
            return 25.0
    lo, hi = min(clean), max(clean)
    if hi <= lo:
        return 50.0
    rank = (current_iv - lo) / (hi - lo) * 100.0
    return max(0.0, min(100.0, rank))


def compute_iv_percentile(current_iv: float, iv_history: Sequence[float]) -> float:
    """IV percentile：历史序列中低于 current_iv 的比例 × 100"""
    if current_iv <= 0 or not iv_history:
        return 50.0
    clean = [float(x) for x in iv_history if x and x > 0]
    if len(clean) < 5:
        return 50.0
    below = sum(1 for x in clean if x < current_iv)
    return below / len(clean) * 100.0


def compute_spread_pct(bid: float, ask: float, last: float = 0.0) -> float:
    """Bid-Ask spread 占 mid 价的比例；mid 非法时返回 1.0（强制拒绝）"""
    if bid <= 0 or ask <= 0 or ask < bid:
        return 1.0
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return 1.0
    return (ask - bid) / mid


# ============================================================
#  守门器
# ============================================================

class OptionsQualityGate:
    """期权合约四道门守卫。

    用法：
        gate = OptionsQualityGate(QualityGateConfig())
        result = gate.check(contract_dict, side="BUY",
                            iv_history=[...], dte=14)
        if not result.passed:
            # 降级到下一个候选合约
            ...
    """

    def __init__(self, config: Optional[QualityGateConfig] = None):
        self.cfg = config or QualityGateConfig()

    def check(
        self,
        contract: Dict[str, Any],
        *,
        side: str = "BUY",
        iv_history: Optional[Sequence[float]] = None,
        dte: Optional[int] = None,
    ) -> GateResult:
        """
        contract 字段（dict 或 dataclass 转 dict）：
            - bid / ask / last_price
            - iv 或 implied_volatility
            - open_interest（可选）
            - volume（可选）
            - theta（可选）
            - dte（可选，若未在 contract 中则用 kwarg）
        """
        cfg = self.cfg
        scores: Dict[str, float] = {}

        # ========== 1. DTE 门 ==========
        d = dte if dte is not None else int(contract.get("dte", 0) or 0)
        scores["dte"] = float(d)
        if cfg.dte_enabled:
            if d < cfg.min_dte:
                return GateResult(
                    passed=False, reject_code="DTE",
                    reject_reason=f"DTE {d} < min {cfg.min_dte}（theta 衰减过激）",
                    scores=scores,
                )
            if d > cfg.max_dte:
                return GateResult(
                    passed=False, reject_code="DTE",
                    reject_reason=f"DTE {d} > max {cfg.max_dte}（占用资金过久）",
                    scores=scores,
                )

        # ========== 2. Spread% 门 ==========
        bid = float(contract.get("bid", 0) or 0)
        ask = float(contract.get("ask", 0) or 0)
        last = float(contract.get("last_price", 0) or 0)
        spread_pct = compute_spread_pct(bid, ask, last)
        scores["spread_pct"] = spread_pct
        if cfg.spread_pct_enabled and spread_pct > cfg.max_spread_pct:
            return GateResult(
                passed=False, reject_code="SPREAD",
                reject_reason=f"spread {spread_pct*100:.2f}% > cap {cfg.max_spread_pct*100:.1f}%",
                scores=scores,
            )

        # ========== 3. 流动性门（OI / Volume） ==========
        oi = int(contract.get("open_interest", 0) or 0)
        vol = int(contract.get("volume", 0) or 0)
        scores["open_interest"] = float(oi)
        scores["volume"] = float(vol)
        if cfg.liquidity_enabled:
            # OI / Volume 任一字段缺失（==0）则放过（避免富途未返字段时的误杀）
            if oi > 0 and oi < cfg.min_open_interest:
                return GateResult(
                    passed=False, reject_code="OI",
                    reject_reason=f"open_interest {oi} < {cfg.min_open_interest}",
                    scores=scores,
                )
            if vol > 0 and vol < cfg.min_volume:
                return GateResult(
                    passed=False, reject_code="VOLUME",
                    reject_reason=f"volume {vol} < {cfg.min_volume}",
                    scores=scores,
                )

        # ========== 4. IV rank 门 ==========
        current_iv = float(contract.get("iv", contract.get("implied_volatility", 0)) or 0)
        scores["iv"] = current_iv
        if cfg.iv_rank_enabled and iv_history:
            iv_rank = compute_iv_rank(current_iv, iv_history)
            scores["iv_rank"] = iv_rank
            side_upper = str(side).upper()
            # 买方（LONG CALL / LONG PUT）：高 IV 拒绝
            if side_upper in ("BUY", "LONG", "LONG_PUT", "LONG_CALL"):
                if iv_rank > cfg.max_iv_rank_buyer:
                    return GateResult(
                        passed=False, reject_code="IV_RANK",
                        reject_reason=(
                            f"IV rank {iv_rank:.1f} > {cfg.max_iv_rank_buyer}"
                            f"（买方在高 IV 环境大概率输 vega）"
                        ),
                        scores=scores,
                    )
            # 卖方（SHORT CALL/PUT / COVERED CALL / CASH_SECURED_PUT）：低 IV 拒绝
            elif side_upper in ("SELL", "SHORT", "WRITE", "COVERED_CALL", "CASH_SECURED_PUT"):
                if iv_rank < cfg.min_iv_rank_seller:
                    return GateResult(
                        passed=False, reject_code="IV_RANK",
                        reject_reason=(
                            f"IV rank {iv_rank:.1f} < {cfg.min_iv_rank_seller}"
                            f"（卖方在低 IV 环境收权利金不足以覆盖尾部风险）"
                        ),
                        scores=scores,
                    )

        # ========== 5. Theta/Premium 门 ==========
        theta = contract.get("theta", None)
        mid_price = last if last > 0 else ((bid + ask) / 2 if (bid > 0 and ask > 0) else 0.0)
        if cfg.theta_enabled and mid_price > 0:
            if theta is not None and theta != 0:
                # 有真实 theta：|theta| / price
                theta_ratio = abs(float(theta)) / mid_price
            elif current_iv > 0 and d > 0:
                # 降级估算：ATM Put theta ≈ -S*σ / (2*sqrt(T))，假定 S≈strike≈mid 无意义，
                # 用经验近似：theta_ratio ≈ 0.5 / sqrt(DTE) × IV（更激进时衰减更快）
                theta_ratio = (0.5 / max(1.0, math.sqrt(d))) * min(1.0, current_iv)
            else:
                theta_ratio = 0.0
            scores["theta_premium_ratio"] = theta_ratio
            if theta_ratio > cfg.max_theta_premium_ratio:
                return GateResult(
                    passed=False, reject_code="THETA",
                    reject_reason=(
                        f"theta/premium {theta_ratio*100:.2f}%/天 > "
                        f"{cfg.max_theta_premium_ratio*100:.1f}%/天"
                    ),
                    scores=scores,
                )

        # 全部通过
        return GateResult(passed=True, scores=scores)


# ============================================================
#  self-test
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    cfg = QualityGateConfig()
    gate = OptionsQualityGate(cfg)

    # 模拟 52 周 IV 历史：最小 20%，最大 80%，当前 30%
    iv_history = [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]

    # case1 正常合约（IV rank 中等、spread 窄、OI 充足）→ 通过
    good = {
        "bid": 2.45, "ask": 2.55, "last_price": 2.50,
        "iv": 0.30, "open_interest": 500, "volume": 80, "theta": -0.03, "dte": 14,
    }
    r1 = gate.check(good, side="BUY", iv_history=iv_history, dte=14)
    print("[case1 good  ]", r1.to_dict())
    assert r1.passed, "优质合约应通过"

    # case2 IV rank 高（当前 75%，rank ≈ 91.7% > 70）→ IV_RANK 拒绝
    high_iv = dict(good, iv=0.75)
    r2 = gate.check(high_iv, side="BUY", iv_history=iv_history, dte=14)
    print("[case2 highIV]", r2.to_dict())
    assert not r2.passed and r2.reject_code == "IV_RANK"

    # case3 Spread 过宽（bid 2.3 / ask 2.7 → 16.7% > 5%）→ SPREAD 拒绝
    wide = dict(good, bid=2.3, ask=2.7)
    r3 = gate.check(wide, side="BUY", iv_history=iv_history, dte=14)
    print("[case3 wide  ]", r3.to_dict())
    assert not r3.passed and r3.reject_code == "SPREAD"

    # case4 OI 不足（50 < 100）→ OI 拒绝
    low_oi = dict(good, open_interest=50)
    r4 = gate.check(low_oi, side="BUY", iv_history=iv_history, dte=14)
    print("[case4 lowOI ]", r4.to_dict())
    assert not r4.passed and r4.reject_code == "OI"

    # case5 Volume 不足（5 < 10）→ VOLUME 拒绝
    low_vol = dict(good, volume=5)
    r5 = gate.check(low_vol, side="BUY", iv_history=iv_history, dte=14)
    print("[case5 lowVol]", r5.to_dict())
    assert not r5.passed and r5.reject_code == "VOLUME"

    # case6 Theta 过激（真实 theta=-0.20 / price=2.5 → 8%/天 > 4%/天）→ THETA 拒绝
    fast_decay = dict(good, theta=-0.20)
    r6 = gate.check(fast_decay, side="BUY", iv_history=iv_history, dte=14)
    print("[case6 theta ]", r6.to_dict())
    assert not r6.passed and r6.reject_code == "THETA"

    # case7 DTE 过短（3 < 5）→ DTE 拒绝
    r7 = gate.check(good, side="BUY", iv_history=iv_history, dte=3)
    print("[case7 shortD]", r7.to_dict())
    assert not r7.passed and r7.reject_code == "DTE"

    # case8 卖方 + 低 IV（当前 22%，rank ≈ 3.3% < 30）→ IV_RANK 拒绝
    low_iv = dict(good, iv=0.22)
    r8 = gate.check(low_iv, side="SELL", iv_history=iv_history, dte=14)
    print("[case8 sellLow]", r8.to_dict())
    assert not r8.passed and r8.reject_code == "IV_RANK"

    # case9 卖方 + 中等 IV（当前 50%）→ 通过
    mid_iv = dict(good, iv=0.50)
    r9 = gate.check(mid_iv, side="SELL", iv_history=iv_history, dte=14)
    print("[case9 sellMid]", r9.to_dict())
    assert r9.passed

    # case10 IV 历史缺失（退化为中性，直接放过）
    r10 = gate.check(good, side="BUY", iv_history=[], dte=14)
    print("[case10 noHist]", r10.to_dict())
    assert r10.passed

    # case 11 - 对照：case1 IV rank 验证
    iv_rank_c1 = compute_iv_rank(0.30, iv_history)
    iv_pct_c1 = compute_iv_percentile(0.30, iv_history)
    print(f"[check] IV=0.30 → rank={iv_rank_c1:.2f} pct={iv_pct_c1:.2f}")
    assert 15 < iv_rank_c1 < 20, "IV 0.30 在 [0.2,0.8] 区间 rank ≈ 16.67"

    print("\noptions_quality_gate self-test 全部通过")
