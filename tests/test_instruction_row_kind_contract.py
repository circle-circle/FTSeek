"""[DEB-20261027 B] 占位行的结构化标记 (row_kind) —— 契约测试。

背景: 生成轮每天写 ~50-70 行"供给占位行" (`direction='SKIP'` / skip_reason 前缀
「非当前通道候选」/ `model_used='deterministic'` / `entry_price=0`), 含义 =
被筛过但 DA bridge 未提名的候选。此前只能靠**字符串前缀**识别 ⇒ 12+ 个读数面
把占位行当真指令/真跳过计数。

本测试锁死:
  1) schema: `row_kind` 列存在, 默认 'instruction';
  2) 写入: 占位行被显式标 'supply_placeholder'; DA/真指令默认 'instruction';
  3) **文本契约不破**: skip_reason 前缀逐字保留 (observe_nightly_supply.py:109
     与 deb20261007_fee_gap_forward.py:73 按 startswith/子串判定);
  4) 回填脚本: 范围 = 既有前缀定义, 幂等;
  5) 生产回归: 所有占位行都必须已标 'supply_placeholder'。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.playbook.db import PlaybookDB  # noqa: E402


class _RegimeVal:
    value = "normal"


class _Regime:
    regime = _RegimeVal()
    vix = 15.0
    spy_ma20 = 500.0
    spy_ma50 = 490.0

from app.playbook.instruction_generator import Instruction  # noqa: E402
from app.playbook.pipeline_parts.instruction_builder import (  # noqa: E402
    _instruction_to_db_dict,
)

KIND_INSTRUCTION = "instruction"
KIND_PLACEHOLDER = "supply_placeholder"
PLACEHOLDER_PREFIX = "非当前通道候选"


@pytest.fixture()
def db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    inst = PlaybookDB(conn=conn)
    inst.ensure_tables()
    return inst


# ── 1) schema ───────────────────────────────────────────────────────────────

def test_row_kind_column_exists_with_default(db):
    cols = {r[1]: r for r in db._conn().execute(
        "PRAGMA table_info(playbook_instructions)").fetchall()}
    assert "row_kind" in cols, "playbook_instructions 缺 row_kind 列"
    dflt = str(cols["row_kind"][4] or "").strip("'\"")
    assert dflt == KIND_INSTRUCTION, f"row_kind 默认值应为 {KIND_INSTRUCTION}, 实为 {dflt!r}"


# ── 2) 写入标记 ─────────────────────────────────────────────────────────────

def test_placeholder_instruction_carries_supply_placeholder_kind():
    inst = Instruction(symbol="US.TEST", direction="SKIP",
                       skip_reason=f"{PLACEHOLDER_PREFIX}(bridge未提名/无方向)",
                       row_kind=KIND_PLACEHOLDER)
    d = _instruction_to_db_dict(inst, _Regime(), "2026-10-27")
    assert d["row_kind"] == KIND_PLACEHOLDER


def test_normal_instruction_defaults_to_instruction():
    inst = Instruction(symbol="US.TEST", direction="BUY", entry_price=100.0)
    d = _instruction_to_db_dict(inst, _Regime(), "2026-10-27")
    assert d["row_kind"] == KIND_INSTRUCTION


def test_db_row_persists_row_kind(db):
    inst = Instruction(symbol="US.TEST", direction="SKIP",
                       skip_reason=f"{PLACEHOLDER_PREFIX}(bridge未提名/无方向)",
                       row_kind=KIND_PLACEHOLDER)
    db.save_instruction(_instruction_to_db_dict(inst, _Regime(), "2026-10-27"))
    rows = db._conn().execute(
        "SELECT row_kind FROM playbook_instructions WHERE symbol='US.TEST'").fetchall()
    assert rows and rows[0][0] == KIND_PLACEHOLDER


def test_da_row_without_row_kind_defaults_to_instruction(db):
    """DA skill 直写的行不传 row_kind ⇒ 落库默认 'instruction' (语义不变)。"""
    db.save_instruction({
        "date": "2026-10-27", "symbol": "US.DA", "direction": "BUY",
        "entry_price": 100.0, "stop_loss": 95.0, "confidence": 70,
        "decision_id": "DA-20261027203654-US.TEST-05c7", "skip_reason": "",
    })
    rows = db._conn().execute(
        "SELECT row_kind FROM playbook_instructions WHERE symbol='US.DA'").fetchall()
    assert rows and rows[0][0] == KIND_INSTRUCTION


# ── 3) 文本契约不破 ──────────────────────────────────────────────────────────

def test_placeholder_skip_reason_prefix_unchanged():
    """占位文案前缀必须逐字保留 —— 下游按 startswith 过滤。"""
    from app.playbook.pipeline import _placeholder_skip_reason
    txt = _placeholder_skip_reason("US.TEST", {})
    assert txt.startswith(PLACEHOLDER_PREFIX), txt
    txt2 = _placeholder_skip_reason("US.TEST", {"US.TEST": "gate:破位硬买"})
    assert txt2.startswith(PLACEHOLDER_PREFIX), txt2
    assert txt2 != txt  # RFC-172 分桶后缀仍在


def test_source_marks_placeholder_at_creation_site():
    src = (REPO / "app" / "playbook" / "pipeline.py").read_text(encoding="utf-8")
    i = src.find("_placeholder_skip_reason(symbol, _da_buckets)")
    assert i > 0, "未找到占位行创建点"
    seg = src[i:i + 400]
    assert 'row_kind="supply_placeholder"' in seg


# ── 4) 回填脚本 ─────────────────────────────────────────────────────────────

def test_backfill_script_scope_is_prefix_contract_and_idempotent(tmp_path, monkeypatch):
    script = REPO / "scripts" / "backfill_instruction_row_kind.py"
    assert script.exists(), "缺回填脚本"
    p = tmp_path / "t.db"
    c = sqlite3.connect(str(p))
    c.execute("""CREATE TABLE playbook_instructions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, symbol TEXT, direction TEXT,
        skip_reason TEXT, model_used TEXT, entry_price REAL, row_kind TEXT, updated_at TEXT)""")
    c.execute("INSERT INTO playbook_instructions (date,symbol,direction,skip_reason,row_kind)"
              " VALUES ('2026-10-27','US.A','SKIP',?,'instruction')",
              (f"{PLACEHOLDER_PREFIX}(bridge未提名/无方向)",))
    c.execute("INSERT INTO playbook_instructions (date,symbol,direction,skip_reason,row_kind)"
              " VALUES ('2026-10-27','US.B','SKIP','其它原因','instruction')")
    c.commit()
    c.close()

    import importlib.util
    spec = importlib.util.spec_from_file_location("bk_row_kind", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    c = sqlite3.connect(str(p))
    c.row_factory = sqlite3.Row
    rows = mod.find_rows(c)
    assert len(rows) == 1, rows
    assert rows[0]["symbol"] == "US.A"          # 只命中前缀契约内的行
    mod.apply_rows(c, rows, tmp_path / "audit.jsonl")
    c.commit()
    # 幂等: 再跑一次 0 行
    assert mod.find_rows(c) == []
    got = dict(c.execute("SELECT symbol, row_kind FROM playbook_instructions").fetchall()[0])
    c.close()
    assert got == {"symbol": "US.A", "row_kind": KIND_PLACEHOLDER}
    assert (tmp_path / "audit.jsonl").exists()


# ── 5) 生产回归 ─────────────────────────────────────────────────────────────

@pytest.mark.skipif(not (REPO / "data" / "ftseek_us.db").exists(), reason="无生产库")
def test_production_all_placeholder_rows_are_marked():
    uri = f"file:{(REPO / 'data' / 'ftseek_us.db').resolve()}?mode=ro"
    c = sqlite3.connect(uri, uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    cols = {r[1] for r in c.execute("PRAGMA table_info(playbook_instructions)").fetchall()}
    if "row_kind" not in cols:
        pytest.skip("row_kind 列尚未迁移 (ensure_tables 未跑)")
    bad = c.execute(
        "SELECT id, date, symbol, row_kind FROM playbook_instructions "
        "WHERE COALESCE(skip_reason,'') LIKE ? AND COALESCE(row_kind,?) <> ?",
        (PLACEHOLDER_PREFIX + "%", KIND_INSTRUCTION, KIND_PLACEHOLDER)).fetchall()
    total = c.execute(
        "SELECT COUNT(*) FROM playbook_instructions WHERE COALESCE(skip_reason,'') LIKE ?",
        (PLACEHOLDER_PREFIX + "%",)).fetchone()[0]
    c.close()
    assert not bad, f"{len(bad)}/{total} 行占位行未标记 row_kind: {[dict(r) for r in bad[:5]]}"
