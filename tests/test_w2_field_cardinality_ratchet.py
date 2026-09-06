"""W2 字段基数棘轮钉子 (2026-09-06, ADR-038 P7 军规自动化, 用户裁"按建议执行")。

病灶: `scripts/shadow_field_cardinality.py` (W0) 能机械判 DEGEN 并 exit 1, 但没有任何
调度方 (crontab / ops 周检 / 日检 全零引用) → "新增落库字段必带基数自检" 军规停在纸面,
常量退化仍只能等人工复盘发现 (已六例: DA conf 罚成 35 / UE conf cap=50 / UE info_source
恒常量 / da_score 塌 6.0 / trigger 计数恒 2 / tripwire 计数恒 2)。

本文件钉住接入 `scripts/daily_invariant_check.py` 后的语义:
  ① 基线外新增 DEGEN → 当天告警 (守卫的真目标 = 证伪力所在);
  ② 基线内已知 DEGEN → silenced_known 计数, 零告警 (存量病灶在修, 不刷不可动作噪声);
  ③ 愈合只报告不自动改写基线 (棘轮只降不升 — 守卫不得自己改判据);
  ④ 采集器异常显性化 ("零违反"与"守卫自己崩了"必须可区分, 同 8/28 D1/D2 课);
  ⑤ 首跑自建基线不告警 (与 W1 bootstrap 同款语义);
  ⑥ n<30 的 INSUFFICIENT 永不进 DEGEN (样本不足不得当塌缩, 否则新字段上线首日即假告警);
  ⑦ 接线钉: 工具的模块级 `_DB` 必须被本脚本按 TRADE_ENV 解析的 DB 覆写 —
     工具默认硬编码 `data/ftseek_us.db`, 切实盘那天会变成"守卫在读陈旧 sim 名单"。
  ⑧ 表/列不存在 = 库形态差异 → skipped_old_schema 静默跳过 (与 W1/I9/I10 同款),
     真故障仍须 W2_detector_error — 两者不得混同。

回滚: 删本文件 + revert 对应提交即可, 零生产代码依赖。
"""
import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "daily_invariant_check", PROJECT_ROOT / "scripts" / "daily_invariant_check.py"
)
dic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dic)

_KNOWN = "da_gate.da_score"
_NEW = "ue_scan.self_tier"

# 接线探针 (⑦): 把注入后的 _DB 与 days 编码进字段名, 由 info["degen"] 透出即可断言
_FAKE_TOOL = '''_DB = "UNINJECTED"


def collect(days):
    return [{"source": "probe", "field": f"{_DB}|{days}", "n": 30, "unique": 1,
             "top_value": "x", "top_share": 1.0, "entropy": 0.0, "verdict": "DEGEN"}]
'''


def _row(source, field, verdict):
    return {"source": source, "field": field, "n": 30, "unique": 1,
            "top_value": "x", "top_share": 1.0, "entropy": 0.0, "verdict": verdict}


