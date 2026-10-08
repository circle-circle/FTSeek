"""RFC-139 L2 契约测试: 统一 runner 确定性 + 分类诚实 + 通俗背景.

钉住的契约:
  · 同输入同输出 (确定性 — agent 不进判定层的机器保障);
  · 分类如实: VERDICT_ARTIFACT / COLLECTOR_ONLY / NO_EVAL_SCRIPT / SCRIPT_FAILED;
  · 未收录 spec 回落注册表原文, 不臆造;
  · 通俗背景四行 (这是什么/决策源/判据原文/要裁什么) 全部可渲染。
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "auto_adjudicate.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("auto_adjudicate_test_mod", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def runner():
    return _load_runner()


def _row(sid, **kw):
    base = {
        "id": sid, "status": "READY_TO_EVALUATE", "n": 38, "min_n": 30,
        "needs_adjudication": True, "criteria": "n≥30 + 单侧安全门",
        "eval_script": "scripts/fake_eval.py", "verdict_era": None,
    }
    base.update(kw)
    return base


class TestClassification:
    def test_verdict_artifact(self, runner, tmp_path, monkeypatch):
        art = tmp_path / "v.json"
        art.write_text(json.dumps({"verdict": "NOT_ESTABLISHED_KEEP_SAMPLING", "n": 36, "mean": -17.7}))

        class _S:
            verdict_artifact = "v.json"
            source = "RFC-TEST"
            metric = "m"
            control = "c"
            min_effect = "me"
            sample_gates = {}
            known_gap = ""
        monkeypatch.setattr(runner, "get_spec", lambda sid: _S() if sid == "x" else None)
        monkeypatch.setattr(runner, "REPO", tmp_path)
        row = _row("x")
        out = runner.evaluate([row])
        assert out["items"][0]["classification"] == "VERDICT_ARTIFACT"
        assert out["items"][0]["verdict"] == "NOT_ESTABLISHED_KEEP_SAMPLING"
        assert out["items"][0]["readout"]["n"] == 36

    def test_collector_only(self, runner, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "REPO", tmp_path)
        # COLLECTOR_ONLY 语义 = 脚本真实存在 (能跑) 但不产 verdict; 建一个真脚本,
        # 否则会被 P2 修复判成 SCRIPT_MISSING (read-only 不再对不存在脚本假称「能跑」)。
        sc = tmp_path / "scripts" / "fake_eval.py"
        sc.parent.mkdir(parents=True, exist_ok=True)
        sc.write_text("# collector only, no verdict\n")
        row = _row("x")  # eval_script=scripts/fake_eval.py 现在真实存在
        out = runner.evaluate([row])
        assert out["items"][0]["classification"] == "COLLECTOR_ONLY"

    def test_no_eval_script(self, runner, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "REPO", tmp_path)
        row = _row("x", eval_script=None)
        out = runner.evaluate([row])
        assert out["items"][0]["classification"] == "NO_EVAL_SCRIPT"

    def test_script_failed(self, runner, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "REPO", tmp_path)
        row = _row("x")
        out = runner.evaluate([row], execute=True, timeout=5)
        # 无产物 + 脚本不存在 ⇒ subprocess rc≠0 ⇒ SCRIPT_FAILED
        assert out["items"][0]["classification"] == "SCRIPT_FAILED"

    def test_pending_filter(self, runner, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "REPO", tmp_path)
        rows = [_row("a"), _row("b", needs_adjudication=False)]
        out = runner.evaluate(rows)
        assert out["pending_count"] == 1
        assert [i["id"] for i in out["items"]] == ["a"]


class TestDeterminism:
    def test_same_input_same_output(self, runner, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "REPO", tmp_path)
        rows = [_row("x")]
        r1 = runner.evaluate(rows)
        r2 = runner.evaluate(rows)
        r1.pop("generated_at"), r2.pop("generated_at")
        assert r1 == r2


class TestRender:
    def test_render_contains_background_and_boundary(self, runner, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "REPO", tmp_path)
        rows = [_row("x")]
        report = runner.evaluate(rows)
        text = runner.render(report)
        assert "这是什么" in text
        assert "决策源" in text
        assert "判据原文" in text
        assert "runner 只执行并呈报, 不判过/不过" in text

    def test_render_collector_gap(self, runner, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "REPO", tmp_path)
        # 同 test_collector_only: COLLECTOR_ONLY 需脚本真实存在 (P2 修复后不存在=SCRIPT_MISSING)
        sc = tmp_path / "scripts" / "fake_eval.py"
        sc.parent.mkdir(parents=True, exist_ok=True)
        sc.write_text("# collector only\n")
        report = runner.evaluate([_row("x")])
        assert "采集器≠结算器" in runner.render(report)
