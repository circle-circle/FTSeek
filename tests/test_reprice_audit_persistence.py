"""[P3.2 2026-09-13] re-price 审计真正落库 — proud-slope-falcon.

被测对象:
  `app/playbook/db.py::PlaybookDB.merge_instruction_metadata` (新增)
  `app/playbook/pipeline.py::run_playbook_auto_execute` 的 re-price 现场 (源级契约)

立项依据 (DEB-20260910 §18.3 只读实测):
  `pipeline.py` 自 2026-06-29 起每次 re-price 都构造
  `reprice_from` / `reprice_to` / `reprice_original_sl` 并注释为「审计字段 — 记录
  re-price 来源」「更新 DB 记录 re-priced 值 (审计可追溯)」, 但:
    - `db._TRACKING_FIELDS` (42 项) **不含**这三键, `update_instruction_tracking`
      只写白名单内的键 ⇒ 每次都被静默丢弃;
    - `playbook_instructions` 的 **73 列**里也没有它们;
    - `db.py` 白名单侧的 2026-07-11 注释同时写着「reprice_from/to/original_sl 非 DB 列,
      仍由 whitelist 正确忽略」。
  两侧注释对同一行为给了**相反意图**, 净效果是: 重定价之后库里既无生成值、也无
  「从哪重定价到哪」, 而 entry_price/stop_loss/take_profit/position_size/risk_amount
  五个真值列已被覆盖 ⇒ P3 验收②「生成预览与最终委托可追踪」不可测。

本批的处置 (零 schema 迁移):
  新增 `merge_instruction_metadata()` 把审计浅合并进既有 `metadata_json` blob
  (口径与 `intraday_reentry.py:719` 的 `original_instruction` 一致), 并在 re-price
  现场额外调用一次。**不动 `_reprice_params` 一个键** —— 它随后还要原样传给
  `execute_instruction` 的 `custom_params` (pipeline.py:4513), 删键会改下单行为。

回滚: 删本文件 + 还原 `db.py` 的新方法 + 删 `pipeline.py` 的 try 块 (三处都是纯增量)。
"""
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.playbook.db import PlaybookDB, _TRACKING_FIELDS  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PIPELINE_SRC = (REPO / "app" / "playbook" / "pipeline.py").read_text(encoding="utf-8")

# 生产库 metadata_json 里真实存在的既有注记键 (377 行非空, §18.3 实测键分布)
PRODUCTION_META_KEYS = (
    "phantom_repair_20260827", "rfc066_backfill_round1", "reentry_of_trade",
    "original_instruction", "ab_contamination", "source",
)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    pdb = PlaybookDB(conn=conn)
    pdb.ensure_tables()
    yield pdb
    conn.close()


def _insert(db, iid=1, meta=None, symbol="US.NVDA"):
    db._conn().execute(
        "INSERT INTO playbook_instructions"
        " (id, date, symbol, direction, metadata_json, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (iid, "2026-09-14", symbol, "BUY", meta,
         "2026-09-14T00:00:00+00:00", "2026-09-14T00:00:00+00:00"),
    )
    db._conn().commit()
    return iid


def _read_meta(db, iid=1):
    row = db._conn().execute(
        "SELECT metadata_json FROM playbook_instructions WHERE id=?", (iid,)).fetchone()
    return row[0]


AUDIT = {"reprice_audit": {"reprice_from": 100.0, "reprice_to": 101.5,
                           "reprice_original_sl": 94.09, "reprice_original_tp": 111.3,
                           "audit_schema": "reprice_audit_v1"}}


# ══ 1. 基本语义 ═══════════════════════════════════════════════════════════

class TestMergeSemantics:
    def test_writes_into_empty_metadata(self, db):
        iid = _insert(db, meta=None)
        assert db.merge_instruction_metadata(iid, AUDIT) is True
        assert json.loads(_read_meta(db))["reprice_audit"]["reprice_to"] == 101.5

    def test_writes_into_empty_string_metadata(self, db):
        iid = _insert(db, meta="")
        assert db.merge_instruction_metadata(iid, AUDIT) is True
        assert "reprice_audit" in json.loads(_read_meta(db))

    def test_returns_false_for_missing_row(self, db):
        assert db.merge_instruction_metadata(9999, AUDIT) is False

    def test_latest_wins_on_same_key(self, db):
        """同名键后写覆盖前写 (不追加历史) —— 契约里写明的 latest-wins。"""
        iid = _insert(db, meta=None)
        db.merge_instruction_metadata(iid, AUDIT)
        db.merge_instruction_metadata(iid, {"reprice_audit": {"reprice_to": 200.0}})
        got = json.loads(_read_meta(db))["reprice_audit"]
        assert got == {"reprice_to": 200.0}, "应是整键覆盖, 不是深合并"


