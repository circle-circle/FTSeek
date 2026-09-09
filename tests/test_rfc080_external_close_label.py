# -*- coding: utf-8 -*-
"""RFC-080 demix2: external_close trade_mode 标签跟随 AUTO_TRADE_ACCOUNT 单测.

钉锁 (broker REAL 成交史实锤 2026-09-09):
  live 库 4 行 external_close (MSFT#900/AVGO#901/TTE#902/BULL#904) 零 REAL 成交证据
  = sim 仓被 position_sync INSERT 硬编码 trade_mode='live' 误标 → RFC-078 迁移
  按标签搬进 live 库 → sim/live 混同。已清 (demix2_broker_verified.py)。

  1. 标签语义: AUTO_TRADE_ACCOUNT=sim → 'simulate'; =real → 'live'
  2. 源码锚点: external_close INSERT 使用 _trade_mode_label, 不再硬编码 'live'
  3. 查重守卫不按 trade_mode 过滤 → 标签变更不破坏 executor↔position_sync 去重
"""
import os
import re
from pathlib import Path

PS = Path(__file__).resolve().parents[1] / "app" / "core" / "position_sync.py"


def _label(auto_acc: str) -> str:
    """与 position_sync external_close 插入点同款逻辑 (源码锚点钉住一致性)."""
    return 'live' if auto_acc.strip().lower() == "real" else 'simulate'


class TestExternalCloseLabel:
    def test_sim_account_labels_simulate(self, monkeypatch):
        monkeypatch.setenv("AUTO_TRADE_ACCOUNT", "sim")
        assert _label(os.getenv("AUTO_TRADE_ACCOUNT", "sim")) == "simulate"

    def test_real_account_labels_live(self, monkeypatch):
        monkeypatch.setenv("AUTO_TRADE_ACCOUNT", "real")
        assert _label(os.getenv("AUTO_TRADE_ACCOUNT", "sim")) == "live"

    def test_default_is_simulate(self, monkeypatch):
        monkeypatch.delenv("AUTO_TRADE_ACCOUNT", raising=False)
        assert _label(os.getenv("AUTO_TRADE_ACCOUNT", "sim")) == "simulate"

    def test_source_anchor_insert_uses_variable(self):
        src = PS.read_text(encoding="utf-8")
        # 标签变量在 external_close 写入块定义
        assert "_trade_mode_label = 'live' if _auto_acc_ec" in src, \
            "external_close trade_mode 标签变量丢失 (RFC-080 demix2)"
        # INSERT 参数表使用变量, 不再硬编码 'live'
        m = re.search(r"'external_close', 'POSITION_SYNC', 'manual', (\w+),", src)
        assert m, "external_close INSERT 参数行未找到 (结构漂移?)"
        assert m.group(1) == "_trade_mode_label", \
            f"INSERT 第14参 = {m.group(1)}, 期望 _trade_mode_label (防回归硬编码 'live')"

    def test_dedup_guard_not_trade_mode_dependent(self):
        """查重守卫按自然键+exit_price>0 判重, 不得引入 trade_mode 条件
        (标签变更不得破坏 executor↔position_sync 双端去重)."""
        src = PS.read_text(encoding="utf-8")
        i = src.find("查重守卫 (2026-08-21")
        assert i >= 0
        block = src[i:i + 2600]
        assert "trade_mode" not in block, "查重守卫出现 trade_mode 条件 — 会因两端口径差异漏判重"
