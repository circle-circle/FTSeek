"""W1 冻结守卫两维口径钉子 (2026-09-03 复盘批, REVIEW-20260903 §四.1).

病灶: `config_version` 在本仓有两个语义 — trade_records 的**列**是配置档案版本 (恒 v2),
metadata_json.config_version 才是**退出参数哈希** (exit-*, KR2 判分真口径)。原守卫只查列
→ 8/24 起退出参数换了 4 版而日级守卫恒 "ok", 只有周六周检查得出来 (损失封顶从"当天"
退化为"最多 6 天")。本文件钉住: ①退出哈希维必须存在且以 exit_versions 豁免清单对账;
②档案列维原行为不得回退; ③无哈希行只计数不当版本 (防首跑假漂移)。

回滚: 删本文件 + revert 对应提交即可, 零生产代码依赖。
"""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "daily_invariant_check", PROJECT_ROOT / "scripts" / "daily_invariant_check.py"
)
dic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dic)

_COLS = ("id INTEGER PRIMARY KEY, symbol TEXT, source_type TEXT, trade_date TEXT, "
         "config_version TEXT, metadata_json TEXT")

_EXEMPT = "exit-aaaaaaaaaaaa"
_NEW = "exit-bbbbbbbbbbbb"


def _mk_db(tmp_path):
    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(f"CREATE TABLE trade_records ({_COLS})")
    conn.commit()
    conn.close()
    return db


def _row(db, *, profile="v2", exit_hash=_EXEMPT, n=1, day="2026-09-02"):
    conn = sqlite3.connect(str(db))
    meta = "{}" if exit_hash is None else json.dumps({"config_version": exit_hash})
    for i in range(n):
        conn.execute(
            "INSERT INTO trade_records (symbol, source_type, trade_date, config_version, "
            "metadata_json) VALUES (?,?,?,?,?)",
            (f"US.T{i}", "PLAYBOOK_SIM", day, profile, meta))
    conn.commit()
    conn.close()


def _baseline(tmp_path, *, versions=("v2",), exit_versions=(_EXEMPT,)):
    p = tmp_path / "config_freeze_baseline.json"
    p.write_text(json.dumps({
        "versions": list(versions),
        "exit_versions": {v: "测试豁免说明 (裁决留痕)" for v in exit_versions},
    }, ensure_ascii=False), encoding="utf-8")
    return p


def _run(tmp_path, monkeypatch, db, baseline):
    monkeypatch.setattr(dic, "DB", db)
    monkeypatch.setattr(dic, "_FREEZE_BASELINE", baseline)
    hits = []

    def rec(k, rid, sym, msg):
        hits.append((k, str(sym), msg))

    return dic._check_config_freeze(rec), hits


def test_unexempted_exit_hash_alarms(tmp_path, monkeypatch):
    """KR2 真口径: 冻结窗内出现未豁免的退出参数哈希 → 当天告警 (不等周六周检)。"""
    db = _mk_db(tmp_path)
    _row(db, exit_hash=_EXEMPT, n=3)
    _row(db, exit_hash=_NEW, n=2)
    info, hits = _run(tmp_path, monkeypatch, db, _baseline(tmp_path))
    assert info["exit_status"] == "drift"
    assert info["status"] == "drift", "两维取劣 — 退出维漂移不得被档案维 ok 掩盖"
    assert info["exit_versions"] == {_EXEMPT: 3, _NEW: 2}, "每哈希笔数=cohort 样本量, 须可见"
    assert [h[0] for h in hits] == ["W1_exit_version_drift"]
    assert _NEW in hits[0][1] and "豁免" in hits[0][2]


def test_all_exit_hashes_exempted_is_ok(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    _row(db, exit_hash=_EXEMPT, n=4)
    info, hits = _run(tmp_path, monkeypatch, db, _baseline(tmp_path))
    assert info["exit_status"] == "ok" and info["status"] == "ok"
    assert info["exit_versions"] == {_EXEMPT: 4}
    assert hits == []


def test_rows_without_exit_hash_are_counted_not_versioned(tmp_path, monkeypatch):
    """无哈希行 (旧行/写入侧未落 metadata) 只计数 — 当作版本会让首跑即假漂移。"""
    db = _mk_db(tmp_path)
    _row(db, exit_hash=_EXEMPT, n=2)
    _row(db, exit_hash=None, n=3)
    info, hits = _run(tmp_path, monkeypatch, db, _baseline(tmp_path))
    assert info["exit_hash_missing_rows"] == 3
    assert info["exit_versions"] == {_EXEMPT: 2}
    assert info["exit_status"] == "ok" and hits == []


def test_profile_column_dimension_not_regressed(tmp_path, monkeypatch):
    """回归红线: 档案列维原行为 (基线比对 + W1_config_version_drift) 一行不改。"""
    db = _mk_db(tmp_path)
    _row(db, profile="v3", exit_hash=_EXEMPT)
    info, hits = _run(tmp_path, monkeypatch, db, _baseline(tmp_path))
    assert info["profile_status"] == "drift"
    assert info["exit_status"] == "ok"
    assert info["status"] == "drift"
    assert [h[0] for h in hits] == ["W1_config_version_drift"]


def test_no_rows_in_window_unchanged(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    info, hits = _run(tmp_path, monkeypatch, db, _baseline(tmp_path))
    assert info["status"] == "no_rows_in_window" and hits == []


def test_baseline_bootstrap_unchanged(tmp_path, monkeypatch):
    """无基线文件 → 自建基线并声明 baseline_established (原语义, 不判漂移)。"""
    db = _mk_db(tmp_path)
    _row(db, exit_hash=_NEW)
    missing = tmp_path / "absent_baseline.json"
    monkeypatch.setattr(dic, "DB", db)
    monkeypatch.setattr(dic, "_FREEZE_BASELINE", missing)
    info = dic._check_config_freeze(lambda *a: None)
    assert info["status"] == "baseline_established"
    assert json.loads(missing.read_text(encoding="utf-8"))["versions"] == ["v2"]


def test_source_pins_metadata_hash_caliber():
    """源级契约: 退出维必须读 metadata_json 哈希 (防被"简化"回只读列 → 尺子再度失明)。"""
    src = (PROJECT_ROOT / "scripts" / "daily_invariant_check.py").read_text(encoding="utf-8")
    assert "json_extract(metadata_json,'$.config_version')" in src
    assert "W1_exit_version_drift" in src
    assert 'baseline.get("exit_versions")' in src


def test_exempt_but_unobserved_hashes_are_visible_not_alarmed(tmp_path, monkeypatch):
    """幽灵豁免 (在册零笔) 只作信息级可见 — "先登记后上线"是合法顺序, 不得告警。"""
    db = _mk_db(tmp_path)
    _row(db, exit_hash=_EXEMPT, n=2)
    info, hits = _run(tmp_path, monkeypatch, db,
                      _baseline(tmp_path, exit_versions=(_EXEMPT, _NEW)))
    assert info["exit_exempt_unobserved"] == [_NEW]
    assert info["exit_status"] == "ok" and hits == []
