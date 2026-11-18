"""[DEB-20261117 P1] 迟成交证据回灌 (exec_gate_audit + execution_tca 双账本)。

病灶 (2026-11-17 实盘实证): 当日 6 笔成交里 4 笔 (ELV 5647 / CAT 5650 / UBER 5652 /
BWXT 5654) 在下单轮只落到 `submitted_unconfirmed` / TCA `fill_price=null`, 成交证据
是稍后由 PositionSync 从券商回执补录的 ⇒
  · `scripts/gate_ledger.py:106` 按 `if "executed" in actions` 判终局 → 执行率 2/6;
  · `scripts/execution_tca_report.py:211` 只取 `success is True and fill_price>0`
    → 计划→成交滑点样本只剩 BA/COIN 2 笔, 缺口 67%。

本测试锁死:
  1) 回灌只**追加**, 且 entry/size/notional 沿用指令计划值 (不污染既有分母);
  2) 真实成交证据只进 extra / 标准 fill 字段, 带 late_fill 来源标记;
  3) 单位契约: TCA `atr_pct` 百分数 (×100), 审计 `atr_pct` 分数 (原值) — 同名不同尺;
  4) 方向归一 bps 符号: 正数 = 对交易更不利 (SHORT 成交更低 / LONG 成交更高);
  5) 去重: 已有 executed / success 行则不再补 (防重启竞态双计);
  6) 失败纪律: 任何垃圾输入都不上抛, 且不写盘;
  7) 时刻尺度: naive 券商串**不得**被当 UTC/CST 解析 (DEB-20260905 §十三 病灶)。
"""
import json
import sqlite3

import pytest

from app.playbook import late_fill_evidence as lfe


@pytest.fixture()
def audit_calls(monkeypatch):
    """拦截审计写方: 既避免污染生产 jsonl, 也拿到实际入参。"""
    import app.playbook.pipeline as pipeline
    calls = []

    def _fake_emit(inst, *, action, entry, size, notional, extra=None):
        calls.append({"inst": dict(inst), "action": action, "entry": entry,
                      "size": size, "notional": notional, "extra": extra or {}})

    monkeypatch.setattr(pipeline, "_emit_exec_gate_audit", _fake_emit)
    return calls


@pytest.fixture()
def tca_path(tmp_path, monkeypatch):
    """TCA 走真实写盘逻辑, 但落到 tmp; 配置读取保持默认 (enabled)。"""
    target = tmp_path / "execution_tca.jsonl"
    monkeypatch.setattr(lfe, "_TCA_PATH", target)
    return target


@pytest.fixture()
def audit_path(tmp_path, monkeypatch):
    target = tmp_path / "exec_gate_audit.jsonl"
    monkeypatch.setattr(lfe, "_AUDIT_PATH", target)
    return target


def _bwxt_instruction():
    """11-17 真实形态: SELL_SHORT, 计划锚 120.6, DB entry 120.41, 成交 120.19。"""
    return {
        "id": 5654,
        "symbol": "US.BWXT",
        "direction": "SELL_SHORT",
        "decision_id": "DA-20261117213252-US.BWXT-c280",
        "entry_price": 120.41,
        "position_size": 31,
        "atr_pct": 0.055769,
        "vol_bucket": "high",
        "stop_loss": 127.03,
        "take_profit": 110.48,
        "skip_reason": "",
        "raw_instruction_json": json.dumps({"planned_entry": 120.6,
                                            "entry_archetype": "FADE_SHORT"}),
    }


