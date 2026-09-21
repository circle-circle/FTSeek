"""因子权重对账钉 (2026-09-21, DEB-20260921-factor-weight-verdict-reconciliation).

钉四件事 (都是"看不见的失败"面):
  1. 静默契约: 对账面签名不变 ⇒ 零输出 = 不推送; 首次运行/签名变化 ⇒ 输出全量。
     (签名只取状态维度 —— 若把绝对金额/产物 mtime 计入, 每轮都"变" ⇒ 静默契约形同虚设)
  2. 六类错配判据逐类可检出 (A 正权重×负证据 / B IC↔verdict 反号 / C Σhist 存量 /
     D 小样本高权重 / E 罚错 / F 尺子冲突) —— 判据写错时不得"静默全绿"。
  3. 探测失败必须可见: 输入缺失 ⇒ rc=2 + 显式行 (严禁读成"无错配")。
  4. 只读纪律: 台账以 mode=ro 打开, 不回写 yaml/prompt/DB。

回滚: 删本文件 (与被测脚本同批)。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "scripts" / "factor_weight_reconcile.py"


def _load():
    spec = importlib.util.spec_from_file_location("factor_weight_reconcile_mod", SRC)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod(tmp_path, monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "PRODUCT", tmp_path / "latest.json")
    monkeypatch.setattr(m, "STATE", tmp_path / "state.json")
    return m


def test_first_run_full_then_silent(mod):
    """首跑全量; 同一对账面再跑 ⇒ 零输出; --force ⇒ 再出全量。"""
    first = mod.build_report(dry=False)
    assert first.strip(), "首次运行必须输出全量 (用户要先看到整块板)"
    assert "因子权重对账" in first
    assert mod.PRODUCT.exists(), "产物未落盘"
    assert mod.build_report(dry=False) == "", "对账面未变却输出了 ⇒ 会天天刷屏"
    assert mod.build_report(force=True, dry=False).strip(), "--force 应忽略 state 再出全量"


def test_signature_changes_when_class_moves(mod):
    """台账 verdict 翻转 ⇒ 必须重新抬出 (状态维度变化不能被静默吞掉)。"""
    assert mod.build_report(dry=False).strip()
    before = mod.STATE.read_text(encoding="utf-8")
    assert before
    # 直接改 state 里某一个因子的 verdict, 模拟台账读数变化
    import json
    st = json.loads(before)
    k = sorted(st["signature"])[0]
    st["signature"][k] = st["signature"][k].replace("negative", "positive") \
        if "negative" in st["signature"][k] else st["signature"][k] + "|x"
    mod.STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    assert mod.build_report(dry=False).strip(), "签名变化后应重新输出"


def test_six_classes_detectable(mod):
    """六类判据逐类可检出 (用合成行, 不依赖线上 DB 读数)。"""
    def item(name, **kw):
        base = {"factor": name, "enabled": True, "base": 1, "ic_adj": 0, "sigma_hist": 0,
                "eff": 1, "resonance_gated": False, "ic": None, "ic_n": None,
                "ledger_n": 0, "ledger_wr": None, "ledger_avg_pnl": None,
                "ledger_total_pnl": None, "ledger_pf": None, "ledger_verdict": None}
        base.update(kw)
        return base

    data = {"items": [
        # A: enabled 正权重 × negative × n>=30
        item("a_pos_neg", eff=3, ledger_n=100, ledger_wr=0.33, ledger_total_pnl=-50.0,
             ledger_verdict="negative"),
        # B: ic_adj +1 而 verdict negative —— 两个变体: 现金同向 / 现金反向
        item("b_ic_conflict", ic_adj=1, eff=2, ledger_n=50, ledger_wr=0.3,
             ledger_total_pnl=-9.0, ledger_verdict="negative"),
        item("b_ic_conflict_cash_ok", ic_adj=1, eff=3, ledger_n=60, ledger_wr=0.35,
             ledger_total_pnl=80.0, ledger_verdict="negative"),
        # C: Σhist 非空
        item("c_hist", sigma_hist=-2, eff=-1, ledger_n=40, ledger_wr=0.4,
             ledger_total_pnl=-1.0, ledger_verdict="negative"),
        # D: eff>0 但台账 n<30
        item("d_thin", eff=9, ledger_n=1, ledger_wr=1.0, ledger_total_pnl=7.0,
             ledger_verdict="observing"),
        # E: eff<0 而 verdict!=negative 且现金为正
        item("e_wrongpenalty", eff=-5, ledger_n=32, ledger_wr=0.34, ledger_total_pnl=313.0,
             ledger_verdict="observing"),
        # F: verdict negative 而现金为正
        item("f_rulerclash", eff=2, ledger_n=179, ledger_wr=0.352, ledger_total_pnl=250.0,
             ledger_pf=1.12, ledger_verdict="negative"),
        # G: verdict negative, 但剔非结果行后胜率过半 (negative 由分母口径驱动)
        item("g_denominator", eff=0, ledger_n=100, ledger_wins=57, ledger_losses=43,
             ledger_n_decided=100, ledger_wr_decided=0.57, ledger_nonoutcome_share=0.60,
             ledger_wr=0.228, ledger_total_pnl=0.0, ledger_verdict="negative"),
    ]}
    mm = mod.find_mismatches(data)
    assert "a_pos_neg" in "".join(mm["A_正权重负证据"])
    assert "b_ic_conflict" in "".join(mm["B_IC通道反号"])
    b_txt = "".join(mm["B_IC通道反号"])
    assert "现金反向" in b_txt and "现金同向" in b_txt, \
        "B 类必须标注现金口径同/反向 (否则读者无法判 IC 究竟对不对)"
    assert "c_hist" in "".join(mm["C_Σhist双计存量"])
    assert "d_thin" in "".join(mm["D_小样本高权重"])
    assert "e_wrongpenalty" in "".join(mm["E_罚错"])
    assert "f_rulerclash" in "".join(mm["F_尺子冲突"])
    assert "g_denominator" in "".join(mm["G_台账分母口径"]), \
        "分母口径类必须可检出 (negative 却剔零后胜率过半)"


def test_disabled_factor_not_reported_as_A(mod):
    """enabled=False 的因子不进 A 类 (不在打分路径上, 报了就是噪声)。"""
    data = {"items": [{
        "factor": "off_factor", "enabled": False, "base": -3, "ic_adj": 1, "sigma_hist": -5,
        "eff": -7, "ledger_n": 275, "ledger_wr": 0.34, "ledger_total_pnl": -472.0,
        "ledger_pf": 0.87, "ledger_verdict": "negative"}]}
    mm = mod.find_mismatches(data)
    assert mm["A_正权重负证据"] == []


def test_detector_error_is_loud(mod, monkeypatch, capsys):
    """台账库不可读 ⇒ rc=2 + 显式行 (不得静默返回 0)。"""
    monkeypatch.setattr(mod, "LEDGER_DB", Path("/nonexistent/ledger.db"))
    monkeypatch.setattr(sys, "argv", ["factor_weight_reconcile.py", "--dry"])
    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 2, "探测失败必须非零退出"
    assert "探测失败" in out and "不得读成" in out


def test_readonly_discipline():
    """源码级只读钉: 台账 mode=ro; 不写 yaml/prompt; 不对台账库做写语句。"""
    src = SRC.read_text(encoding="utf-8")
    assert "mode=ro" in src, "台账库必须以只读方式打开"
    for forbidden in ("yaml.dump", "yaml.safe_dump", "INSERT INTO playbook_factor_stats",
                      "UPDATE playbook_factor_stats", "DELETE FROM playbook_factor_stats"):
        assert forbidden not in src, f"对账器出现写路径: {forbidden}"