"""scripts/ic_weekly_report.py 信噪比修复批钉子测试 (2026-10-11).

背景: 用户问「请核查是否有改进的问题 提升信噪比」。实测发现五处会**误导决策**的缺陷,
本文件把每处的修复钉住, 防回归。全部打桩, 不碰真实 DB、不推飞书。

五处缺陷与实测依据 (2026-10-11, 只读实测):
  ① 建议门不看 p 值/不看持续性 ⇒ deep_analysis 近30天 IC=-0.0700 **p=0.214 不显著**
     却输出「🔴 持续负IC, 建议禁用或降权」; 而分窗复算 31-60天前 = **+0.0695**(符号相反)、
     全史 **+0.0446** (n=1221) ⇒ 「持续」与数据相反, 建议禁用一个全史 IC 为正的策略。
     同时**唯一显著信号被漏报**: unknown 全史 IC=-0.1433 **p=0.000** (n=1429) 因近30天
     n=0 永不进 by_strategy。
  ② 明细表 emoji 阈值 ±2bps ≪ 费用线 ~10bps ⇒ 25 格里 11 格被打标, 但「过 n≥30 且过
     费用线」的 = **0 格**; 5 个 ✅ 是误导性绿勾 (最极端 universe_expansion/LONG/mid
     n=7 5d=+0.042% WR=86% → 扣费后为负)。
  ③ 明细表**无 trade_date 过滤** ⇒ 用全史 4929 行(2026-05-14→10-08), 而同页 IC 段用
     近30天 470 行, 相差 **10.5×** 且未标注; 抽 6 格复算 **2 格符号翻转**。
  ④ 影子组合只凭净额符号下「该松/保持」⇒ band n=16 净+172.15 判「🔴 该松」, 但
     **Top3 占净额 80.1%**、16 笔散在 6 个交易日 ⇒ 违反「集中度>50% 不得作落地依据」。
  ⑤ KR1.3 已**饱和 164×** (filled=4929/目标30, 首笔达标日=数据起点 2026-05-14) ⇒
     每周输出「✅ 已达标(超出4899笔)」占报告开头却零信息量。

回滚: 删本文件即可, 零生产代码依赖。
"""
import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "ic_weekly_report_snr", PROJECT_ROOT / "scripts" / "ic_weekly_report.py"
)
iwr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iwr)

# 修复后必须消失的旧措辞 / 必须出现的新措辞
_OLD_DISABLE = "持续负IC, 建议禁用或降权"
_VERDICT_LOOSEN = "拦掉的是钱 (该松)"      # 注意: 不能只断言 "该松" —
_VERDICT_KEEP = "拦掉的是亏 (保持)"        # 拒绝文案「不给「该松/保持」结论」里也含该子串


# ── 共用桩 ────────────────────────────────────────────────────────────────
def _ic_stub(by_strategy=None, by_direction=None, distinguishable=False):
    return {
        "status": "ok",
        "ic_mean": -0.0270,
        "ic_p_value": 0.5945,
        "n_signals": 470,
        "n_independent": 368,
        "ic_independent": -0.0359,
        "day_clustered": {
            "day_ic_mean": -0.0245, "t": -0.43, "mde": 0.1575,
            "distinguishable": distinguishable, "n_days": 20,
        },
        "by_strategy": by_strategy if by_strategy is not None else {
            "deep_analysis": {"ic": -0.0700, "pval": 0.214, "n": 317},
            "universe_expansion": {"ic": -0.0033, "pval": 0.978, "n": 73},
        },
        "by_direction": by_direction if by_direction is not None else {
            "LONG": {"ic": -0.0170, "n": 289},
            "SHORT": {"ic": -0.0557, "n": 101},
        },
    }