def _read(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


# --- 审计账本 ---------------------------------------------------------------


def test_audit_backfill_uses_planned_values_and_extra_carries_fill(audit_calls,
                                                                  audit_path, tca_path):
    out = lfe.record_late_fill(_bwxt_instruction(), fill_price=120.19, dealt_qty=31,
                               execution_ts="2026-11-17T09:51:40.167509-05:00")
    assert out["audit"] is True
    assert len(audit_calls) == 1
    call = audit_calls[0]
    assert call["action"] == "executed"
    # entry/size/notional 必须是**计划值**, 与同指令那行 submitted_unconfirmed 同口径
    assert call["entry"] == pytest.approx(120.41)
    assert call["size"] == pytest.approx(31)
    assert call["notional"] == pytest.approx(round(120.41 * 31, 2))
    extra = call["extra"]
    assert extra["late_fill"] is True
    assert extra["evidence_source"] == "sync_reconcile"
    assert extra["execution_price"] == pytest.approx(120.19)
    assert extra["dealt_qty"] == 31
    assert extra["execution_time"] == "2026-11-17T09:51:40.167509-05:00"
    # 单位契约: 审计侧 atr_pct 是**分数**, 不得 ×100
    assert extra["atr_pct"] == pytest.approx(0.055769)


def test_audit_backfill_skips_when_executed_row_exists(audit_calls, audit_path, tca_path):
    audit_path.write_text(json.dumps(
        {"inst_id": 5654, "action": "executed", "date": "2026-11-17"}) + "\n",
        encoding="utf-8")
    out = lfe.record_late_fill(_bwxt_instruction(), fill_price=120.19, dealt_qty=31)
    assert out["audit"] is False
    assert audit_calls == []


def test_audit_backfill_ignores_other_instruction_rows(audit_calls, audit_path, tca_path):
    """同日的 submitted_unconfirmed / 别的指令的 executed 都不构成去重理由。"""
    audit_path.write_text("\n".join(json.dumps(row) for row in (
        {"inst_id": 5654, "action": "submitted_unconfirmed", "date": "2026-11-17"},
        {"inst_id": 5658, "action": "executed", "date": "2026-11-17"},
    )) + "\n", encoding="utf-8")
    out = lfe.record_late_fill(_bwxt_instruction(), fill_price=120.19, dealt_qty=31)
    assert out["audit"] is True


def test_sqlite_row_instruction_is_accepted(audit_calls, audit_path, tca_path):
    """调用方按 sqlite3.Row 传行 (Row 不是 Mapping 实例) — 入口必须自己归一。"""
    inst = _bwxt_instruction()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE playbook_instructions (id INTEGER, symbol TEXT, "
                 "direction TEXT, decision_id TEXT, entry_price REAL, "
                 "position_size INTEGER, atr_pct REAL, vol_bucket TEXT, "
                 "stop_loss REAL, take_profit REAL, skip_reason TEXT, "
                 "raw_instruction_json TEXT)")
    conn.execute("INSERT INTO playbook_instructions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 tuple(inst[key] for key in (
                     "id", "symbol", "direction", "decision_id", "entry_price",
                     "position_size", "atr_pct", "vol_bucket", "stop_loss",
                     "take_profit", "skip_reason", "raw_instruction_json")))
    row = conn.execute("SELECT * FROM playbook_instructions WHERE id=5654").fetchone()
    out = lfe.record_late_fill(row, fill_price=120.19, dealt_qty=31)
    assert out["audit"] is True
    assert audit_calls[0]["inst"]["symbol"] == "US.BWXT"
    assert _read(tca_path)[0]["planned_entry"] == pytest.approx(120.6)


# --- TCA 账本 ---------------------------------------------------------------


