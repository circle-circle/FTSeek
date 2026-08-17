"""[2026-08-17 DA覆盖事故根源修复] save_instruction 覆盖仲裁全家族成对契约测试。

根源修复: 仲裁从调用方守卫 (先读后写, TOCTOU) 下沉到 save_instruction SQL 层
ON CONFLICT DO UPDATE WHERE 优先级阶梯 — 一处规则原子覆盖现在与未来全部写入方。

优先级阶梯:
  3 = DA- / universe_expansion* / intraday_reentry-  (深度供给/重武装)
  1 = 其他非空 decision_id (TFB-/SNAP-/DEC-)          (内部有源)
  0 = 空 decision_id (SKIP票)                         (内部无源)
规则: 新行 >= 现存行才覆盖; UE 浅指令永不覆盖 DA 深度内容; reentry 无条件放行。

8/17 事故回放断言: 空 decision_id SKIP 票 (0) 覆盖 DA 行 (3) → 拒绝。
取代晨间版 _generate_da_priority_check 契约测试 (该函数已随根源修复删除)。
"""
import sqlite3
import pytest

from app.playbook.db import PlaybookDB

FAMS = {
    "DA":      "DA-20260817-TEST-x1",
    "UE":      "universe_expansion:2026-08-17:TEST",
    "REENTRY": "intraday_reentry-123-1015",
    "TFB":     "TFB-TEST-1755432000",
    "SNAP":    "SNAP-TEST-1755432000000",
    "DEC":     "DEC-202606230400-TEST",
    "EMPTY":   "",
}

# (现存, 写入) → True=允许覆盖 / False=拒绝覆盖 — 完整 7×7 期望矩阵
EXPECTED = {
    # 写入 DA (3): 除 reentry 同级放行外全部允许 (DA 最深, 可刷新一切)
    ("DA", "DA"): True,      ("UE", "DA"): True,      ("REENTRY", "DA"): True,
    ("TFB", "DA"): True,     ("SNAP", "DA"): True,    ("DEC", "DA"): True,
    ("EMPTY", "DA"): True,
    # 写入 UE (3): 同级放行, 但深度语义禁覆盖 DA (P1-14 同源规则)
    ("DA", "UE"): False,     ("UE", "UE"): True,      ("REENTRY", "UE"): True,
    ("TFB", "UE"): True,     ("SNAP", "UE"): True,    ("DEC", "UE"): True,
    ("EMPTY", "UE"): True,
    # 写入 REENTRY: 无条件放行 (合法重武装, 覆盖前自归档原行)
    ("DA", "REENTRY"): True, ("UE", "REENTRY"): True, ("REENTRY", "REENTRY"): True,
    ("TFB", "REENTRY"): True, ("SNAP", "REENTRY"): True, ("DEC", "REENTRY"): True,
    ("EMPTY", "REENTRY"): True,
    # 写入 TFB/SNAP/DEC (1): 深度行(3)拒绝, 同级(1)放行, 空行(0)放行
    ("DA", "TFB"): False,    ("UE", "TFB"): False,    ("REENTRY", "TFB"): False,
    ("TFB", "TFB"): True,    ("SNAP", "TFB"): True,   ("DEC", "TFB"): True,
    ("EMPTY", "TFB"): True,
    ("DA", "SNAP"): False,   ("UE", "SNAP"): False,   ("REENTRY", "SNAP"): False,
    ("TFB", "SNAP"): True,   ("SNAP", "SNAP"): True,  ("DEC", "SNAP"): True,
    ("EMPTY", "SNAP"): True,
    ("DA", "DEC"): False,    ("UE", "DEC"): False,    ("REENTRY", "DEC"): False,
    ("TFB", "DEC"): True,    ("SNAP", "DEC"): True,   ("DEC", "DEC"): True,
    ("EMPTY", "DEC"): True,
    # 写入 EMPTY (0, SKIP票): 8/17 事故形态 — 任何有源行都拒绝, 空行幂等放行
    ("DA", "EMPTY"): False,  ("UE", "EMPTY"): False,  ("REENTRY", "EMPTY"): False,
    ("TFB", "EMPTY"): False, ("SNAP", "EMPTY"): False, ("DEC", "EMPTY"): False,
    ("EMPTY", "EMPTY"): True,
}


def _mkdb():
    conn = sqlite3.connect(":memory:")
    db = PlaybookDB(conn=conn)
    db.ensure_tables()
    return db, conn


def _write(db, fam, tag):
    """写一条指定家族的指令 (direction 用家族名标记, 便于断言谁赢了)。"""
    db.save_instruction({
        "date": "2026-08-17", "symbol": "US.TEST",
        "direction": "BUY" if fam != "UE" else "SELL_SHORT",
        "entry_price": 100.0, "confidence": 66,
        "decision_id": FAMS[fam],
        "thesis": f"existing-{fam}" if tag == "existing" else f"incoming-{fam}",
    })


