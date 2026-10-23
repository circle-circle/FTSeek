"""RFC-139 复查修复批契约测试 (2026-10-08, 用户裁「继续」路径1).

Hermes 独立复查他 agent 的 RFC-139 未提交批, 发现 4 处缺陷 (全部 stub/DB 实证),
本文件钉住修复。**独立成文**而非并入 test_auto_adjudicate.py: ①避免与原作者
可能继续编辑的文件并行冲突; ②这批语义是「复查修复」, 与 RFC-139 原批分开留痕。

P1  eth_relative_volume_gate 假达标 (count_mode=lines 把全部拒单采样行计入,
    实测 53 行 would_pass 全 None ⇒ 真轴 0/30) + spec.sample_gates write-only 死门
P2  auto_adjudicate read-only 模式对「脚本不存在」假称「能跑」(COLLECTOR_ONLY)
P3  render 把 era fail-open(applies=None) 冒充「未裁项」(违反 RFC-138 尺子坏了不冒充判定)
P4  entry_band_widen 10/04 已裁 NOT_ESTABLISHED_KEEP_SAMPLING 却未登记 ADJUDICATED
    ⇒ needs_adjudication 恒 True, 每周假抬进①段 (标题「需要你决定」vs 背景「暂无」自相矛盾)
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_runner():
    spec = importlib.util.spec_from_file_location("aa_fixes", ROOT / "scripts" / "auto_adjudicate.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _load_report():
    spec = importlib.util.spec_from_file_location(
        "par_fixes", ROOT / "scripts" / "pending_adjudication_report.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── P1a: eth count_mode 同轴 (注册表 SSOT 钉) ──────────────────────────────
class TestP1CountModeSameAxis:
    def test_eth_count_mode_is_would_pass_axis(self):
        """判据 n 轴 = would_pass_relative 样本, count_mode 必须同轴 (不得回退 lines)."""
        from app.playbook.shadow_registry import REGISTRY
        item = next(it for it in REGISTRY if it.id == "eth_relative_volume_gate")
        assert item.count_mode == "where:would_pass_relative=True", (
            "count_mode 与判据 n 轴不同轴 = 假达标根因复发 "
            "(判据: would_pass_relative 样本 n≥30; lines 会把 turnover_too_low 采样行计入)")

    def test_eth_real_axis_not_falsely_ready(self):
        """真轴 (would_pass 样本) 未达门 ⇒ 不得虚假迁移 READY_TO_EVALUATE.

        依赖生产 jsonl (53 行全 would_pass=None ⇒ n=0); 裸 worktree 无 data/ ⇒ skip。
        """
        shadow = ROOT / "data" / "shadow" / "eth_relative_volume_gate_shadow.jsonl"
        if not shadow.exists():
            pytest.skip(f"生产数据不可用 ({shadow}) — 裸 worktree; 按「skip≠绿」纪律人工核活仓")
        from app.playbook import shadow_registry as sr
        sr.refresh_counts()
        item = next(it for it in sr.REGISTRY if it.id == "eth_relative_volume_gate")
        # 真轴: 数 would_pass_relative=True 的行
        rows = [json.loads(l) for l in shadow.read_text().splitlines() if l.strip()]
        wp = [r for r in rows if r.get("would_pass_relative") is True]
        assert item.current_n == len(wp), (
            f"current_n={item.current_n} != would_pass 真轴 {len(wp)} — count_mode 未按判据轴计数")
        if len(wp) < item.min_n:
            # 2026-10-23 RFC-162: 退役态豁免 — KILLED/SUPERSEDED 不是「READY」, 语义上不构成
            # 本守卫要抓的假达标。收窄为「未退役者必须停在 OBSERVING」, 并新增反向不变量:
            # 已退役者的状态只能是终态 (防「退役了却还挂在观测面」)。
            if item.collection_retired:
                assert item.status.value in ("KILLED", "SUPERSEDED"), (
                    f"已退役 (collection_retired=True) 却 status={item.status.value} — "
                    f"退役态只能是 KILLED/SUPERSEDED")
            else:
                assert item.status.value == "OBSERVING", (
                    f"真轴 {len(wp)}/{item.min_n} 未达门却 status={item.status.value} — 假达标复发")


# ── P1b: spec.sample_gates 接线成消费面 (死门变活) ─────────────────────────
class TestP1SampleGatesWired:
    def test_gate_line_renders_spec_gates(self):
        """sample_gates 此前 write-only (evaluate 只透传不呈报); 修复后 _gate_line 接线."""
        runner = _load_runner()
        spec_d = {"sample_gates": {"trading_days_min": 15, "would_pass_n_min": 30,
                                   "deadline": "2026-11-30"}}
        line = runner._gate_line({"n": 0}, spec_d)
        assert "trading_days_min=15" in line
        assert "would_pass_n_min=30" in line
        assert "注册面 n=0" in line
        assert "未达" in line, "真轴 0 < 30 门 ⇒ 必须如实标未达"
        assert "deadline=2026-11-30" in line

    def test_gate_line_empty_when_no_gates(self):
        runner = _load_runner()
        assert runner._gate_line({"n": 5}, None) == ""
        assert runner._gate_line({"n": 5}, {"sample_gates": {}}) == ""
        assert runner._gate_line({"n": 5}, {"sample_gates": {"note": "无 int 门"}}) == ""

    def test_eth_spec_gates_are_real_axis(self):
        """eth spec.sample_gates 必须是真判据轴 (would_pass_n / trading_days), 非行数."""
        from app.review.criteria_spec import get_spec
        spec = get_spec("eth_relative_volume_gate")
        assert spec is not None
        assert spec.sample_gates.get("would_pass_n_min") == 30
        assert spec.sample_gates.get("trading_days_min") == 15


# ── P2: read-only 不假称「能跑」─────────────────────────────────────────────
class TestP2ScriptMissing:
    def test_readonly_missing_script_reports_script_missing(self):
        """脚本不存在 ⇒ read-only 也必须如实报 SCRIPT_MISSING, 不冒充 COLLECTOR_ONLY('能跑')."""
        runner = _load_runner()
        row = {"id": "ghost", "status": "READY_TO_EVALUATE", "n": 5, "min_n": 30,
               "needs_adjudication": True, "criteria": "c",
               "eval_script": "scripts/__DEFINITELY_NOT_EXIST_99887__.py", "verdict_era": None}
        out = runner.evaluate([row])  # read-only (默认)
        assert out["items"][0]["classification"] == "SCRIPT_MISSING", (
            "read-only 对不存在脚本判 COLLECTOR_ONLY = 假陈述「能跑」")
        text = runner.render(out)
        assert "eval_script 文件不存在" in text
        assert "能跑但不产 verdict" not in text, "不得再印「能跑」(它从没跑过)"

    def test_readonly_existing_script_still_collector_only(self, tmp_path, monkeypatch):
        """脚本存在但无产物 ⇒ 仍 COLLECTOR_ONLY (回归保护, 不误伤)."""
        runner = _load_runner()
        sc = tmp_path / "scripts" / "real_eval.py"
        sc.parent.mkdir(parents=True)
        sc.write_text("# collector\n")
        monkeypatch.setattr(runner, "REPO", tmp_path)
        row = {"id": "x", "status": "READY_TO_EVALUATE", "n": 5, "min_n": 30,
               "needs_adjudication": True, "criteria": "c",
               "eval_script": "scripts/real_eval.py", "verdict_era": None}
        out = runner.evaluate([row])
        assert out["items"][0]["classification"] == "COLLECTOR_ONLY"


# ── P3: era fail-open 不冒充「未裁项」───────────────────────────────────────
class TestP3EraFailOpenNotMisreported:
    def test_era_none_renders_undeterminable_not_unadjudicated(self):
        """applies=None (era 查询失败) ⇒ 印「不可判定」+ note, 不得印「未裁项」(RFC-138 纪律)."""
        runner = _load_runner()
        row = {"id": "x", "status": "READY_TO_EVALUATE", "n": 5, "min_n": 30,
               "needs_adjudication": True, "criteria": "c", "eval_script": None,
               "verdict_era": {"then": None, "now": "", "applies": None,
                               "note": "era 查询失败 (RuntimeError: boom) — 不作适用性判定"}}
        text = runner.render(runner.evaluate([row]))
        assert "era: ⏳ 不可判定" in text
        assert "boom" in text or "查询失败" in text
        assert "era: 未裁项" not in text, "fail-open 态被冒充未裁 = 掩盖尺子坏了"

    def test_era_absent_still_unadjudicated(self):
        """verdict_era is None (真未裁) ⇒ 仍印「未裁项」(回归保护)."""
        runner = _load_runner()
        row = {"id": "x", "status": "READY_TO_EVALUATE", "n": 5, "min_n": 30,
               "needs_adjudication": True, "criteria": "c", "eval_script": None,
               "verdict_era": None}
        text = runner.render(runner.evaluate([row]))
        assert "era: 未裁项" in text


# ── P4: entry_band_widen 假待裁修复 ────────────────────────────────────────
class TestP4EntryBandWidenNotFakePending:
    def test_entry_band_widen_adjudicated_registered(self):
        """10/04 已裁读数必须收进 ADJUDICATED SSOT (否则每周假抬①段)."""
        from app.playbook.shadow_registry import ADJUDICATED
        assert "entry_band_widen" in ADJUDICATED
        rec = ADJUDICATED["entry_band_widen"]
        assert rec["read_at"] == "2026-10-04"
        assert rec["needs_user"] is False
        # note 双锚 (test_adjudicated_note_anchor 守卫): script + doc 出处
        assert "settle_entry_band_widen.py" in rec["note"]
        assert "RFC-065" in rec["note"]

    def test_entry_band_widen_not_needs_adjudication(self):
        """已裁 + needs_user=False ⇒ 退出「需要你决定」待裁面 (消除标题/背景自相矛盾)."""
        shadow = ROOT / "data" / "entry_band_widen_shadow.jsonl"
        if not shadow.exists():
            pytest.skip("生产数据不可用 (裸 worktree)")
        from app.review.criteria_status import pending_adjudications
        rows = pending_adjudications()
        ebw = next((r for r in rows if r["id"] == "entry_band_widen"), None)
        assert ebw is not None
        assert ebw["needs_adjudication"] is False, (
            "entry_band_widen 已裁 NOT_ESTABLISHED_KEEP_SAMPLING 却仍在待裁面 = P4 假待裁复发")

    def test_lexicon_eth_no_false_pass_text(self):
        """lexicon eth conclusion 不得残留旧 lines 口径「53/30 达门」假陈述."""
        from app.review.shadow_lexicon import SHADOW_LEXICON
        conc = SHADOW_LEXICON["eth_relative_volume_gate"]["conclusion"]
        assert "53/30 达门" not in conc, "count_mode 修复后 lexicon 仍印旧假达门口径 = 文本漂移"
        assert "0/30" in conc or "would_pass" in conc, "须反映真轴"