class _FakeConn:
    """最小 sqlite3 替身: execute() 返回自身且**可迭代** (生产代码用列表推导直接迭代游标)。"""

    def __init__(self, rows):
        self._rows = list(rows)

    def execute(self, *a, **k):
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def patched(monkeypatch):
    """打桩全部数据源 + 飞书, 返回可覆写的句柄 dict。"""
    import app.notifier.feishu as feishu
    h = {
        "ic": _ic_stub(),
        "stats": [],
        "okr": {"total_signals": 4951, "filled": 4929, "fill_rate": 99.6,
                "week_signals": 104, "week_filled": 89,
                "okr_gap_text": "STUB_OKR", "okr_gap": 0,
                "okr_target_30": "KR1.3: 需≥30笔有outcome"},
        "shadow": ([], []),
        "win_ic": {},
        "neutral": 80,
    }
    monkeypatch.setattr(iwr, "get_ic_summary", lambda: h["ic"])
    monkeypatch.setattr(iwr, "get_signal_stats", lambda *a, **k: h["stats"])
    monkeypatch.setattr(iwr, "get_okr_progress", lambda: h["okr"])
    monkeypatch.setattr(iwr, "_read_shadow_portfolio_summary", lambda: h["shadow"])
    monkeypatch.setattr(iwr, "_count_neutral_signals", lambda: h["neutral"])
    monkeypatch.setattr(iwr, "_window_ic",
                        lambda **kw: h["win_ic"].get(kw.get("strategy") or kw.get("direction"),
                                                     {"ic": None, "pval": None, "n": 0}))
    # 补报段会开 DB 取 DISTINCT strategy_id — 默认给空, 需要时单测自行覆写
    monkeypatch.setattr(iwr.sqlite3, "connect", lambda *a, **k: _FakeConn([]))
    monkeypatch.setattr(feishu, "send_text", lambda *a, **k: True)
    return h


def _stat(**kw):
    """明细表单格的最小合法结构 (缺字段补默认, 避免每处测试重复写全)。"""
    d = {"strategy": "s", "direction": "LONG", "bucket": "strong(50+)", "n": 40,
         "avg_1d": 0.0, "avg_5d": 0.0, "wr": 50.0, "hist_n": 40,
         "hist_5d": 0.0, "sign_flip": False}
    d.update(kw)
    return d


# ── ① 建议门: 不显著不得给「建议禁用」 ────────────────────────────────────
def test_insignificant_ic_must_not_suggest_disable(patched):
    """p=0.214 不显著 ⇒ 禁出「建议禁用或降权」(修复前必出)。"""
    out = iwr.generate_report()
    assert _OLD_DISABLE not in out and "建议禁用或降权" not in out
    assert "p=0.214 不显著" in out and "不构成建议" in out


def test_headline_indistinguishable_blocks_all_verdicts(patched):
    """头条日聚类判不可分辨 ⇒ 必须显式声明本窗无资格产出禁用建议。"""
    out = iwr.generate_report()
    assert "无资格" in out and "不可分辨" in out


def test_dual_window_sign_flip_blocks_persistent_claim(patched, monkeypatch):
    """前窗反号 ⇒ 不得称「持续」, 且须报全史读数。

    实测依据: deep_analysis 近30天 IC=-0.0700, 31-60天前 **+0.0695**(符号相反),
    全史 **+0.0446** (n=1221)。本例把近30天 p 设为显著(0.01), 才能验到「反号」这道闸 —
    否则先被显著性闸拦下, 测不到本条。
    """
    monkeypatch.setitem(patched["ic"]["by_strategy"], "deep_analysis",
                        {"ic": -0.0700, "pval": 0.01, "n": 317})
    monkeypatch.delitem(patched["ic"]["by_strategy"], "universe_expansion")

    def _fake(**kw):
        """按 (标的, 窗口) 分派 — 不能用递增计数器: 方向段与建议段会交叉调用。"""
        name = kw.get("strategy") or kw.get("direction")
        if name != "deep_analysis":
            return {"ic": None, "pval": None, "n": 0}
        if kw.get("days_back", 30) > 1000:                       # 全史
            return {"ic": +0.0446, "pval": 0.119, "n": 1221}
        if kw.get("offset_days", 0):                             # 31-60天前 → 反号
            return {"ic": +0.0695, "pval": 0.02, "n": 312}
        return {"ic": -0.0700, "pval": 0.01, "n": 317}           # 近30天

    monkeypatch.setattr(iwr, "_window_ic", _fake)
    out = iwr.generate_report()
    assert "建议禁用或降权" not in out, "前窗反号仍建议禁用 = 回归"
    assert "前窗反号" in out and "禁提禁用/降权" in out
    assert "+0.0695" in out and "+0.0446" in out


