"""da_glm_shadow 影子: 评估逻辑行为级测试 + 注册表钉子 (9.1/9.5/9.6 修复守卫).

红绿纪律: 判定函数对构造数据必须给出机械一致结论;
注册项状态钉在 READY_TO_EVALUATE (2026-09-15 窗口 8/31-9/14 收口: 机械裁决已出
PROMOTE_PROPOSAL, 采集环已退役, 待用户对 DEB-20260915 终裁 — 终态翻牌必须是显式裁决);
eval 脚本文件必须真实存在; 越窗行不得入读数 (2026-09-15 收口补丁)。
"""
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.da_glm_shadow_eval import (  # noqa: E402
    CRON_MARKER, MIN_N_SHORT, WINDOW_END, WINDOW_START, _self_remove_cron,
    count_out_of_window, evaluate, is_tainted, load_rows, taint_diff)
from app.playbook.shadow_registry import REGISTRY, ShadowStatus  # noqa: E402


def _row(date="2026-09-01", sym="US.TEST", direction="SELL_SHORT",
         err=None, eff=None, prod_chg=None, mode="live"):
    r = {"type": "da_glm_shadow", "run_mode": mode, "date": date,
         "symbol": sym, "glm_direction": direction,
         "glm_error": err}
    if eff is not None:
        r["shadow_effective_chg_pct"] = eff
    if prod_chg is not None:
        r["prod_snapshot_change_pct"] = prod_chg
    return r


def _mk_jsonl(tmp_path: Path, rows: list) -> str:
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return str(p)


def test_load_rows_live_only(tmp_path):
    rows = [_row(mode="live"), _row(mode="test"), _row()]
    got = load_rows(_mk_jsonl(tmp_path, rows))
    assert len(got) == 2  # test 行剔除


def test_verdict_promote_and_archive():
    # ①②全过: 10 条成功, 5 条 SHORT → PROMOTE_PROPOSAL
    rows = [_row(direction="SELL_SHORT") for _ in range(5)] + \
           [_row(direction="BUY") for _ in range(5)]
    e = evaluate(rows)
    assert e["json_rate"] == 1.0 and e["c1_json"] and e["c2_short"]
    assert e["verdict"] == "PROMOTE_PROPOSAL"
    # ②不过: SHORT 仅 4 → ARCHIVE
    rows2 = [_row(direction="SELL_SHORT") for _ in range(4)] + \
            [_row(direction="BUY") for _ in range(6)]
    assert evaluate(rows2)["verdict"] == "ARCHIVE"
    # ①不过: 一半带 glm_error → ARCHIVE (50% < 90%)
    rows3 = [_row(direction="SELL_SHORT") for _ in range(5)] + \
            [_row(direction="BUY", err="timeout") for _ in range(5)]
    assert evaluate(rows3)["verdict"] == "ARCHIVE"


def test_tainted_effective_pairing():
    # 9.6 修复①(口径配对): 双侧 effective 口径 — 异基比较是旧版误杀源头
    # EIX 型: 两臂同见盘前大跌, 90min 间漂移 0.33pp → 材料 同构, 不污
    assert is_tainted({"shadow_effective_chg_pct": -9.5,
                       "prod_snapshot_change_pct": -9.83}) is False
    # 盘中窗口内新息: 07:00 -1.5% → 08:30 -3.0%, 漂移 1.5pp > 1.0 → 污
    assert is_tainted({"shadow_effective_chg_pct": -3.0,
                       "prod_snapshot_change_pct": -1.5}) is True
    # 生产值行内优先: 检查点过期 (无文件) 不影响判污 (修复②过期免疫)
    assert is_tainted({"date": "2099-01-01", "symbol": "US.NOFIle",
                       "shadow_effective_chg_pct": -3.0,
                       "prod_snapshot_change_pct": -1.5}) is True
    # 兜底: 行内缺生产值但检查点在 (snap 注入) → 重读判污
    assert is_tainted({"shadow_effective_chg_pct": 3.0},
                      {"change_pct": 1.0}) is True
    # 旧行 (修复前采集): 无 effective 字段 → 缺一不判, 不再走 stale 推导
    assert is_tainted({"shadow_change_pct": 3.0,
                       "prod_snapshot_change_pct": 1.0}) is False
    assert taint_diff({"shadow_change_pct": 3.0}) is None


def test_taint_unevaluable_counted_transparent():
    # 9.6 修复: 不可判行单列计数 (n_taint_unevaluable), 全部保守保留不剔除
    rows = [_row(direction="SELL_SHORT") for _ in range(MIN_N_SHORT)]
    e = evaluate(rows)
    assert e["n_tainted"] == 0
    assert e["n_taint_unevaluable"] == MIN_N_SHORT
    assert e["n_short"] == MIN_N_SHORT  # SHORT 计数不缩
    # 新行混合: 1 污 1 不可判 → 仅污行剔除
    rows2 = ([_row(direction="SELL_SHORT") for _ in range(MIN_N_SHORT - 1)]
             + [_row(direction="SELL_SHORT", eff=-3.0, prod_chg=-1.5)])
    e2 = evaluate(rows2)
    assert e2["n_tainted"] == 1 and e2["n_taint_unevaluable"] == MIN_N_SHORT - 1
    assert e2["n_short"] == MIN_N_SHORT - 1


