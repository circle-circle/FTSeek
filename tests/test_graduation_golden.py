"""RFC-138 借鉴② 契约测试: exit_shadow_graduation 9/26 冻结裁决 golden 钉.

背景 (痛点实锤): 回放引擎 (ef5a1cc5d) 在裁决后继续演化, 旧 DEB 数字无法复现 ⇒
be_0.4 裁决作废改走实盘验收。本测试把 2026-09-26 裁决日 (shadow_registry
ADJUDICATED[exit_param_shadow] read_at, N=495 三门裁决) 的**全部聚合读数**钉成
golden fixture, 引擎再演化只有两条路:
  ① 数字不变 (无害改动, 测试绿);
  ② 测试红 ⇒ 显式承认「9/26 裁决不可复现」并触发 era 分层 — 不许悄悄改。

纪律 (与 tests/test_w1_exit_version_guard 先例同形态):
  **改 fixture = 改判据 = 重新裁决** (须 DEB + 用户裁)。裁决语义钉
  (TestAdjudicationSemantics) 防「重生成 fixture 洗白引擎改动」— 重生成必须同时
  推翻语义钉, 而语义钉写死的是 9/26 用户已裁的事实 (BE/DD 全 FAIL / dead_signal
  唯二 PASS / N=495), 推翻它 = 推翻裁决。

数据依赖: 冻结窗 = 生产 append-only 文件 data/shadow/exit_param_shadow.jsonl 中
ts≤2026-09-26 的行。裸 worktree / CI 无 data/ ⇒ skip (显式理由; 按工程变更规范
「skip≠绿」纪律, 等基回归时人工核活仓侧真跑)。
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "exit_shadow_graduation_golden_20260926.json"
SHADOW = ROOT / "data" / "shadow" / "exit_param_shadow.jsonl"
GRAD_SCRIPT = ROOT / "scripts" / "exit_shadow_graduation.py"
FROZEN_CUTOFF = "2026-09-26"   # ADJUDICATED read_at (SSOT: shadow_registry)
FROZEN_N = 495                 # 9/26 裁决 note 逐字: "N=495"


def _load_grad():
    spec = importlib.util.spec_from_file_location("grad_golden", GRAD_SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def golden():
    if not FIXTURE.exists():
        pytest.fail(f"golden fixture 缺失: {FIXTURE} (删除 = 拆钉子, 须 DEB+用户裁)")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def recomputed(golden):
    """用现役引擎在冻结窗上重跑四族聚合 (jsonl append-only ⇒ 冻结窗恒定)."""
    if not SHADOW.exists():
        pytest.skip(f"生产数据不可用 ({SHADOW}) — 裸 worktree/CI; 按「skip≠绿」纪律人工核活仓")
    m = _load_grad()
    records = m._load_records(0)
    frozen = [r for r in records if str(r.get("ts", ""))[:10] <= FROZEN_CUTOFF]
    return {
        "m": m,
        "frozen_n": len(frozen),
        "exit_variants": m._assess_exit_variants(frozen),
        "dead_signal": m._assess_dead_signal(frozen),
        "abort_30m": m._assess_counterfactual_variants(frozen, "abort_30m", "ABORT_30M_VARIANTS"),
        "early_tight": m._assess_counterfactual_variants(frozen, "early_tight", "EARLY_TIGHT_VARIANTS"),
    }


class TestFrozenWindowIntegrity:
    """冻结窗完整性: append-only 生产文件的裁决窗不得被改写/清洗."""

    def test_frozen_n_exact(self, recomputed):
        # 行数变化 = jsonl 冻结窗被改写 (append-only 违约) = 篡改探测, 直接红
        assert recomputed["frozen_n"] == FROZEN_N, (
            f"冻结窗 N={recomputed['frozen_n']} != {FROZEN_N} — "
            "exit_param_shadow.jsonl 的 ts≤9/26 行被改写/清洗? append-only 违约, "
            "9/26 裁决 (shadow_registry ADJUDICATED) 的证据基础已不可复现"
        )


class TestBitForBitReproduction:
    """逐位复现钉: 现役引擎 × 冻结窗 ⇒ 与 golden fixture 全量相等."""

    @pytest.mark.parametrize("family", ["exit_variants", "dead_signal", "abort_30m", "early_tight"])
    def test_family_matches_golden(self, golden, recomputed, family):
        assert recomputed[family] == golden[family], (
            f"{family} 与 9/26 golden 不一致 — 引擎数值路径被改。处置二选一: "
            "①证明变化无害并在 RFC-138 留痕; ②承认旧裁决不可复现, 触发 era 分层 "
            "(登记新指纹 + 旧裁决标注适用性未验)。直接重生成 fixture = 改判据 = 须 DEB+用户裁"
        )


class TestAdjudicationSemantics:
    """裁决语义钉: 防「重生成 fixture 洗白引擎改动」— 这些是 9/26 用户已裁事实."""

    def test_be_dd_family_all_fail(self, golden):
        variants = golden["exit_variants"]["variants"]
        assert len(variants) == 5
        assert all(v["status"] == "FAIL" for v in variants.values()), \
            "9/26 裁决: BE/DD 族 5 变体全 FAIL — 语义变化 = 推翻已裁事实"

    def test_dead_signal_only_pass(self, golden):
        ds = golden["dead_signal"]["variants"]
        assert set(ds) == {"dead_signal_60m_mfe0.2", "dead_signal_60m_mfe0.3"}
        assert all(v["status"] == "PASS" for v in ds.values())
        # 分离度逐字 (9/26 note: +1.212pp / +1.157pp, fixture 生成时引擎读数 1.2117/1.1568)
        assert ds["dead_signal_60m_mfe0.2"]["separation_pp"] == pytest.approx(1.2117, abs=5e-4)
        assert ds["dead_signal_60m_mfe0.3"]["separation_pp"] == pytest.approx(1.1568, abs=5e-4)

    def test_abort_and_early_tight_fail(self, golden):
        assert golden["abort_30m"]["variants"]["abort_30m_mfe0.1_loss"]["status"] == "FAIL"
        et = golden["early_tight"]["variants"]
        assert len(et) == 5
        assert all(v["status"] == "FAIL" for v in et.values())

    def test_baseline_n(self, golden):
        assert golden["exit_variants"]["baseline_n"] == 437
        assert golden["frozen_n"] == FROZEN_N

    def test_era_fingerprint_recorded(self, golden):
        """fixture 记录裁决时代指纹 = exit-34c7153088a1 (9/17 起现役, 9/26 裁决同代)."""
        assert golden["_meta"]["era_fingerprint"] == "exit-34c7153088a1"


class TestGraduationScriptEraBlock:
    """graduation 脚本 era 块 (RFC-138 借鉴① 在本脚本的接线)."""

    def test_era_block_live_no_drift(self):
        """活体: era 块对 9/26 裁决日指纹与现役指纹的判定 (2026-10-09 重钉).

        原钉 then==current (9/26 至今零漂移) 已于 10-08 失效: 410297295 (RFC-106 §5.2)
        把 overnight_gap_stop_pct=3.0 同值显式化写入 yaml, 整文件哈希副作用 ⇒
        现役 exit-ca4487172a6f。逐提交二分归因证实**零行为变更** (隔离 worktree
        9/26 钉值精确复现), 豁免已登记 config_freeze_baseline.json (用户裁「继续」)。
        本条转为**阳性对照**: era 守卫必须检出指纹不等 (applies=False) —— 检出=有牙齿;
        行为等价声明在豁免台账, era 模块按设计只做指纹比对 (RFC-138: 只标注不降级)。
        """
        if not SHADOW.exists():
            pytest.skip("生产数据不可用 (裸 worktree)")
        m = _load_grad()
        era = m._era_block()
        assert "error" not in era, f"era fail-open 被触发: {era.get('error')}"
        from app.playbook.shadow_registry import ADJUDICATED
        assert era["adjudicated_at"] == ADJUDICATED["exit_param_shadow"]["read_at"], \
            "裁决日必须从 ADJUDICATED SSOT 读, 不得漂移/硬编码"
        from app.review.verdict_era import era_of_date
        expected_then = era_of_date(ADJUDICATED["exit_param_shadow"]["read_at"])["version"]
        assert era["then"] == expected_then, "裁决日指纹必须由 DB 按 read_at 推导, 不得硬编码"
        assert era["current"] == "exit-ca4487172a6f"
        assert era["applies"] is (era["then"] == era["current"]), \
            "applies 必须与 then/current 指纹比较一致"

    def test_era_block_fail_open(self, monkeypatch):
        """指纹查询炸 ⇒ fail-open 在 era_status 内部消化: applies=None + note 注明失败,
        _era_block 返回正常结构不抛异常 (报告照常, 三门判定不受影响)."""
        m = _load_grad()
        import app.review.verdict_era as ve
        monkeypatch.setattr(ve, "_current_fingerprint",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        era = m._era_block()
        assert era["applies"] is None
        assert "era 查询失败" in era["note"]
        from app.playbook.shadow_registry import ADJUDICATED
        assert era["adjudicated_at"] == ADJUDICATED["exit_param_shadow"]["read_at"]