# ══ 2. 既有注记一个都不能丢 (生产库 377 行已有别的批次事故留痕) ══════════

class TestPreservesExistingNotes:
    @pytest.mark.parametrize("key", PRODUCTION_META_KEYS)
    def test_existing_production_note_key_survives(self, db, key):
        iid = _insert(db, meta=json.dumps({key: {"n": 1, "at": "2026-08-27"}}))
        assert db.merge_instruction_metadata(iid, AUDIT) is True
        got = json.loads(_read_meta(db))
        assert got[key] == {"n": 1, "at": "2026-08-27"}
        assert "reprice_audit" in got

    def test_all_production_notes_survive_together(self, db):
        before = {k: {"i": i} for i, k in enumerate(PRODUCTION_META_KEYS)}
        iid = _insert(db, meta=json.dumps(before, ensure_ascii=False))
        db.merge_instruction_metadata(iid, AUDIT)
        got = json.loads(_read_meta(db))
        assert got == {**before, **AUDIT}

    def test_non_ascii_note_is_not_mangled(self, db):
        iid = _insert(db, meta=json.dumps({"note": "跨日方向翻转 降仓50%"}, ensure_ascii=False))
        db.merge_instruction_metadata(iid, AUDIT)
        assert json.loads(_read_meta(db))["note"] == "跨日方向翻转 降仓50%"

    def test_unparsable_existing_blob_is_left_byte_identical(self, db):
        """解析不了 ⇒ **不写**。宁可丢新审计, 不可毁既有注记。"""
        raw = "{not json at all"
        iid = _insert(db, meta=raw)
        assert db.merge_instruction_metadata(iid, AUDIT) is False
        assert _read_meta(db) == raw, "既有 blob 必须逐字节原样"

    def test_non_dict_existing_blob_is_left_byte_identical(self, db):
        raw = json.dumps([1, 2, 3])
        iid = _insert(db, meta=raw)
        assert db.merge_instruction_metadata(iid, AUDIT) is False
        assert json.loads(_read_meta(db)) == [1, 2, 3]

    def test_scalar_existing_blob_is_left_byte_identical(self, db):
        raw = json.dumps("just a string")
        iid = _insert(db, meta=raw)
        assert db.merge_instruction_metadata(iid, AUDIT) is False
        assert _read_meta(db) == raw


# ══ 3. 审计降级优于阻断下单 ═══════════════════════════════════════════════

class TestNeverRaises:
    def test_db_exception_is_swallowed(self, db, monkeypatch):
        """DB 层抛错 (锁/磁盘/连接断) 不得冒泡到下单路径。

        sqlite3.Connection.execute 是只读属性、monkeypatch 打不上去 ⇒ 换成把
        `_conn()` 整个替换成一个一调用就抛的假连接 (这也更接近真实故障面:
        连接池取连接本身就可能抛)。
        """
        iid = _insert(db, meta=None)

        class BoomConn:
            def execute(self, *a, **k):
                raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(db, "_conn", lambda: BoomConn())
        assert db.merge_instruction_metadata(iid, AUDIT) is False

    def test_unserializable_patch_is_swallowed(self, db):
        iid = _insert(db, meta=None)
        assert db.merge_instruction_metadata(iid, {"x": object()}) is False

    def test_empty_patch_is_a_noop_that_still_succeeds(self, db):
        iid = _insert(db, meta=json.dumps({"keep": 1}))
        assert db.merge_instruction_metadata(iid, {}) is True
        assert json.loads(_read_meta(db)) == {"keep": 1}

    def test_none_patch_is_a_noop(self, db):
        iid = _insert(db, meta=json.dumps({"keep": 1}))
        assert db.merge_instruction_metadata(iid, None) is True
        assert json.loads(_read_meta(db)) == {"keep": 1}


# ══ 4. 零迁移 / 零白名单变更 (债标记: 修好那天必须变红) ═══════════════════

