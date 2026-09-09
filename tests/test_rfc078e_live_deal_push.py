# -*- coding: utf-8 -*-
"""RFC-078 批E 实盘成交实时推送订阅器单测 (record-only push 层).

钉锁正确性要点 (全部写码前探针实测):
  1. **REAL-only 过滤**: sim playbook 同账户 SIMULATE 成交不得落实盘账本 (最大污染陷阱)
  2. **幂等去重**: 双 ctx 双收 / sync 先落 → 同 deal_id 不重复插入、不重复通知
  3. **push 行市场字段 trd_market** (非 query 行的 deal_market) → market/currency 正确
  4. **deal_id 19 位** 经 DataFrame → str, 幂等键不失效
  5. **异常全收口**: record_deal_push 抛错 → handler 不上抛 (不打断 SDK recv 循环)
  6. **飞书通知派生 daemon 线程**, 不阻塞 recv; 通知失败不影响落账
tmp_db 隔离, 不触生产 live 库。
"""
import os
import sqlite3
import sys
import threading
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.web_dashboard import live_ledger  # noqa: E402
from app.web_dashboard import live_deal_push as ldp  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "live_ledger_test.db"
    monkeypatch.setattr(live_ledger, "_db_path", lambda: db)
    live_ledger.ensure_tables(db)
    return db


def _push_row(deal_id, code="US.TSLA", side="SELL", qty=50, price=365.04,
              env="REAL", market_field="trd_market", market="US"):
    """模拟 TradeDealHandlerBase.unpack_rsp 产出的单行 (字段名实测: trd_market/trd_env)."""
    return {
        "trd_env": env, "code": code, "stock_name": "特斯拉", "deal_id": deal_id,
        "order_id": "FH1D" + deal_id[:6], "qty": float(qty), "price": float(price),
        "trd_side": side, "create_time": "2026-09-08 11:03:14.604",
        "counter_broker_id": "123", "counter_broker_name": "Test",
        market_field: market, "status": "OK",
    }


def _subscriber(monkeypatch, tmp_db, notify=True):
    """构造订阅器, 通知/落库都接到 tmp_db; 通知默认记录调用不真发飞书."""
    sub = ldp.LiveDealPushSubscriber()
    sent = []
    monkeypatch.setattr(ldp, "notify_enabled", lambda: notify)
    # _notify_async 起 daemon 线程真调 feishu — 测试里替换为同步记录, 不联网
    def _fake_notify(raw, deal_id):
        sent.append(deal_id)
    monkeypatch.setattr(sub, "_notify_async", _fake_notify)
    return sub, sent


def _feed(sub, row):
    sub._handle_push("US", pd.DataFrame([row]))


def test_real_deal_inserted(tmp_db, monkeypatch):
    sub, sent = _subscriber(monkeypatch, tmp_db)
    _feed(sub, _push_row("1111111111111111111"))
    rows = live_ledger.read_all_deals()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "US.TSLA"
    assert rows[0]["market"] == "US"
    assert rows[0]["currency"] == "USD"
    assert rows[0]["fee"] is None  # 券商 deal 级不提供, 不伪造 0
    assert sub.stats["real_inserted"] == 1
    assert sent == ["1111111111111111111"]  # 通知一次


def test_simulate_env_filtered_out(tmp_db, monkeypatch):
    """要点1: sim playbook 同账户 SIMULATE 成交绝不落实盘账本."""
    sub, sent = _subscriber(monkeypatch, tmp_db)
    _feed(sub, _push_row("2222222222222222222", code="US.QCOM", env="SIMULATE"))
    assert live_ledger.read_all_deals() == []
    assert sub.stats["sim_skipped"] == 1
    assert sub.stats["real_inserted"] == 0
    assert sent == []


