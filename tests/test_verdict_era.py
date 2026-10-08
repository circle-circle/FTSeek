"""RFC-138 借鉴① 契约测试: app/review/verdict_era.py 裁决参数指纹 (era 适用性).

钉住六条路径 (预注册验收判据 A1, docs/decisions/RFC-138-*.md §4):
  1. 多数派: 同日多版本取笔数多数派并标 ambiguous;
  2. 回溯: 无数据日向前找最近有数据日 (≤30 天);
  3. pre-era: 早于打标起点 2026-07-20 ⇒ version=None + 显式 unknown_reason (不臆造);
  4. 漂移标注: then != now ⇒ applies=False + note 含两代指纹;
  5. 同代: then == now ⇒ applies=True;
  6. fail-open: 指纹查询异常 ⇒ applies=None (尺子坏了不冒充判定, 也不抛给调用方)。

不依赖生产 DB / 生产配置: era_map 与 _current_fingerprint 全部 monkeypatch 打桩
(生产数据无漂移样本 — 9/26 至今零改版 ⇒ 漂移路径只能用桩验证, 属 RFC-138 §4 A4
预注册的「stub 单测覆盖 ⚠️ 路径」, 非假数据演活体)。
"""
import pytest

from app.review import verdict_era as ve


# ── 打桩 era_map (模拟生产 DB 推导结果的结构) ──────────────────────────────
_FAKE_MAP = {
    "2026-08-24": {"version": "exit-aaa111", "ambiguous": False, "n": 10},
    "2026-08-27": {"version": "exit-bbb222", "ambiguous": True, "n": 12},   # 改版日多数派
    "2026-09-26": {"version": "exit-34c7153088a1", "ambiguous": False, "n": 9},
}


@pytest.fixture()
def stub_map(monkeypatch):
    monkeypatch.setattr(ve, "era_map", lambda: dict(_FAKE_MAP))


def test_era_of_date_majority_and_ambiguous(stub_map):
    """同日多版本 ⇒ 多数派 + ambiguous 披露 (不隐瞒)."""
    r = ve.era_of_date("2026-08-27")
    assert r["version"] == "exit-bbb222"
    assert r["ambiguous"] is True


def test_era_of_date_lookback(stub_map):
    """无数据日 (周末/假日) ⇒ 向前回溯到最近有数据日, as_of 记实际映射日."""
    r = ve.era_of_date("2026-08-30")  # 周日, 8/28-8/30 无行
    assert r["version"] == "exit-bbb222"
    assert r["as_of"] == "2026-08-27"
    assert r["unknown_reason"] == ""


def test_era_of_date_pre_tagging_epoch_explicit_unknown(stub_map):
    """早于打标起点 ⇒ version=None + 显式 unknown (禁止臆造旧时代指纹)."""
    r = ve.era_of_date("2026-06-15")
    assert r["version"] is None
    assert "打标起点" in r["unknown_reason"]


def test_era_of_date_empty_and_bad_input(stub_map):
    assert ve.era_of_date("")["version"] is None
    r = ve.era_of_date("not-a-date")
    assert r["version"] is None
    assert "非法日期" in r["unknown_reason"]


def test_era_of_date_lookback_exhausted(stub_map):
    """打标起点后但 30 天内无任何数据 ⇒ unknown, 不无限回溯."""
    r = ve.era_of_date("2026-08-20")  # 向前 30 天无 _FAKE_MAP 命中且未到 epoch
    assert r["version"] is None
    assert "回溯" in r["unknown_reason"]


def test_era_status_same_era_applies_true(stub_map, monkeypatch):
    monkeypatch.setattr(ve, "_current_fingerprint", lambda: "exit-34c7153088a1")
    st = ve.era_status("2026-09-26")
    assert st["applies"] is True
    assert st["then"] == st["now"] == "exit-34c7153088a1"
    assert "⚠" not in st["note"]


