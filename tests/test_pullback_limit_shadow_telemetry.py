"""[DEB-20260901 Phase B] pullback_limit_shadow 遥测 dict 兼容契约测试.

2026-09-01 首日全灭事故 (用户裁"立即修复"): db._row_to_dict (db.py) 自动把
raw_instruction_json 解析成 dict 进入 pipeline, 而 Phase B 三处新代码假设 str:
  ① trigger 影子行 json.loads(dict) TypeError 被 except 静默吞 → archetype 恒
     None → data/shadow/pullback_limit_shadow.jsonl 零落盘 (首日样本全灭)
  ② chase_cap 违例遥测 json.loads(dict) TypeError → 每轮 re-price 刷 warning
  ③ 触发特性影子 source 兜底同型 (fail-soft, 字段丢失)
本文件锁住: pipeline 不得再对 inst.get("raw_instruction_json") 直接 json.loads,
解析一律走 entry_routing.parse_raw_semantics (原生兼容 str|dict)。

2026-09-02 追加 (DEB-20260902-mfe-caliber-and-shadow-zero 议题二): replay 假零口径 —
影子未采集 (文件缺失/当日零行) 时 n_chase_cap_violated 必须 null 而非 0,
行级 chase_cap_violated 无影子行时 null 而非 False ("未采集"≠"采到零违例")。
"""
import importlib.util
import json
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "app" / "playbook" / "pipeline.py"
_REPLAY = Path(__file__).resolve().parents[1] / "scripts" / "cf_pullback_limit_replay.py"


def _src() -> str:
    return _SRC.read_text(encoding="utf-8")


def test_no_direct_json_loads_on_raw_instruction_json():
    s = _src()
    assert 'json.loads(inst.get("raw_instruction_json")' not in s, (
        "raw_instruction_json 经 _row_to_dict 已是 dict, 直接 json.loads 抛 "
        "TypeError (2026-09-01 首日遥测全灭根因); 应改用 "
        "entry_routing.parse_raw_semantics"
    )


def test_chase_cap_telemetry_uses_parse_raw_semantics():
    s = _src()
    idx = s.find("chase_cap遥测异常")
    assert idx > 0, "chase_cap 遥测异常兜底日志必须存在"
    # 窗口 2500→4200 (2026-09-03): DEB-20260903 议题一把 warning 移出去重门 +
    #   留痕注释, 块体变长, parse_raw_semantics 仍在同块内更上游.
    # 窗口 4200→6500 (2026-09-04): P0-A enforce 分支 (终态跳过三件套) 嵌入
    #   遥测写盘之后, 块体再次变长.
    block = s[max(0, idx - 6500):idx]
    assert "parse_raw_semantics" in block, (
        "chase_cap 遥测块必须用 parse_raw_semantics 解析 (dict 兼容)"
    )


def test_trigger_shadow_row_uses_parse_raw_semantics():
    s = _src()
    idx = s.find("[archetype影子]")
    assert idx > 0, "trigger 影子落盘日志标记必须存在"
    block = s[max(0, idx - 3000):idx]
    assert "parse_raw_semantics" in block, (
        "trigger 影子行必须用 parse_raw_semantics 解析 (dict 兼容, 防静默零落盘)"
    )


def test_trigger_shadow_success_log_no_undefined_var():
    """2026-09-02 修复: 成功日志行引用未定义 _raw_pb → NameError 被 except 吞成
    '影子样本丢失' 假警告 (样本实际已落盘 — 写盘在日志行之前, 21:37 GTLB/DELL/
    RIVN/BWXT 四条假警告实证)。锁住: 该块日志只能引用在场的 _sem_pb。"""
    s = _src()
    idx = s.find("[archetype影子]")
    assert idx > 0, "trigger 影子落盘日志标记必须存在"
    block = s[max(0, idx - 3000):idx + 500]
    assert "_raw_pb" not in block, (
        "trigger 影子成功日志不得引用未定义变量 _raw_pb (2026-09-02 NameError "
        "假警告根因); 应引用解析结果 _sem_pb"
    )
    assert "_sem_pb.get('confirmation')" in block, (
        "确认位日志应引用 _sem_pb 的 confirmation 字段"
    )


def test_source_fallback_isinstance_guard():
    s = _src()
    idx = s.find('_raw = inst.get("raw_instruction_json")')
    assert idx > 0, "source 兜底读取必须存在"
    block = s[idx:idx + 300]
    assert "isinstance(_raw, str)" in block, (
        "source 兜底必须先判 isinstance(str) 再 json.loads (dict 直通)"
    )