def test_tca_backfill_writes_success_row_with_fill(audit_calls, audit_path, tca_path):
    out = lfe.record_late_fill(_bwxt_instruction(), fill_price=120.19, dealt_qty=31,
                               execution_ts="2026-11-17T09:51:40.167509-05:00")
    assert out["tca"] is True
    rows = _read(tca_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["schema"] == "execution_tca_v1"
    assert row["success"] is True and row["executed"] is True
    assert row["fill_price"] == pytest.approx(120.19)
    assert row["late_fill"] is True
    assert row["fill_evidence_source"] == "sync_reconcile"
    # 成交瞬间已过去, 到达报价不得伪造
    assert row["quote_source"] == "late_fill_no_arrival_quote"
    for key in ("bid", "ask", "mid", "spread_bps", "quote_age_ms"):
        assert row[key] is None
    # 时间戳必须记成交日, 不是回灌日
    assert row["date"] == "2026-11-17"
    assert row["ts_et"].startswith("2026-11-17T09:51:40")


def test_tca_planned_to_fill_bps_sign_is_unfavorable_positive(audit_calls, audit_path,
                                                             tca_path):
    """SHORT 计划 120.6 → 成交 120.19: 卖得更低 = 更不利 ⇒ 正 bps。"""
    lfe.record_late_fill(_bwxt_instruction(), fill_price=120.19, dealt_qty=31)
    row = _read(tca_path)[0]
    assert row["planned_entry"] == pytest.approx(120.6)
    assert row["planned_entry_source"] == "raw.planned_entry"
    assert row["planned_to_fill_bps"] == pytest.approx(34.0, abs=0.05)
    assert row["planned_to_fill_bps"] > 0


def test_tca_long_direction_sign(audit_calls, audit_path, tca_path):
    """LONG 计划 433.0 → 成交 439.42 (ELV 11-17 真实值): 买得更贵 = 更不利 ⇒ 正 bps。"""
    inst = _bwxt_instruction()
    inst.update({"id": 5647, "symbol": "US.ELV", "direction": "BUY",
                 "entry_price": 440.08, "position_size": 9, "atr_pct": 0.030138,
                 "raw_instruction_json": json.dumps({"planned_entry": 433.0})})
    lfe.record_late_fill(inst, fill_price=439.42, dealt_qty=9)
    row = _read(tca_path)[0]
    assert row["direction"] == "LONG"
    assert row["planned_to_fill_bps"] == pytest.approx(148.27, abs=0.05)
    assert row["planned_to_fill_bps"] > 0


def test_tca_atr_pct_is_percentage_not_fraction(audit_calls, audit_path, tca_path):
    """单位契约钉子: DB 0.055769 (分数) → TCA 5.5769 (百分数), 同 auto_execute_flow:1837。"""
    lfe.record_late_fill(_bwxt_instruction(), fill_price=120.19, dealt_qty=31)
    assert _read(tca_path)[0]["atr_pct"] == pytest.approx(5.5769)


def test_tca_backfill_skips_when_success_row_exists(audit_calls, audit_path, tca_path):
    tca_path.write_text(json.dumps(
        {"inst_id": 5654, "success": True, "fill_price": 120.19}) + "\n",
        encoding="utf-8")
    out = lfe.record_late_fill(_bwxt_instruction(), fill_price=120.19, dealt_qty=31)
    assert out["tca"] is False
    assert len(_read(tca_path)) == 1


def test_tca_backfill_respects_shadow_switch(audit_calls, audit_path, tca_path, monkeypatch):
    """回滚/关闭面: execution_tca_shadow_enabled=false ⇒ 一行都不写。"""
    import app.playbook.execution_tca_shadow as tca
    monkeypatch.setattr(tca, "tca_config", lambda pb_cfg=None: {
        "enabled": False, "spread_block_bps": 20.0, "quote_age_block_ms": 2000})
    out = lfe.record_late_fill(_bwxt_instruction(), fill_price=120.19, dealt_qty=31)
    assert out["tca"] is False
    assert _read(tca_path) == []


# --- 失败纪律 / 尺度 ---------------------------------------------------------


@pytest.mark.parametrize("fill,qty", [(None, 31), (0, 31), (120.19, None),
                                      (120.19, 0), ("", 31), ("abc", 31)])
def test_garbage_fill_evidence_writes_nothing(audit_calls, audit_path, tca_path,
                                              fill, qty):
    out = lfe.record_late_fill(_bwxt_instruction(), fill_price=fill, dealt_qty=qty)
    assert out == {"audit": False, "tca": False}
    assert audit_calls == []
    assert _read(tca_path) == []


def test_test_symbol_is_never_recorded(audit_calls, audit_path, tca_path):
    inst = _bwxt_instruction()
    inst["symbol"] = "US.TEST_SYNC"
    out = lfe.record_late_fill(inst, fill_price=120.19, dealt_qty=31)
    assert out == {"audit": False, "tca": False}
    assert audit_calls == []


def test_never_raises_when_emitter_blows_up(audit_path, tca_path, monkeypatch):
    """度量故障不得变成交易故障: 写方炸了也只返回 False。"""
    import app.playbook.pipeline as pipeline

    def _boom(*_args, **_kwargs):
        raise RuntimeError("emitter down")

    monkeypatch.setattr(pipeline, "_emit_exec_gate_audit", _boom)
    out = lfe.record_late_fill(_bwxt_instruction(), fill_price=120.19, dealt_qty=31)
    assert out["audit"] is False
    assert out["tca"] is True, "审计侧失败不得连带干掉 TCA 侧"


def test_fill_instant_rejects_naive_broker_timestamp():
    """naive 券商串既不是 UTC 也不是 CST (是 ET) ⇒ 必须回退, 不得错尺度落盘。"""
    from app.utils.timezone import now_et
    resolved = lfe._fill_instant("2026-11-17 09:51:40")
    assert resolved.tzinfo is not None
    assert resolved.strftime("%Y-%m-%d") == now_et().strftime("%Y-%m-%d")
    assert lfe._fill_instant("").tzinfo is not None
    assert lfe._fill_instant(None).tzinfo is not None


def test_fill_instant_keeps_aware_timestamp():
    resolved = lfe._fill_instant("2026-11-17T09:51:40.167509-05:00")
    assert resolved.utcoffset().total_seconds() == -5 * 3600
    assert resolved.strftime("%Y-%m-%d") == "2026-11-17"


@pytest.mark.parametrize("text,offset_hours", [
    ("2026-11-17T09:51:40-05:00", -5),   # 负偏移 (ET 冬令时) — 曾漏判成 naive
    ("2026-11-17T09:51:40+08:00", 8),    # 正偏移
    ("2026-11-17T09:51:40Z", 0),         # Z 后缀
])
def test_fill_instant_accepts_both_offset_signs(text, offset_hours):
    resolved = lfe._fill_instant(text)
    assert resolved.utcoffset().total_seconds() == offset_hours * 3600


def test_tail_rows_skips_corrupt_lines(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text('{"inst_id": 1}\nnot json\n\n{"inst_id": 2}\n', encoding="utf-8")
    rows = lfe._tail_rows(path)
    assert [row["inst_id"] for row in rows] == [1, 2]


def test_reconcile_call_site_wires_late_fill():
    """调用点钉子: 补录成功分支必须真的调 record_late_fill, 否则本模块是死代码。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "core"
           / "position_sync.py").read_text(encoding="utf-8")
    assert "from app.playbook.late_fill_evidence import record_late_fill" in src
    assert "evidence_source='sync_reconcile'" in src


# --- audit_path 陷阱守卫 (2026-11-18 事故修正) -------------------------------
#
# 病灶: 下游 `_emit_exec_gate_audit` (risk_gates.py:433) 无 path 参数, output_path 由
# module_file 推导 ⇒ 写恒落生产。而 `_backfill_audit` 的 path 只管去重读 ⇒ 传 tmp 而不动
# 模块常量, 等于「去重读空 + 写生产」。实证: 2026-11-18 09:30 CST 一次离线验证因此把 4 行
# evidence_source='verify_real_row' 追加进生产 exec_gate_audit.jsonl 末尾 (8839-8842/8842),
# 已按用户授权截除, 留痕 data/audits/exec_gate_audit_excision_20261118T014102Z.jsonl。
# 原 25 例未抓到: 它们整体 monkeypatch 了 emitter, 从未验证 audit_path 是否真被 honored。


def test_audit_backfill_refuses_when_path_diverges_from_module_constant(
        audit_calls, tmp_path, monkeypatch, tca_path):
    """传了 audit_path 但没 monkeypatch _AUDIT_PATH ⇒ 必须拒绝, 且不碰 emitter。

    无守卫时本例会红: _backfill_audit 会照常调 emitter 并返回 True (写落"生产")。
    """
    prod_like = tmp_path / "prod" / "exec_gate_audit.jsonl"
    divergent = tmp_path / "verifier_thinks_this_is_tmp.jsonl"
    monkeypatch.setattr(lfe, "_AUDIT_PATH", prod_like)

    out = lfe.record_late_fill(_bwxt_instruction(), fill_price=120.19, dealt_qty=31,
                               execution_ts="2026-11-17T09:51:40.167509-05:00",
                               audit_path=divergent, tca_path=tca_path)

    assert out["audit"] is False, "路径分叉时必须拒绝回灌审计侧"
    assert audit_calls == [], "拒绝就不得触达 emitter (否则写会落生产)"
    assert not divergent.exists(), "被拒的 audit_path 不应产生任何文件"


def test_audit_guard_leaves_tca_side_working(audit_calls, tmp_path, monkeypatch, tca_path):
    """守卫只作用于审计侧 —— TCA 的 path 缝是真生效的, 故 tca 仍应写盘。

    这条钉子把两侧的**不对称**钉住: execution_tca_shadow.finalize_and_record 有 path 参数
    (DEB-20261117 P1 加的缝), 而 _emit_exec_gate_audit 没有。别把守卫复制到 TCA 侧。
    """
    monkeypatch.setattr(lfe, "_AUDIT_PATH", tmp_path / "prod" / "exec_gate_audit.jsonl")

    out = lfe.record_late_fill(_bwxt_instruction(), fill_price=120.19, dealt_qty=31,
                               execution_ts="2026-11-17T09:51:40.167509-05:00",
                               audit_path=tmp_path / "divergent.jsonl", tca_path=tca_path)

    assert out == {"audit": False, "tca": True}
    rows = _read(tca_path)
    assert len(rows) == 1 and rows[0]["success"] is True
    assert rows[0]["fill_price"] == 120.19


def test_audit_guard_is_noop_when_paths_agree(audit_calls, audit_path, tca_path):
    """audit_path 与 _AUDIT_PATH 同源 (测试夹具即如此) ⇒ 守卫不得改变既有行为。"""
    out = lfe.record_late_fill(_bwxt_instruction(), fill_price=120.19, dealt_qty=31,
                               execution_ts="2026-11-17T09:51:40.167509-05:00",
                               audit_path=audit_path, tca_path=tca_path)

    assert out["audit"] is True
    assert len(audit_calls) == 1
    assert audit_calls[0]["action"] == "executed"
    assert audit_calls[0]["extra"]["late_fill"] is True


def test_audit_guard_tolerates_equivalent_path_spelling(audit_calls, tmp_path,
                                                        monkeypatch, tca_path):
    """同一文件的不同拼法 (相对/../ 绕路) 不算分叉, 否则守卫会误杀正当调用。"""
    prod_like = tmp_path / "prod" / "exec_gate_audit.jsonl"
    monkeypatch.setattr(lfe, "_AUDIT_PATH", prod_like)
    same_file_other_spelling = tmp_path / "prod" / "subdir" / ".." / "exec_gate_audit.jsonl"

    out = lfe.record_late_fill(_bwxt_instruction(), fill_price=120.19, dealt_qty=31,
                               execution_ts="2026-11-17T09:51:40.167509-05:00",
                               audit_path=same_file_other_spelling, tca_path=tca_path)

    assert out["audit"] is True, "resolve() 后同一文件应视为同源"
    assert len(audit_calls) == 1


def test_downstream_emitter_really_has_no_path_parameter():
    """根因钉子: 若哪天 emitter 加了 path 参数, 守卫就该改成透传而非拒绝 —— 让本例先红。"""
    import inspect
    from app.playbook.pipeline_parts import risk_gates

    params = inspect.signature(risk_gates._emit_exec_gate_audit).parameters
    assert "path" not in params and "output_path" not in params, (
        "下游已支持 path 注入 ⇒ _backfill_audit 的 fail-close 守卫应改为把 path 透传下去")