def test_dir_criterion_needs_n20():
    # 9.5: n<20 时③只出参考值, 不作证据读 — t1 依赖真实日K文件, 构造空集验证参考路径
    rows = [_row(direction="SELL_SHORT") for _ in range(6)]
    e = evaluate(rows)
    assert e["n_t1"] == 0          # 无 replay_bars_daily 文件 → 不可结算
    assert e["c3_direction"] is False and e["c3_reference_only"] is False


def test_registry_pin():
    items = {it.id: it for it in REGISTRY}
    it = items.get("da_glm_shadow")
    assert it is not None, "da_glm_shadow 未注册 (9.1 修复回归)"
    # 2026-09-15 **用户终裁「停止 da_glm_shadow 不转正」** ⇒ 终态 SUPERSEDED (归档留痕:
    # 脚本与数据保留 = 停用非删证)。此前钉的 READY_TO_EVALUATE 是"待终裁"中间态, 已过期。
    assert it.status == ShadowStatus.SUPERSEDED
    assert it.eval_script == "scripts/da_glm_shadow_eval.py"
    # 采集行退役后 eval 无周期挂载面 → 按需触发 (防挂载哨兵把"一次性已跑完"当悬空报)
    assert it.eval_on_demand is True
    assert it.last_evaluated == "2026-09-15"
    assert "PROMOTE_PROPOSAL" in it.graduation_criteria
    assert "n≥20" in it.graduation_criteria  # 9.5 判据③降级钉住
    # 2026-09-01 复查收口: live-only 计数口径钉住 (原 lines 把 test 行计入)
    assert it.count_mode == "where:run_mode=live"
    assert (Path(__file__).resolve().parents[1] / it.eval_script).exists()
    # 终裁落痕: 裁决历史末条必须是 SUPERSEDED, 且不得写成"转正"的假绿
    last = it.verdict_history[-1]
    assert last["date"] == "2026-09-15" and "SUPERSEDED" in last["verdict"]
    assert "不转正" in it.notes, "终态理由须落在 notes (归档留痕)"


# ── 2026-09-15 收口: 预注册窗口硬边界 (采集环与裁决器是两个开关) ──

def test_out_of_window_rows_excluded(tmp_path):
    """越窗行不入读数: 9/15 采集环多跑一夜 (16 行) 不得被算进 8/31-9/14 窗口。"""
    rows = [_row(date="2026-09-14"), _row(date="2026-08-31"),
            _row(date="2026-09-15"), _row(date="2026-09-15")]
    p = _mk_jsonl(tmp_path, rows)
    got = load_rows(p)
    assert [r["date"] for r in got] == ["2026-09-14", "2026-08-31"]
    assert len(got) == 2, "越窗行混进了窗口读数 (窗口污染)"
    assert count_out_of_window(p) == 2, "越窗行必须单列披露, 不得静默丢弃"
    # 窗口外单独成样本时判据照常机械判 (不做"窗口外也凑数"的假绿)
    assert evaluate(load_rows(p))["n_live"] == 2


def test_window_bound_is_pinned():
    """窗口边界是预注册事实, 改动=重新裁决 — 漂了必须红。"""
    assert (WINDOW_START, WINDOW_END) == ("2026-08-31", "2026-09-14")


def test_out_of_window_counts_live_only(tmp_path):
    """越窗计数只数 live 行 (test 行不进任何读数, 含披露计数)。"""
    rows = [_row(date="2026-09-15"), _row(date="2026-09-15", mode="test")]
    assert count_out_of_window(_mk_jsonl(tmp_path, rows)) == 1


# ── 一次性裁决 cron 自删 (2026-09-01 复查收口: eval 补挂 9/15 一次性机械裁决) ──

def _fake_crontab(monkeypatch, cur_text):
    installed = {}

    def fake_run(cmd, **kw):
        if cmd[:2] == ["crontab", "-l"]:
            return types.SimpleNamespace(stdout=cur_text)
        if cmd[:1] == ["crontab"]:
            installed["text"] = kw.get("input")
            return types.SimpleNamespace(stdout="")
        raise AssertionError(f"unexpected cmd: {cmd}")

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    return installed


def test_self_remove_cron_removes_marker_line(monkeypatch):
    cur = ("0 5 * * 1 job_stays\n"
           f"35 6 15 9 * ... scripts/da_glm_shadow_eval.py ... # {CRON_MARKER}\n")
    installed = _fake_crontab(monkeypatch, cur)
    msg = _self_remove_cron()
    assert "已自删" in msg
    assert installed["text"] == "0 5 * * 1 job_stays\n"
    assert CRON_MARKER not in installed["text"]


def test_self_remove_cron_noop_when_absent(monkeypatch):
    _fake_crontab(monkeypatch, "0 5 * * 1 job_stays\n")
    assert "未找到" in _self_remove_cron()