def test_dual_window_same_sign_and_significant_still_acts(patched, monkeypatch):
    """正向守卫: 双窗同号且均显著 ⇒ 仍须给出建议 (防修成「什么都不建议」)。"""
    monkeypatch.setitem(patched["ic"]["by_strategy"], "deep_analysis",
                        {"ic": -0.0700, "pval": 0.01, "n": 317})
    monkeypatch.delitem(patched["ic"]["by_strategy"], "universe_expansion")

    def _fake(**kw):
        if (kw.get("strategy") or kw.get("direction")) != "deep_analysis":
            return {"ic": None, "pval": None, "n": 0}
        if kw.get("days_back", 30) > 1000:
            return {"ic": -0.0600, "pval": 0.01, "n": 1221}
        return {"ic": -0.0700 if not kw.get("offset_days") else -0.0650,
                "pval": 0.01, "n": 315}

    monkeypatch.setattr(iwr, "_window_ic", _fake)
    out = iwr.generate_report()
    assert "双窗同号且均显著" in out and "建议禁用或降权" in out


def test_full_history_significant_but_recent_zero_is_reported(patched, monkeypatch):
    """近30天 n=0 但全史显著 ⇒ 必须补报 (修复前静默漏掉 unknown IC=-0.1433 p=0.000)。"""
    monkeypatch.setattr(iwr.sqlite3, "connect",
                        lambda *a, **k: _FakeConn([("unknown",), ("voting_llm",)]))
    seq = {"unknown": {"ic": -0.1433, "pval": 0.0001, "n": 1429},
           "voting_llm": {"ic": +0.1013, "pval": 0.008, "n": 679}}
    monkeypatch.setattr(iwr, "_window_ic",
                        lambda **kw: seq.get(kw.get("strategy"),
                                             {"ic": None, "pval": None, "n": 0}))
    out = iwr.generate_report()
    assert "修复前此类被静默漏报" in out
    assert "-0.1433" in out and "+0.1013" in out


# ── ② emoji 阈值 = 费用线, 不是 2bps ─────────────────────────────────────
def test_below_fee_line_never_gets_green_check(patched):
    """5d=+0.042%(4.2bps < 10bps 费用线) ⇒ 必须 ⚪, 不得 ✅。"""
    patched["stats"] = [_stat(strategy="universe_expansion", direction="LONG",
                              bucket="mid(30-50)", n=7, avg_1d=0.02, avg_5d=0.042,
                              wr=86.0, hist_n=7, hist_5d=0.042)]
    out = iwr.generate_report()
    row = next(l for l in out.splitlines() if "universe_expansion" in l and "mid" in l)
    assert "✅" not in row, "低于费用线仍打绿勾 = 回归 (误导性绿勾)"
    assert "⚪" in row


def test_small_n_never_marked(patched):
    """n=7 < 30 ⇒ 一律 ⚪ (既有纪律: n<30 只报进度禁止决策)。"""
    patched["stats"] = [_stat(n=7, avg_1d=0.5, avg_5d=0.9, wr=86.0, hist_5d=0.9)]
    out = iwr.generate_report()
    assert "✅" not in next(l for l in out.splitlines() if "strong(50+)" in l)


def test_qualified_cell_above_fee_line_still_marked(patched):
    """正向守卫: 过 n≥30 且过费用线的格仍须打标 (防止修成「什么都不标」)。"""
    patched["stats"] = [_stat(n=60, avg_1d=0.1, avg_5d=0.35, wr=65.0,
                              hist_n=200, hist_5d=0.30)]
    out = iwr.generate_report()
    assert "✅" in next(l for l in out.splitlines() if "strong(50+)" in l)


def test_qualified_negative_cell_marked_red(patched):
    """正向守卫: 过双门槛且为负 ⇒ 🔴 (防止修成只标绿不标红)。"""
    patched["stats"] = [_stat(n=60, avg_1d=-0.1, avg_5d=-0.35, wr=35.0,
                              hist_n=200, hist_5d=-0.30)]
    out = iwr.generate_report()
    assert "🔴" in next(l for l in out.splitlines() if "strong(50+)" in l)