def test_idempotent_double_ctx_no_dup(tmp_db, monkeypatch):
    """要点2: 双 ctx 收到同一 deal_id → 只落一行, 只通知一次."""
    sub, sent = _subscriber(monkeypatch, tmp_db)
    _feed(sub, _push_row("3333333333333333333"))   # US ctx 先到
    _feed(sub, _push_row("3333333333333333333"))   # HK ctx 后到 (同 deal_id)
    assert len(live_ledger.read_all_deals()) == 1
    assert sub.stats["real_inserted"] == 1
    assert sub.stats["real_updated"] == 1
    assert sent == ["3333333333333333333"]  # 第二次 updated 不重复通知


def test_push_and_sync_share_ssot(tmp_db, monkeypatch):
    """push 先落, 每日 sync 再到同一 deal_id → upsert 不产生重复行 (SSOT 共用验证)."""
    sub, _ = _subscriber(monkeypatch, tmp_db)
    _feed(sub, _push_row("4444444444444444444"))
    # 模拟 sync 路径直接调共享 mapper
    conn = live_ledger._connect()
    existing = {r[0] for r in conn.execute("SELECT deal_id FROM live_deal_records")}
    res = live_ledger._upsert_deal_row(
        conn, _push_row("4444444444444444444"), "2026-09-09T00:00:00+00:00", existing)
    conn.commit(); conn.close()
    assert res == "updated"
    assert len(live_ledger.read_all_deals()) == 1


def test_hk_market_field_trd_market(tmp_db, monkeypatch):
    """要点3: push 行市场字段是 trd_market (非 deal_market) → HK 正确识别."""
    sub, _ = _subscriber(monkeypatch, tmp_db)
    _feed(sub, _push_row("5555555555555555555", code="HK.00700", market_field="trd_market", market="HK"))
    rows = live_ledger.read_all_deals()
    assert len(rows) == 1
    assert rows[0]["market"] == "HK"
    assert rows[0]["currency"] == "HKD"


def test_deal_id_19digit_preserved_as_text(tmp_db, monkeypatch):
    """要点4: 19 位 deal_id 经 DataFrame 仍是 TEXT, 幂等键不失效."""
    sub, _ = _subscriber(monkeypatch, tmp_db)
    big = "4657813206881206341"  # 真实量级 19 位 > 2^53
    _feed(sub, _push_row(big))
    rows = live_ledger.read_all_deals()
    assert rows[0]["deal_id"] == big  # 未变位
    # 再喂同 id 必须命中去重而非新增
    _feed(sub, _push_row(big))
    assert len(live_ledger.read_all_deals()) == 1


def test_record_push_exception_swallowed(tmp_db, monkeypatch):
    """要点5: 落库抛错 → handler 不上抛 (不打断 SDK recv 循环), 计入 errors."""
    sub, _ = _subscriber(monkeypatch, tmp_db)
    monkeypatch.setattr(live_ledger, "record_deal_push",
                        lambda raw: (_ for _ in ()).throw(RuntimeError("db locked")))
    _feed(sub, _push_row("6666666666666666666"))  # 不得抛出
    assert sub.stats["errors"] == 1


def test_notify_failure_does_not_block_ledger(tmp_db, monkeypatch):
    """要点6: 飞书通知失败 → 成交仍已落账 (通知是旁路)."""
    sub, _ = _subscriber(monkeypatch, tmp_db)
    def _boom(raw, deal_id):
        raise RuntimeError("feishu down")
    monkeypatch.setattr(sub, "_notify_async", _boom)
    _feed(sub, _push_row("7777777777777777777"))
    assert len(live_ledger.read_all_deals()) == 1  # 落账成功
    assert sub.stats["real_inserted"] == 1


def test_disabled_skips_subscription(monkeypatch):
    """开关 FTSEEK_LIVE_DEAL_PUSH=0 → start_live_deal_push 直接返回 False, 不建 ctx."""
    monkeypatch.setenv("FTSEEK_LIVE_DEAL_PUSH", "0")
    monkeypatch.setattr(ldp, "_instance", None)
    assert ldp.push_enabled() is False
    assert ldp.start_live_deal_push() is False
    assert ldp.get_live_deal_push() is None  # 未建实例
