"""scripts/da_prompt_distill_shadow.py 契约测试 (2026-09-01, DEB-20260901c 阶段二).

影子采集脚本的纯逻辑验证: 缺目录优雅退出 / 无生产对照跳过 / 配对落行字段完整 /
注册表登记。DeepAnalyzer 与 llm_synth 全部打桩 — 绝不触发真实 LLM 调用。
回滚: 删本文件即可。
"""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "da_prompt_distill_shadow", PROJECT_ROOT / "scripts" / "da_prompt_distill_shadow.py"
)
sh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sh)


@pytest.fixture
def fake_skill(monkeypatch):
    """打桩 skill 侧模块 — 测试环境零 LLM 调用。"""
    fake_llm = types.ModuleType("llm_synth")
    fake_llm.synthesize = lambda *a, **k: {}
    fake_da = types.ModuleType("deep_analyzer")

    class _FakeDA:
        def __init__(self, collected, synth_module=None):
            self.collected = collected

        def analyze(self):
            return {"_llm_output": {
                "direction": "LONG", "score_band": "HIGH", "rating": "看多",
                "executive_summary": "v2 摘要", "_finish_reason": "stop",
                "_llm_latency_ms": 12345, "_thinking_enabled": True,
                "_usage": {"prompt_tokens": 5000, "completion_tokens": 2500},
            }}

    fake_da.DeepAnalyzer = _FakeDA
    monkeypatch.setitem(sys.modules, "llm_synth", fake_llm)
    monkeypatch.setitem(sys.modules, "deep_analyzer", fake_da)


@pytest.fixture
def tmp_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(sh, "CKPT_ROOT", str(tmp_path / "da_checkpoint"))
    monkeypatch.setattr(sh, "SHADOW_PATH", str(tmp_path / "shadow" / "out.jsonl"))
    return tmp_path


def test_missing_dir_graceful(tmp_paths, fake_skill, capsys):
    assert sh.run(run_mode="test") == 0
    assert "不存在" in capsys.readouterr().out
    assert not Path(sh.SHADOW_PATH).exists()


def _mk_day(tmp_paths, day="2026-09-02"):
    d = tmp_paths / "da_checkpoint" / day
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_pairs_and_writes_row(tmp_paths, fake_skill, monkeypatch):
    d = _mk_day(tmp_paths)
    monkeypatch.setattr(sh, "_today_et", lambda: "2026-09-02")
    # 候选 A: raw + 生产 checkpoint 俱在 → 处理
    (d / "US.AAA.raw.json").write_text(json.dumps({
        "symbol": "US.AAA", "trade_date": "2026-09-02", "schema": "da_raw_inputs/v1",
        "collected": {"snapshot": {}}}), encoding="utf-8")
    (d / "US.AAA.json").write_text(json.dumps({
        "_llm_output": {"direction": "LONG", "score_band": "MEDIUM", "rating": "看多",
                          "_prompt_hash": "1f53d632452b",
                          "_usage": {"prompt_tokens": 8000}}}), encoding="utf-8")
    # 候选 B: 只有 raw 无生产对照 → 跳过
    (d / "US.BBB.raw.json").write_text(json.dumps({
        "symbol": "US.BBB", "collected": {}}), encoding="utf-8")
    assert sh.run(run_mode="live") == 0
    rows = [json.loads(l) for l in Path(sh.SHADOW_PATH).read_text().splitlines()]
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "US.AAA" and r["run_mode"] == "live"
    assert r["v1_direction"] == "LONG" and r["v2_direction"] == "LONG"
    assert r["direction_agree"] is True
    assert r["prompt_v1_hash"] == "1f53d632452b"
    assert r["v2_tokens_in"] == 5000 and r["v1_tokens_in"] == 8000
    assert r["v2_finish_reason"] == "stop"


def test_corrupt_raw_skipped_not_crash(tmp_paths, fake_skill, monkeypatch, capsys):
    d = _mk_day(tmp_paths)
    monkeypatch.setattr(sh, "_today_et", lambda: "2026-09-02")
    (d / "US.BAD.raw.json").write_text("{broken", encoding="utf-8")
    (d / "US.BAD.json").write_text(json.dumps({"_llm_output": {"direction": "SKIP"}}),
                                    encoding="utf-8")
    assert sh.run(run_mode="test") == 0  # 损坏容错, 不崩
    assert not Path(sh.SHADOW_PATH).exists()
    assert "读取失败" in capsys.readouterr().err  # R5 留痕 (非静默吞)


def test_registry_entry():
    from app.playbook.shadow_registry import REGISTRY
    item = next((i for i in REGISTRY if i.id == "da_prompt_distill_shadow"), None)
    assert item is not None
    assert item.count_mode == "where:run_mode=live"
    assert item.min_n == 10
    assert "9/2-9/15" in item.graduation_criteria
