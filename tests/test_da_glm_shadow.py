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
    CRON_MARKER, MIN_N_SHORT, _self_remove_cron, evaluate, is_tainted, load_rows)
from app.playbook.shadow_registry import REGISTRY, ShadowStatus  # noqa: E402


def _row(date="2026-09-01", sym="US.TEST", direction="SELL_SHORT",
         err=None, change=None, mode="live"):
    return {"type": "da_glm_shadow", "run_mode": mode, "date": date,
            "symbol": sym, "glm_direction": direction,
            "glm_error": err, "shadow_change_pct": change}


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


def test_tainted_excluded_from_short_count():
    # 9.6: 漂移样本剔除 — is_tainted 与生产快照偏差>1.0pp
    assert is_tainted({"date": "x", "symbol": "x", "shadow_change_pct": 3.0},
                      {"change_pct": 1.0}) is True      # 2.0pp > 1.0
    assert is_tainted({"date": "x", "symbol": "x", "shadow_change_pct": 1.5},
                      {"change_pct": 1.0}) is False     # 0.5pp ≤ 1.0
    assert is_tainted({"shadow_change_pct": None}, {"change_pct": 1.0}) is False  # 缺一不判
    # 5 SHORT 里 1 条被快照判污 (ckpt 读不到→不判污, 用注入快照口径另测):
    # evaluate 内部 _ckpt_snapshot 对不存在文件返 {} → 不判污, SHORT 计数不缩
    e = evaluate([_row() for _ in range(MIN_N_SHORT)])
    assert e["n_tainted"] == 0 and e["n_short"] == MIN_N_SHORT


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
