"""元标签影子契约测试 (DEB-20261031).

钉住三件事 (错了会让读数方向反掉):
  1. AUC 口径: 完美排序 = 1.0, 反向排序 = 0.0 (符号反了就分不清"有排序力"和"反指");
  2. 政策分档边界: p<0.40 弃 / 0.40-0.60 半仓 / >0.60 全仓 (阈值事先定, 测试防漂移);
  3. 负基线的 G3 方向: 基线为负时"少亏"必须判过, 不能用比值 (比值在负数区间反转)。
"""
import importlib.util
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "meta_label_shadow.py"


def _load():
    spec = importlib.util.spec_from_file_location("meta_label_shadow", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_auc_perfect_and_reversed():
    m = _load()
    y = np.array([0, 0, 1, 1], dtype=float)
    assert m.auc(y, np.array([0.1, 0.2, 0.8, 0.9])) == 1.0
    assert m.auc(y, np.array([0.9, 0.8, 0.2, 0.1])) == 0.0
    # 全同分 ⇒ 0.5 (无排序力)
    assert abs(m.auc(y, np.array([0.5, 0.5, 0.5, 0.5])) - 0.5) < 1e-9


def test_policy_tier_boundaries():
    m = _load()
    pnl = np.array([10.0, 10.0, 10.0, 10.0])
    prob = np.array([0.10, 0.39, 0.50, 0.61])       # 弃 / 弃 / 半仓 / 全仓
    pol = m.policy(pnl, prob)
    assert pol["P1_three_tier"]["n"] == 2, "0.39 应仍被弃, 0.50 半仓, 0.61 全仓"
    assert pol["P1_three_tier"]["net"] == 15.0, "10×0.5 (半仓) + 10×1.0 (全仓) = 15"
    assert pol["P1_three_tier"]["dropped_n"] == 2


def test_policy_three_tier_matches_pre_registered_thresholds():
    m = _load()
    assert (m.P_SKIP, m.P_FULL) == (0.40, 0.60), "阈值是预注册常量, 不得随手改 (改=阈值购物)"


def test_fit_predict_returns_probabilities():
    m = _load()
    rng = np.random.default_rng(0)
    X = rng.normal(size=(200, 3))
    y = (X[:, 0] + rng.normal(scale=0.5, size=200) > 0).astype(float)
    w, b = m.fit(X, y)
    p = m.predict(X, w, b)
    assert p.min() >= 0.0 and p.max() <= 1.0
    assert m.auc(y, p) > 0.6, "可分数据上学不出 >0.6 的 AUC ⇒ 梯度实现有 bug"


def test_negative_baseline_gate_uses_absolute_comparison():
    """基线为负时: 政策 −1072 vs 基线 −1315 = 少亏 ⇒ 必须判过 (比值法会误判成劣化)。"""
    base, pol_net = -1315.29, -1071.66
    ratio = pol_net / base
    assert ratio < 0.95, "比值法确实会判红 → 这就是要用绝对比较的原因"
    assert pol_net >= base, "绝对比较: 少亏 = 过门"