class TestUpsertArbitrationMatrix:
    """7 家族 × 7 家族 = 49 种 (现存, 写入) 组合的完整覆盖仲裁矩阵。"""

    @pytest.mark.parametrize("key", sorted(EXPECTED.keys()))
    def test_pair(self, key):
        existing, incoming = key
        allowed = EXPECTED[key]
        db, conn = _mkdb()
        _write(db, existing, "existing")
        _write(db, incoming, "incoming")
        row = conn.execute(
            "SELECT thesis, decision_id FROM playbook_instructions "
            "WHERE date='2026-08-17' AND symbol='US.TEST'"
        ).fetchone()
        n = conn.execute(
            "SELECT COUNT(*) FROM playbook_instructions "
            "WHERE date='2026-08-17' AND symbol='US.TEST'"
        ).fetchone()[0]
        assert n == 1, f"UPSERT 必须保持单行, 实际 {n}"
        if allowed:
            assert row[0] == f"incoming-{incoming}", \
                f"{existing}→{incoming} 期望允许覆盖, 实际现存行未被替换"
        else:
            assert row[0] == f"existing-{existing}", \
                f"{existing}→{incoming} 期望拒绝覆盖, 实际现存行被破坏"
            assert row[1] == FAMS[existing], \
                f"{existing}→{incoming} 拒绝后 decision_id 必须原样保留"


class TestIncidentReplay:
    """8/17 事故精确回放: generate 64条 SKIP 票不得摧毁 DA gate-PASS 行。"""

    def test_da_row_survives_skip_barrage(self):
        db, conn = _mkdb()
        # DA skill 07:31 直写 gate-PASS BUY 指令
        db.save_instruction({
            "date": "2026-08-17", "symbol": "US.MSFT", "direction": "BUY",
            "entry_price": 512.3, "stop_loss": 495.0, "take_profit": 545.0,
            "confidence": 60, "decision_id": "DA-20260817073100-1-MSFT",
            "thesis": "DA 深度分析: 机构吸筹 + 财报后回踩支撑",
        })
        # FTSeek generate 09:00 投票 SKIP (空 decision_id) — 事故元凶
        db.save_instruction({
            "date": "2026-08-17", "symbol": "US.MSFT", "direction": "SKIP",
            "skip_reason": "投票中性, 无方向信号", "decision_id": "",
        })
        row = conn.execute(
            "SELECT direction, decision_id, thesis, entry_price, stop_loss "
            "FROM playbook_instructions WHERE date='2026-08-17' AND symbol='US.MSFT'"
        ).fetchone()
        assert row[0] == "BUY", "DA 指令被 SKIP 票覆盖 — 8/17 事故复现!"
        assert row[1].startswith("DA-"), "DA decision_id 必须保留"
        assert "深度分析" in row[2], "DA thesis 深度内容必须保留"
        assert row[3] == 512.3 and row[4] == 495.0, "gate 原始锚价/SL 必须保留"

    def test_rejected_write_returns_existing_row_id(self):
        """被拒覆盖的返回值契约: 返回现存行 id (非 0), 调用方不报错。"""
        db, conn = _mkdb()
        first_id = db.save_instruction({
            "date": "2026-08-17", "symbol": "US.AAPL", "direction": "BUY",
            "entry_price": 200.0, "decision_id": "DA-x",
        })
        second_id = db.save_instruction({
            "date": "2026-08-17", "symbol": "US.AAPL", "direction": "SKIP",
            "decision_id": "",
        })
        assert first_id > 0
        assert second_id == first_id, "被拒写入应返回现存行 id (SELECT 兜底)"


class TestIdempotencyPreserved:
    """根源修复不得破坏既有幂等语义 (#22 完成标记制不受影响)。"""

    def test_skip_overwrites_skip(self):
        """generate 重跑: SKIP 票覆盖 SKIP 票 — 幂等不受影响。"""
        db, conn = _mkdb()
        db.save_instruction({"date": "2026-08-17", "symbol": "US.X", "direction": "SKIP",
                             "skip_reason": "第一次", "decision_id": ""})
        db.save_instruction({"date": "2026-08-17", "symbol": "US.X", "direction": "SKIP",
                             "skip_reason": "第二次", "decision_id": ""})
        row = conn.execute(
            "SELECT skip_reason FROM playbook_instructions "
            "WHERE date='2026-08-17' AND symbol='US.X'"
        ).fetchone()
        assert row[0] == "第二次", "空→空覆盖必须放行 (幂等重跑)"

    def test_new_symbol_insert_unaffected(self):
        """无冲突的新标的插入不受 WHERE 子句影响。"""
        db, conn = _mkdb()
        rid = db.save_instruction({"date": "2026-08-17", "symbol": "US.NEW",
                                   "direction": "SKIP", "decision_id": ""})
        assert rid > 0
        n = conn.execute("SELECT COUNT(*) FROM playbook_instructions").fetchone()[0]
        assert n == 1
