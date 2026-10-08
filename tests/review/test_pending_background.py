"""RFC-139 L4 契约测试: 待裁推送通俗背景块 + 判据实测读 verdict 产物.

钉住的契约 (用户裁 2026-10-08「推送做好通俗背景说明, 避免遗忘和混淆决策源」):
  · 每条待裁条目带背景块: 这是什么 / 决策源 / 判据原文 / (已知缺口) / 你要决定什么;
  · 决策源优先读 L1 spec.source (机器可查出处), 未收录回落注册表判据原文;
  · 判据实测优先读 spec.verdict_artifact (修复: 过去读 data_path jsonl 永远无读数);
  · NEEDS_USER 项的「你要决定」必须是明确动作, 不许静默。
"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "pending_adjudication_report.py"


def _load_report_mod():
    spec = importlib.util.spec_from_file_location("pending_report_test_mod", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _row(sid, **kw):
    base = {
        "id": sid, "status": "READY_TO_EVALUATE", "n": 38, "min_n": 30,
        "needs_adjudication": True, "criteria": "n≥30 + 单侧安全门",
        "artifact": "data/entry_band_widen_shadow.jsonl",
        "eval_script": "scripts/settle_entry_band_widen.py",
        "verdict_era": None,
    }
    base.update(kw)
    return base


class TestBackgroundBlock:
    def test_needs_user_has_actionable_decision(self):
        m = _load_report_mod()
        it = _row("eth_relative_volume_gate")
        lines = m._background_block(it)
        joined = "\n".join(lines)
        assert "这是什么" in joined
        assert "决策源" in joined
        assert "判据原文" in joined
        assert "你要决定" in joined
        assert "立刻处置" in joined  # NEEDS_USER 项必须给明确动作

    def test_auto_watch_no_action_required(self):
        m = _load_report_mod()
        it = _row("entry_band_widen")  # lexicon cls=AUTO_WATCH
        lines = m._background_block(it)
        assert any("暂无" in ln for ln in lines)

    def test_unknown_id_falls_back_to_registry(self):
        m = _load_report_mod()
        it = _row("totally_unknown_shadow")
        lines = m._background_block(it)
        joined = "\n".join(lines)
        assert "未收录白话名" in joined
        assert "判据原文" in joined

    def test_spec_source_used_when_available(self):
        m = _load_report_mod()
        it = _row("entry_band_widen")
        lines = m._background_block(it)
        assert any("RFC-065" in ln for ln in lines)


class TestArtifactReadoutFix:
    def test_reads_verdict_artifact_not_data_jsonl(self, tmp_path, monkeypatch):
        """RFC-139 修复: artifact=data_path(jsonl) 时过去永远「产物无读数」;
        spec.verdict_artifact 指向 verdict json ⇒ 现在读到真实判分读数。"""
        m = _load_report_mod()
        art = tmp_path / "verdict.json"
        art.write_text(json.dumps({
            "verdict": "NOT_ESTABLISHED_KEEP_SAMPLING", "n": 36, "mean": -17.7}))
        monkeypatch.setattr(m, "REPO", tmp_path)

        class _S:
            verdict_artifact = "verdict.json"
        monkeypatch.setattr(m, "get_spec", lambda sid: _S() if sid == "x" else None)
        out = m._artifact_readout("data/fake_shadow.jsonl", "scripts/fake.py", "x")
        assert "NOT_ESTABLISHED_KEEP_SAMPLING" in out
        assert "n=36" in out