def test_no_qualified_cell_emits_explicit_warning(patched):
    """全窗零格可判定 ⇒ 必须显式出声, 不静默。"""
    patched["stats"] = [_stat(strategy="deep_analysis", n=237, avg_5d=0.01,
                              wr=50.0, hist_n=1060, hist_5d=-0.001)]
    out = iwr.generate_report()
    assert "可判定 0" in out and "不得据此调策略权重" in out


# ── ③ 明细表窗口标注 + 长短窗反号 ────────────────────────────────────────
def test_detail_table_declares_window(patched):
    """明细表必须标注窗口, 且与 IC 段同窗 (修复前用全史 10.5× 样本无标注)。"""
    out = iwr.generate_report()
    assert "近30天, 与IC段同窗" in out


def test_get_signal_stats_filters_window():
    """源码守卫: get_signal_stats 必须带 trade_date 过滤 (修复前完全没有)。"""
    src = inspect.getsource(iwr.get_signal_stats)
    assert "trade_date" in src, "明细表又变成无窗口过滤 = 回归"
    assert "days_back" in src


def test_sign_flip_flagged(patched):
    """长短窗符号相反 ⇒ 必须打 ⚠️ (regime 漂移, 不得当稳定 edge)。"""
    patched["stats"] = [_stat(strategy="universe_expansion", direction="SHORT",
                              n=22, avg_1d=-0.01, avg_5d=-0.03, wr=41.0,
                              hist_n=133, hist_5d=0.03, sign_flip=True)]
    out = iwr.generate_report()
    assert "长短窗反号" in out


def test_history_comparison_column_present(patched):
    """全史对照列必须出现 (让读者看见两窗差异, 而非只看一个数)。"""
    patched["stats"] = [_stat(strategy="deep_analysis", n=237, avg_5d=0.012,
                              wr=50.0, hist_n=1060, hist_5d=-0.001)]
    out = iwr.generate_report()
    assert "全史 n=1060" in out


# ── ④ 影子组合: n 门槛 + 集中度 + 胜率背离 ───────────────────────────────
def test_shadow_small_n_gets_no_verdict(patched):
    """band n=16<30 ⇒ 不得输出「该松」裁决 (修复前输出「🔴 拦掉的是钱 (该松)」)。"""
    patched["shadow"] = ([{"gate": "band", "n": 16, "net": 172.15, "win_rate": 75.0,
                           "median": 11.59, "worst": -26.65, "best": 63.61,
                           "top3_share_pct": 80.1}], [])
    out = iwr.generate_report()
    assert _VERDICT_LOOSEN not in out, "n=16 仍判「该松」= 回归"
    assert "样本不足" in out


def test_shadow_high_concentration_blocks_verdict(patched):
    """n 够但 Top3 集中度 >50% ⇒ 判「单笔主导」, 不给该松/保持。"""
    patched["shadow"] = ([{"gate": "g", "n": 40, "net": 100.0, "win_rate": 60.0,
                           "median": 5.0, "worst": -20.0, "best": 50.0,
                           "top3_share_pct": 80.1}], [])
    out = iwr.generate_report()
    assert "单笔主导" in out and "不得作落地依据" in out
    assert _VERDICT_LOOSEN not in out and _VERDICT_KEEP not in out


def test_shadow_winrate_net_divergence_exposed(patched):
    """净-920 但胜率70% ⇒ 必须展开分布并报背离 (修复前只报总账)。"""
    patched["shadow"] = ([{"gate": "falsification_vwap_slope", "n": 69, "net": -920.26,
                           "win_rate": 70.0, "median": 12.74, "worst": -259.11,
                           "best": 84.53, "top3_share_pct": 20.0}], [])
    out = iwr.generate_report()
    assert "胜率与净额背离" in out
    assert "中位=+12.74" in out and "最差=-259.11" in out