class TestNoSchemaNoWhitelistChange:
    def test_reprice_keys_still_absent_from_tracking_whitelist(self):
        """**本批刻意没动白名单**。

        加进 `_TRACKING_FIELDS` 会让 `update_instruction_tracking` 生成
        `SET reprice_from = ?` ⇒ sqlite 报 "no such column" ⇒ 打红整条 re-price 路径。
        所以走 metadata_json 而不是白名单。这条变红 = 有人加了列或改了白名单,
        那时应改成断言"列已存在且审计走列"。
        """
        for key in ("reprice_from", "reprice_to", "reprice_original_sl"):
            assert key not in _TRACKING_FIELDS

    def test_tracking_whitelist_cardinality_unchanged(self):
        assert len(_TRACKING_FIELDS) == 42

    def test_metadata_json_is_not_a_tracking_field(self):
        """metadata_json 是 insert/UPSERT 时整块写入的 blob, 不是白名单列。"""
        assert "metadata_json" not in _TRACKING_FIELDS

    def test_no_new_column_was_added_to_the_table(self, db):
        cols = [r[1] for r in db._conn().execute("PRAGMA table_info(playbook_instructions)")]
        assert not [c for c in cols if c.startswith("reprice_")], "本批承诺零 schema 迁移"

    def test_update_instruction_tracking_still_drops_reprice_keys(self, db):
        """旧路径行为**一字不改** —— 这是"零行为变更"的反证钉子。"""
        iid = _insert(db, meta=None)
        db.update_instruction_tracking(iid, {
            "entry_price": 101.5, "stop_loss": 95.0,
            "reprice_from": 100.0, "reprice_to": 101.5, "reprice_original_sl": 94.09,
        })
        row = db._conn().execute(
            "SELECT entry_price, stop_loss FROM playbook_instructions WHERE id=?",
            (iid,)).fetchone()
        assert row[0] == 101.5 and row[1] == 95.0, "白名单内的列必须照旧写入"
        cols = [r[1] for r in db._conn().execute("PRAGMA table_info(playbook_instructions)")]
        assert "reprice_from" not in cols


# ══ 5. pipeline 接线 (源级契约 — 不 import 488KB 的巨型模块) ══════════════

class TestPipelineWiring:
    @pytest.fixture(scope="class")
    def region(self):
        """取 re-price 现场那一段源码 (从 _reprice_params 构造到 chase_cap 块之前)。"""
        start = PIPELINE_SRC.index("_reprice_params = {")
        end = PIPELINE_SRC.index("chase_cap 违例遥测", start)
        return PIPELINE_SRC[start:end]

    def test_audit_is_routed_through_merge_not_whitelist(self, region):
        assert "merge_instruction_metadata" in region
        assert '"reprice_audit"' in region

    def test_audit_schema_is_versioned(self, region):
        assert '"audit_schema": "reprice_audit_v1"' in region

    def test_original_tp_is_now_recorded(self, region):
        """原三键只留 SL 不留 TP。

        §18.5 实测: 已成交 517 行里 12 行 take_profit<=0, 其中 7 行是 SELL_SHORT ——
        而 tp=0.0 在 SHORT 判据 `tp<entry<sl` 下**恒通过**朴素几何检查。不留原始 TP
        就无法事后分辨「生成时就没有 TP」与「重定价把 TP 算没了」。
        """
        assert "reprice_original_tp" in region

    def test_sl_pct_tp_mult_bucket_atr_are_recorded(self, region):
        """重定价的**判据输入**也要留, 否则只知道结果不知道为什么。"""
        for key in ("reprice_sl_pct", "reprice_tp_mult", "vol_bucket", "atr_pct"):
            assert f'"{key}"' in region, f"缺少 {key}"

    def test_reprice_params_keys_are_untouched(self, region):
        """**不许删 `_reprice_params` 里的键** —— 它还要传给 execute_instruction。

        删键看着像"清理死代码", 实际会改下单行为 (custom_params 消费口径见
        pipeline.py:4456-4460 与 :4513)。这条钉住三个键仍在原字典里。
        """
        for key in ("reprice_from", "reprice_to", "reprice_original_sl"):
            assert re.search(r'"%s":' % key, region), f"{key} 被从 _reprice_params 删掉了"

    def test_audit_failure_cannot_block_the_order(self, region):
        """审计落库必须就地隔离: 降级优于丢单。

        判据不用"紧邻 try:" —— 中间隔着解释性注释是本仓风格。改用**块结构**:
        merge 调用之前最近的 `try:` 必须比最近的 `except` 更靠近它 (即调用在 try 体内),
        且其后必须出现配对的 `except Exception` + 日志。
        """
        i = region.index("merge_instruction_metadata")
        head, tail = region[:i], region[i:]
        last_try, last_except = head.rfind("try:"), head.rfind("except ")
        assert last_try != -1, "merge 调用前没有 try:"
        assert last_try > last_except, "merge 调用不在 try 体内 (最近的 except 更靠近它)"
        assert re.search(r"except Exception as \w+:\s*\n\s*logger\.", tail), \
            "merge 调用必须有 except + 日志, 不得外抛"

    def test_update_instruction_tracking_call_still_present(self, region):
        """新路径是**追加**, 不是替换: 原来那次白名单写入必须还在。"""
        assert "db.update_instruction_tracking(inst[\"id\"], _reprice_params)" in region

    def test_audit_write_happens_after_the_tracking_write(self, region):
        assert (region.index("update_instruction_tracking")
                < region.index("merge_instruction_metadata"))
