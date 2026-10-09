# -*- coding: utf-8 -*-
"""auto-trade 开关持久化原子性契约 (2026-10-08 运维体检批).

病根: /api/auto-trade/toggle 原实现先改内存、后写 YAML, 且忽略写盘返回值 —
YAML 写失败 (key 缺失/缩进漂移/IO) 时接口仍报 success。最坏形态:
用户关闭自动交易 → 写盘失败 → 重启后 ModeSwitchService 从 YAML 恢复成 ON
(开关"复活"), 且内存/YAML 两源当场分叉 (scan_loop 读内存, 三处裸 regex 直读 YAML)。

钉四件事:
  1. 写盘成功: 原子替换 (tmp + os.replace), 只动 auto_trading_enabled 一行;
  2. key 缺失/缩进漂移: 返回 False, 文件字节不动 (不得半写);
  3. IO 异常 (replace 失败): 返回 False, tmp 清理, 原文件保留;
  4. 端点顺序: YAML 失败 → 500 AUTO_TRADE_PERSIST_ERROR 且内存 set 不被调用;
     YAML 成功 → 内存 set 才执行 (持久层先行)。
"""
import asyncio
import json
import os
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.web_dashboard.routes import trade as trade_routes


YAML_SNIPPET = """feature_flags:
  auto_trading_enabled: true
  auto_aggression: conservative
"""


class _FakeRequest:
    def __init__(self, enabled):
        self._body = {"enabled": enabled}

    async def json(self):
        return self._body


def _register_once():
    """在模块级 router 上注册路由并取回 toggle 端点函数 (幂等: 已注册则复用)."""
    existing = {getattr(r, "path", None) for r in trade_routes.router.routes}
    if "/api/auto-trade/toggle" not in existing:
        trade_routes.register(MagicMock(), MagicMock())
    for route in reversed(trade_routes.router.routes):
        if getattr(route, "path", None) == "/api/auto-trade/toggle":
            return route.endpoint
    raise AssertionError("toggle 路由未注册")


def _decode_response(response):
    body = json.loads(response.body.decode("utf-8"))
    return response.status_code, body


# ── 1-3: 持久化函数本体 ──────────────────────────────────────────

def test_write_yaml_success_is_atomic_and_targeted(tmp_path, monkeypatch):
    yaml_path = tmp_path / "unified_config.yaml"
    yaml_path.write_text(YAML_SNIPPET, encoding="utf-8")
    monkeypatch.setattr(trade_routes, "_AUTO_TRADING_YAML_PATH", yaml_path)

    assert trade_routes._write_yaml_auto_trading(False) is True
    new_text = yaml_path.read_text(encoding="utf-8")
    assert "  auto_trading_enabled: false" in new_text
    # 只动目标行 — 其余行逐字节保留 (regex 全文重写不得误伤)
    assert "  auto_aggression: conservative" in new_text
    assert list(tmp_path.glob("*.tmp")) == []  # 原子写不留残骸


def test_missing_key_returns_false_and_preserves_file(tmp_path, monkeypatch):
    yaml_path = tmp_path / "unified_config.yaml"
    original = "feature_flags:\n  auto_aggression: conservative\n"
    yaml_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(trade_routes, "_AUTO_TRADING_YAML_PATH", yaml_path)

    assert trade_routes._write_yaml_auto_trading(True) is False
    assert yaml_path.read_text(encoding="utf-8") == original


def test_replace_failure_cleans_tmp_and_preserves_file(tmp_path, monkeypatch):
    yaml_path = tmp_path / "unified_config.yaml"
    yaml_path.write_text(YAML_SNIPPET, encoding="utf-8")
    monkeypatch.setattr(trade_routes, "_AUTO_TRADING_YAML_PATH", yaml_path)

    real_replace = os.replace

    def fail_replace(source, target):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(trade_routes.os, "replace", fail_replace)
    assert trade_routes._write_yaml_auto_trading(False) is False
    monkeypatch.setattr(trade_routes.os, "replace", real_replace)

    assert "auto_trading_enabled: true" in yaml_path.read_text(encoding="utf-8")
    assert list(tmp_path.glob("*.tmp")) == []


# ── 4: 端点顺序契约 (持久层先行, 失败拒绝且内存不动) ───────────

def test_toggle_rejects_when_yaml_persist_fails(monkeypatch):
    endpoint = _register_once()
    set_calls = []

    class _ModeService:
        def set_auto_enabled(self, enabled):
            set_calls.append(enabled)
            return {"success": True, "auto_enabled": enabled}

    monkeypatch.setattr(trade_routes, "_write_yaml_auto_trading", lambda enabled: False)
    monkeypatch.setattr(trade_routes, "get_mode_switch_service", lambda: _ModeService())
    monkeypatch.setattr(
        trade_routes, "get_data_service",
        lambda: (_ for _ in ()).throw(AssertionError("失败路径不得清缓存")))

    status, body = _decode_response(asyncio.run(endpoint(_FakeRequest(False))))
    assert status == 500
    assert body["error"]["code"] == "AUTO_TRADE_PERSIST_ERROR"
    assert set_calls == []  # 内存开关未被翻动


def test_toggle_sets_memory_only_after_yaml_success(monkeypatch):
    endpoint = _register_once()
    set_calls = []

    class _ModeService:
        def set_auto_enabled(self, enabled):
            set_calls.append(enabled)
            return {"success": True, "auto_enabled": enabled, "toggled_at": "now"}

    monkeypatch.setattr(trade_routes, "_write_yaml_auto_trading", lambda enabled: True)
    monkeypatch.setattr(trade_routes, "get_mode_switch_service", lambda: _ModeService())
    monkeypatch.setattr(
        trade_routes, "get_data_service", MagicMock())

    status, body = _decode_response(asyncio.run(endpoint(_FakeRequest(False))))
    assert status == 200
    assert body["ok"] is True
    assert set_calls == [False]  # 持久层成功后才翻内存


def test_source_contract_no_swallowed_persist_result():
    """源码级钉子: 不得回潮 `_write_yaml_auto_trading(enabled)` 裸调用忽略返回值。"""
    source = Path(trade_routes.__file__).read_text(encoding="utf-8")
    assert not re.search(
        r"^\s*_write_yaml_auto_trading\([^)]*\)\s*$", source, re.MULTILINE
    ), "toggle 端点又出现忽略持久化返回值的裸调用"