def test_era_status_drift_applies_false(stub_map, monkeypatch):
    """核心路径: 裁决于旧指纹时代, 现役已漂移 ⇒ applies=False + note 含两代指纹."""
    monkeypatch.setattr(ve, "_current_fingerprint", lambda: "exit-34c7153088a1")
    st = ve.era_status("2026-08-25")  # 映射到 exit-aaa111 时代
    assert st["applies"] is False
    assert st["then"] == "exit-aaa111"
    assert "exit-34c7153088a1" in st["note"]
    assert "适用性未验" in st["note"]


def test_era_status_unknown_read_at_applies_none(stub_map, monkeypatch):
    monkeypatch.setattr(ve, "_current_fingerprint", lambda: "exit-34c7153088a1")
    st = ve.era_status("2026-05-01")  # pre-era
    assert st["applies"] is None
    assert "不作适用性判定" in st["note"]


def test_era_status_fail_open_on_exception(stub_map, monkeypatch):
    """指纹查询炸了 ⇒ applies=None + note 注明失败, 不抛异常给呈现面 (fail-open)."""
    def _boom():
        raise RuntimeError("config exploded")
    monkeypatch.setattr(ve, "_current_fingerprint", _boom)
    st = ve.era_status("2026-09-26")
    assert st["applies"] is None
    assert "era 查询失败" in st["note"]


def test_era_status_batch_shares_map(stub_map, monkeypatch):
    """_emap 注入: 批量调用不重复查 DB (N 条记录 1 次查询)."""
    monkeypatch.setattr(ve, "_current_fingerprint", lambda: "exit-34c7153088a1")
    emap = ve.era_map()
    calls = {"n": 0}
    real_map = ve.era_map

    def counting_map():
        calls["n"] += 1
        return real_map()

    monkeypatch.setattr(ve, "era_map", counting_map)
    for d in ("2026-09-26", "2026-08-25", "2026-08-27"):
        ve.era_status(d, emap)
    assert calls["n"] == 0, "传入 _emap 后仍查 era_map = 批量路径失效"


# ── criteria_status 接线钉 (verdict_era 字段必须出现在 rows, 未裁条目=None) ──
def test_pending_rows_carry_verdict_era_key(monkeypatch):
    """pending_adjudications 每行含 verdict_era 键; 已裁条目为 dict (打桩注册表)."""
    from app.review import criteria_status as cs
    from app.playbook import shadow_registry as sr

    fake_item = sr.ShadowItem(
        id="fake_shadow", name="f", status=sr.ShadowStatus.READY_TO_EVALUATE,
        description="d", data_path="data/x.jsonl", min_n=30, current_n=40,
    )
    monkeypatch.setattr(cs, "REGISTRY", [fake_item])
    monkeypatch.setattr(cs, "refresh_counts", lambda: None)
    monkeypatch.setattr(sr, "ADJUDICATED", {"fake_shadow": {"read_at": "2026-09-26"}})
    monkeypatch.setattr(ve, "era_map", lambda: dict(_FAKE_MAP))
    monkeypatch.setattr(ve, "_current_fingerprint", lambda: "exit-34c7153088a1")

    rows = cs.pending_adjudications()
    assert len(rows) == 1
    era = rows[0]["verdict_era"]
    assert isinstance(era, dict)
    assert era["applies"] is True

    # 未裁条目 ⇒ verdict_era is None (无旧结论可复活, 不查询)
    monkeypatch.setattr(sr, "ADJUDICATED", {})
    rows2 = cs.pending_adjudications()
    assert rows2[0]["verdict_era"] is None


def test_era_line_renders_only_on_drift():
    """呈现面契约: ⚠️ 行只在 applies=False 时出现 (True/None/缺键 全静默)."""
    import importlib.util
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / "scripts" / "pending_adjudication_report.py"
    spec = importlib.util.spec_from_file_location("par_test", script)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    assert m._era_line({"applies": True, "then": "exit-a", "note": "ok"}) == ""
    assert m._era_line({"applies": None, "note": "unknown"}) == ""
    assert m._era_line(None) == ""
    assert m._era_line({}) == ""
    drift = m._era_line({"applies": False, "then": "exit-old",
                         "note": "参数时代漂移: 裁决于 exit-old, 现役 exit-new"})
    assert "⚠️" in drift and "exit-old" in drift and "RFC-138" in drift