# ========== replay 假零口径契约 (2026-09-02, DEB-20260902 议题二) ==========

_spec = importlib.util.spec_from_file_location("cf_pullback_replay", _REPLAY)
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)


def _mk_replay_env(tmp_path, monkeypatch, shadow_rows, n_instr=2):
    """构造最小重放环境: 内存 DB (1 archetype 指令×n) + 可选影子文件 + bar 目录."""
    import sqlite3
    db = tmp_path / "db.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE playbook_instructions (
        id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, direction TEXT,
        entry_price REAL, stop_loss REAL, take_profit REAL, position_size INTEGER,
        decision_id TEXT, raw_instruction_json TEXT)""")
    # v2 (2026-09-07) 双臂同尺需读 exit_time / metadata_json (对齐生产真表列) —
    # 缺列会让 build_rows 的 SELECT 直接 OperationalError, 影子口径三例集体假失败。
    conn.execute("""CREATE TABLE trade_records (
        id INTEGER PRIMARY KEY, trade_date TEXT, symbol TEXT, direction TEXT,
        entry_time TEXT, exit_time TEXT, entry_price REAL, exit_price REAL,
        quantity INTEGER, pnl_net REAL, pnl_pct REAL, exit_reason TEXT,
        source_type TEXT, decision_id TEXT, metadata_json TEXT, created_at TEXT)""")
    for i in range(n_instr):
        conn.execute(
            "INSERT INTO playbook_instructions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (100 + i, "2026-06-18", "US.NTES", "BUY", 100.0, 95.0, 110.0, 50,
             f"DA-t{i}",
             json.dumps({"entry_archetype": "PULLBACK_LIMIT",
                         "confirmation": {"price_level": 98.0, "side": "below",
                                           "ttl_min": 30},
                         "chase_cap_price": 101.0})))
    conn.commit()
    conn.close()

    shadow = tmp_path / "pullback_limit_shadow.jsonl"
    if shadow_rows is not None:
        shadow.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in shadow_rows),
            encoding="utf-8")

    bar_dir = tmp_path / "bars"
    bar_dir.mkdir()
    bars = [[f"2026-06-18 {h:02d}:30:00", 100.0, 100.5, 97.0, 99.0]
            for h in range(9, 16)]
    (bar_dir / "US_NTES_2026-06-18.json").write_text(json.dumps(bars))

    monkeypatch.setattr(rp, "DB", db)
    monkeypatch.setattr(rp, "BAR_DIR", bar_dir)
    monkeypatch.setattr(rp, "SHADOW", shadow)
    monkeypatch.setattr(rp, "OUT_JSONL", tmp_path / "out.jsonl")
    monkeypatch.setattr(rp, "OUT_LATEST", tmp_path / "out_latest.json")
    return tmp_path / "out_latest.json"


class TestReplayShadowNullCaliber:
    """影子未采集 → chase_cap 维度 null (非 0/False), note 带口径注记."""

    def test_shadow_file_missing_summary_null_and_note(self, tmp_path, monkeypatch, capsys):
        """9/1 病灶复刻: 影子文件缺失 → n_chase_cap_violated=null + note 含"未采集"."""
        out = _mk_replay_env(tmp_path, monkeypatch, shadow_rows=None)
        import sys
        from unittest.mock import patch
        with patch.object(sys, "argv", ["cf_pullback_limit_replay.py", "--date", "2026-06-18"]):
            rp.main()
        summary = json.loads(out.read_text())["summary"]
        assert summary["n_chase_cap_violated"] is None, (
            "影子文件缺失时 n_chase_cap_violated 必须为 null — 0 会被误读为'零违例'")
        assert "未采集" in summary["note"]

    def test_shadow_file_missing_row_level_null(self, tmp_path, monkeypatch, capsys):
        """行级: 无影子行的指令 chase_cap_violated=null (非 False)."""
        out = _mk_replay_env(tmp_path, monkeypatch, shadow_rows=None)
        import sys
        from unittest.mock import patch
        with patch.object(sys, "argv", ["cf_pullback_limit_replay.py", "--date", "2026-06-18"]):
            rp.main()
        rows = json.loads(out.read_text())["rows"]
        assert rows, "合成指令必须产出重放行"
        assert all(r["chase_cap_violated"] is None for r in rows), (
            "无影子行的指令 chase_cap_violated 必须为 null — False 是'采到零违例'的假零")

    def test_shadow_collected_counts_normally(self, tmp_path, monkeypatch, capsys):
        """影子已采集: 正常计数 (False=零违例, True=违例), note 无"未采集"."""
        shadow_rows = [
            {"date": "2026-06-18", "inst_id": 100, "type": "trigger",
             "ts": "2026-06-18T09:31:00-04:00"},
            {"date": "2026-06-18", "inst_id": 101, "type": "chase_cap_violation",
             "price": 101.5},
        ]
        out = _mk_replay_env(tmp_path, monkeypatch, shadow_rows=shadow_rows)
        import sys
        from unittest.mock import patch
        with patch.object(sys, "argv", ["cf_pullback_limit_replay.py", "--date", "2026-06-18"]):
            rp.main()
        data = json.loads(out.read_text())
        summary = data["summary"]
        assert summary["n_chase_cap_violated"] == 1, (
            "inst 101 有 chase_cap_violation 行 → 计 1")
        assert "未采集" not in summary["note"]
        by_inst = {r["inst_id"]: r["chase_cap_violated"] for r in data["rows"]}
        assert by_inst[100] is False and by_inst[101] is True


# ========== 议题一: 违例升级被去重吞没修复 (2026-09-03, DEB-20260903 方案A, 用户裁"按建议执行") ==========

def test_chase_cap_warning_outside_dedup_gate():
    """9/2 DELL 病灶: warning 在去重门内 → 二次更糟 reprice (464.41→474.27) 被
    (inst_id,type) 全文件去重静默吞没, 无 jsonl 行无日志; 而 n_chase_cap_violated
    是 DEB-20260901 Phase C 裁决证据列, 系统性少记=尺子偏向"违例罕见".
    锁住: warning 先于去重门发射 (每次违例 reprice 都打).

    DEB-20260904 #175: 去重门拆为 _shadow_dedup_claim (只查不占) +
    _shadow_dedup_commit (写盘成功后提交) — claim 在 warning 前后均可 (只读),
    本测试锁的是 warning 不得被去重判定拦住 (即 warning 在 `if claim is not None`
    分支之外先行发射)."""
    s = _src()
    idx = s.find("chase_cap违例遥测")
    assert idx > 0, "chase_cap 违例遥测日志必须存在"
    block = s[max(0, idx - 3000):idx + 800]
    w = block.find("logger.warning(")
    g = block.find("_shadow_dedup_claim(_cc_path")
    assert 0 < w < g, (
        "chase_cap warning 必须在 _shadow_dedup_claim 去重门之前 — "
        "在去重门内 = 升级违例静默 (9/2 DELL 474.27 无遥测事故)")


def test_chase_cap_dedup_key_embeds_reprice_value():
    """去重键内嵌 reprice 值: 同值重报仍去重 (防同轮刷屏), 更差 reprice 记新行."""
    s = _src()
    assert 'f"chase_cap_violation:{round(_new_entry, 2)}"' in s, (
        "chase_cap 去重 kind 必须内嵌 reprice 值 (DEB-20260903 方案A)")


def test_shadow_dedup_value_keyed_file_seeding(tmp_path):
    """功能级: 文件已有 464.41 违例行 → 同值去重 / 更差值 474.27 放行入行 /
    放行后内存侧同值去重 / 无关 kind (trigger) 互不染指。"""
    from app.playbook import pipeline as _pl
    p = tmp_path / "shadow.jsonl"
    p.write_text(json.dumps({"inst_id": 2072, "type": "chase_cap_violation",
                             "repriced_entry": 464.41}) + "\n", encoding="utf-8")
    _pl._SHADOW_SEEN.pop(str(p), None)
    assert _pl._shadow_dedup_ok(p, 2072, "chase_cap_violation:464.41") is False, (
        "文件已有同值违例 → 去重 (重启后同值不重复入行)")
    assert _pl._shadow_dedup_ok(p, 2072, "chase_cap_violation:474.27") is True, (
        "更差 reprice → 放行记新行 (9/2 DELL 升级违例必须可见)")
    assert _pl._shadow_dedup_ok(p, 2072, "chase_cap_violation:474.27") is False, (
        "放行后内存侧同值去重 (防同轮刷屏)")
    assert _pl._shadow_dedup_ok(p, 2072, "trigger") is True, (
        "值键补种不得污染其他 kind 的去重命名空间")