def test_shadow_normal_case_still_gives_verdict(patched):
    """正向守卫: n 足 + 集中度低 ⇒ 仍须给「保持」结论 (防修成什么都不判)。"""
    patched["shadow"] = ([{"gate": "vwap_gate", "n": 75, "net": -404.17, "win_rate": 50.0,
                           "median": 11.78, "worst": -225.26, "best": 66.55,
                           "top3_share_pct": 30.0}], [])
    out = iwr.generate_report()
    assert _VERDICT_KEEP in out


def test_shadow_reader_helper_present(patched):
    """正负号反直觉 ⇒ 必须带读法说明 (负=拦对, 正=拦掉钱), 防读者误读。"""
    patched["shadow"] = ([{"gate": "g", "n": 40, "net": -10.0, "win_rate": 50.0,
                           "median": 0.0, "worst": -5.0, "best": 5.0,
                           "top3_share_pct": 30.0}], [])
    assert "读法" in iwr.generate_report()


def test_shadow_missing_file_still_speaks(patched):
    """既有契约不回归: 结算文件缺失必须出声 (2026-08-28 修过的静默失效)。"""
    patched["shadow"] = ([], ["结算文件不存在 → 影子采集/结算链疑似从未跑过 (查 Hermes 影子结算作业)"])
    assert "影子组合段不完整" in iwr.generate_report()


# ── ⑤ KR1.3 饱和标注 ────────────────────────────────────────────────────
def test_saturated_kr_flags_saturation(patched):
    """饱和指标必须标注饱和, 不再伪装成有信息量的周度进展。"""
    patched["okr"]["okr_gap_text"] = ("✅ 已达标 (4929笔 = 目标30的 164×, **指标已饱和**; "
                                      "首笔达标日 2026-05-14 ⇒ 此后恒为 ✅, 无周度信息量)")
    out = iwr.generate_report()
    assert "指标已饱和" in out and "无周度信息量" in out


def test_okr_gap_text_logic_saturates():
    """直接验 get_okr_progress 的饱和文案分支 (不依赖桩)。"""
    src = inspect.getsource(iwr.get_okr_progress)
    assert "指标已饱和" in src, "饱和分支被删 = 回归"
    assert "_mult" in src


# ── 常量与口径守卫 ──────────────────────────────────────────────────────
def test_fee_line_constant_is_ten_bps():
    """阈值必须锚费用线 ~10bps (既有裁决: edge 必须 > 费用线)。"""
    assert iwr.FEE_LINE_PCT == pytest.approx(0.10)
    assert iwr.MIN_N_FOR_MARK == 30
    assert iwr.P_SIGNIFICANT == pytest.approx(0.05)
    assert iwr.TOP_CONC_LIMIT == pytest.approx(50.0)


def test_old_two_bps_threshold_gone():
    """旧的 ±0.02 (2bps) emoji 阈值必须已移除。"""
    src = inspect.getsource(iwr.generate_report)
    assert 'avg_5d"] > 0.02' not in src
    assert 'avg_5d"] < -0.02' not in src


def test_direction_section_shows_p_value(patched):
    """按方向段必须带 p 值 (否则与建议门口径不一致 ⇒ 同页自相矛盾)。"""
    patched["win_ic"] = {"LONG": {"ic": -0.0170, "pval": 0.774, "n": 289},
                         "SHORT": {"ic": -0.0557, "pval": 0.580, "n": 101}}
    out = iwr.generate_report()
    seg = out.split("按方向:")[1].split("策略×方向×强度")[0]
    assert "p=0.774" in seg and "p=0.580" in seg
    assert "🔴 SHORT" not in seg, "不显著仍打红 = 与建议段矛盾"


def test_neutral_denominator_unchanged(patched):
    """回归守卫: NEUTRAL 占比分母是 n_signals(**含** NEUTRAL) — 我一度误判为错, 实测是对的。

    实测: 近30天 总470 = 非NEUTRAL 390 + NEUTRAL 80 ⇒ 80/470=17% 正确,
    且 signal_ic_tracker 算 IC 时 `if d == "NEUTRAL": continue` 已正确排除。
    本测试钉住这点, 防后人"修"成 80/390 = 20.5%。
    """
    patched["neutral"] = 80
    assert "n=80 (17%" in iwr.generate_report()
