"""da_glm_shadow 影子: 评估逻辑行为级测试 + 注册表钉子 (9.1/9.5/9.6 修复守卫).

红绿纪律: 判定函数对构造数据必须给出机械一致结论;
注册项必须保持 OBSERVING 直到 9/14 窗口裁决; eval 脚本文件必须真实存在。
"""
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.da_glm_shadow_eval import (  # noqa: E402
    CRON_MARKER, MIN_N_SHORT, _self_remove_cron, evaluate, is_tainted,
    load_rows, taint_diff)
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
    assert it.status == ShadowStatus.OBSERVING
    assert it.eval_script == "scripts/da_glm_shadow_eval.py"
    assert "PROMOTE_PROPOSAL" in it.graduation_criteria
    assert "n≥20" in it.graduation_criteria  # 9.5 判据③降级钉住
    # 2026-09-01 复查收口: live-only 计数口径钉住 (原 lines 把 test 行计入)
    assert it.count_mode == "where:run_mode=live"
    assert (Path(__file__).resolve().parents[1] / it.eval_script).exists()


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