def _baseline(tmp_path, degen, refs=None):
    p = tmp_path / "field_cardinality_baseline.json"
    p.write_text(json.dumps({"degen": list(degen), "refs": refs or {}},
                            ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def _run(monkeypatch, tmp_path, results, baseline=None, raises=None):
    monkeypatch.setattr(
        dic, "_FIELD_CARD_BASELINE", baseline or (tmp_path / "absent_baseline.json"))
    hits = []

    def rec(k, rid, sym, msg):
        hits.append((k, str(sym), msg))

    def _collect(days):
        if raises is not None:
            raise raises
        return results

    return dic._check_field_cardinality(rec, _collect=_collect), hits


def test_new_degen_field_alarms_same_night(tmp_path, monkeypatch):
    """①真目标: 基线外新增塌缩字段 → 当晚 W2_new_field_degen (损失封顶当天, 不等人工复盘)。"""
    info, hits = _run(monkeypatch, tmp_path,
                      [_row("da_gate", "da_score", "DEGEN"),
                       _row("ue_scan", "self_tier", "DEGEN")],
                      baseline=_baseline(tmp_path, [_KNOWN]))
    assert info["status"] == "drift"
    assert [h[0] for h in hits] == ["W2_new_field_degen"], "已知病灶不得重复告警"
    assert hits[0][1] == _NEW
    assert "P7" in hits[0][2] and "field_cardinality_baseline.json" in hits[0][2]
    assert info["degen"] == [_KNOWN, _NEW]
    assert info["silenced_known"] == 1


def test_known_degen_silenced_not_noise(tmp_path, monkeypatch):
    """②存量病灶 (六例, 均在修) 只计数不告警 — 否则守卫天天红 = 没人看。"""
    info, hits = _run(monkeypatch, tmp_path,
                      [_row("da_gate", "da_score", "DEGEN"),
                       _row("ue_instruction", "confidence", "OK")],
                      baseline=_baseline(tmp_path, [_KNOWN]))
    assert info["status"] == "ok" and hits == []
    assert info["silenced_known"] == 1 and info["fields_checked"] == 2


def test_healed_reported_and_baseline_not_rewritten(tmp_path, monkeypatch):
    """③棘轮只降不升: 愈合项进 healed 供人工收缩, 守卫绝不自动改写判据文件。"""
    healed_baseline = _baseline(tmp_path, [_KNOWN, "da_gate.tripwire_count"])
    before = healed_baseline.read_bytes()
    info, hits = _run(monkeypatch, tmp_path, [_row("da_gate", "da_score", "DEGEN")],
                      baseline=healed_baseline)
    assert info["status"] == "ok" and hits == []
    assert info["healed"] == ["da_gate.tripwire_count"]
    assert healed_baseline.read_bytes() == before, "基线被自动改写 = 守卫自己改判据"


def test_insufficient_never_degen(tmp_path, monkeypatch):
    """⑥n<30 只记 INSUFFICIENT: 新字段上线首日不得被判塌缩 (假告警会训练人忽略守卫)。"""
    info, hits = _run(monkeypatch, tmp_path,
                      [_row("da_instruction", "score_band", "INSUFFICIENT")],
                      baseline=_baseline(tmp_path, []))
    assert info["status"] == "ok" and hits == []
    assert info["degen"] == []
    assert info["insufficient"] == ["da_instruction.score_band"]


def test_first_run_bootstraps_baseline_without_alarm(tmp_path, monkeypatch):
    """⑤首跑自建基线 (与 W1 bootstrap 同款): 立档当刻的病灶即已知集, 不判漂移。"""
    info, hits = _run(monkeypatch, tmp_path,
                      [_row("da_gate", "da_score", "DEGEN"),
                       _row("da_gate", "decision", "OK")])
    assert info["status"] == "baseline_established" and hits == []
    written = json.loads((tmp_path / "absent_baseline.json").read_text(encoding="utf-8"))
    assert written["degen"] == [_KNOWN] and "established_at" in written
    assert "只降不升" in written["note"], "豁免纪律必须写在判据文件里 (同 config_freeze)"


def test_collector_exception_is_loud(tmp_path, monkeypatch):
    """④守卫自己崩了 ≠ 零违反 (8/28 D1/D2 同课): 采集异常必须落 W2_detector_error。"""
    info, hits = _run(monkeypatch, tmp_path, [], baseline=_baseline(tmp_path, [_KNOWN]),
                      raises=RuntimeError("boom"))
    assert info["status"] == "detector_error"
    assert [h[0] for h in hits] == ["W2_detector_error"]
    assert "不可信" in hits[0][2]


def test_missing_table_is_old_schema_not_detector_failure(tmp_path, monkeypatch):
    """⑧库形态差异 (旧库/最小夹具无 playbook_instructions) → 静默跳过, 不告警。

    钉住 2026-09-06 实测教训: W2 首版把缺表当 detector_error, 把 8 个用最小夹具跑
    main() 的既有测试 (I10/D4/attribution 退役) 全染红。缺表与"守卫崩了"必须分流,
    否则要么噪声淹没真故障, 要么为消噪声把真故障一起吞掉。
    """
    info, hits = _run(monkeypatch, tmp_path, [],
                      raises=Exception("no such table: playbook_instructions"))
    assert info["status"] == "skipped_old_schema"
    assert hits == [], "缺表属库形态差异, 不得告警"


def test_tool_db_path_overridden_by_trade_env_resolver(tmp_path, monkeypatch):
    """⑦接线钉: 工具模块级 _DB 必须被 dic.DB (按 TRADE_ENV 解析) 覆写, days 口径同源。

    证伪力: 若哪天有人把覆写行删了 (或改成 import 后不覆写), 探针字段名会退回
    'UNINJECTED' → 本断言即红, 而不是等到切实盘那天守卫静默读陈旧 sim 库。
    """
    fake = tmp_path / "fake_cardinality_tool.py"
    fake.write_text(_FAKE_TOOL, encoding="utf-8")
    monkeypatch.setattr(dic, "_FIELD_CARD_SCRIPT", fake)
    monkeypatch.setattr(dic, "_FIELD_CARD_BASELINE", _baseline(tmp_path, []))
    hits = []
    info = dic._check_field_cardinality(lambda k, rid, sym, msg: hits.append(k))
    assert info["degen"] == [f"probe.{dic.DB}|{dic._FIELD_CARD_DAYS}"]
    assert info["status"] == "drift" and hits == ["W2_new_field_degen"]
