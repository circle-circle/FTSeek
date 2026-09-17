#!/usr/bin/env python3
"""F7 回归护栏: trade_records 的**每个写入方都必须显式写 config_version** (2026-09-18 用户令「修」).

背景: trade_records.config_version 的 schema 默认值是 'v1' (app/playbook/db.py), 规范写入方
app/storage/trade_store.py 显式写 'v2'; 延长时段开仓写方与 position_sync 写方此前漏写本列 ⇒
落默认 'v1' ⇒ W1 冻结窗"版本分裂"假象 + 让 OKR 版本分层统计读错 (edge_weekly_report 以
`r["config_version"] or "v1"` 兜底, 进一步把缺口掩盖成合法值).

本测试扫描全仓 `INSERT INTO trade_records` 的写入点, 断言:
  ① 列清单里必须出现 config_version (不写 = 落默认值, 静默错版本);
  ② 写入值必须是显式 'v2' (与规范写入方一致)。
新增写入方漏写时本测试直接失败 —— 防复发 (同类缺口 2026-09-18 曾在两处同时出现)。
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
WANT_VERSION = "'v2'"
# 非当期写入方: 一次性修复/对账脚本, 不参与常规链路 (登记即视作已知例外)
ALLOW_KNOWN = {"scripts/demix2_broker_verified.py"}


def _iter_insert_blocks(path):
    """把每个 `INSERT INTO trade_records` 起、到该语句字符串末尾（含参数元组前的 SQL 部分）的文本切出来."""
    text = path.read_text(errors="ignore")
    out = []
    for m in re.finditer(r"INSERT\s+(?:OR\s+REPLACE\s+)?INTO\s+trade_records", text, flags=re.I):
        out.append(text[m.start(): m.start() + 1400])
    return out


def test_every_trade_records_writer_sets_config_version():
    offenders_col = []
    offenders_val = []
    scanned = 0
    for path in list(ROOT.glob("app/**/*.py")) + list(ROOT.glob("scripts/**/*.py")):
        if "__pycache__" in str(path):
            continue
        rel = str(path.relative_to(ROOT))
        for block in _iter_insert_blocks(path):
            scanned += 1
            # 只看列清单 + VALUES 段（到参数元组为止）
            head = re.split(r"\\n\s*\)\s*,|""" + r"\n\s*\(", block)[0]
            if "config_version" not in head.split("VALUES")[0]:
                if rel not in ALLOW_KNOWN:
                    offenders_col.append(rel)
            elif WANT_VERSION not in head:
                if rel not in ALLOW_KNOWN:
                    offenders_val.append(rel)
    assert scanned >= 4, f"扫描到的 trade_records 写入点过少 ({scanned}) —— 正则或路径失效, 测试失去意义"
    assert not offenders_col, (
        "以下写入方未显式写 config_version（会落 schema 默认 'v1'）: " + ", ".join(sorted(set(offenders_col)))
    )
    assert not offenders_val, (
        f"以下写入方的 config_version 不是 {WANT_VERSION}: " + ", ".join(sorted(set(offenders_val)))
    )


def test_registry_files_are_consistent():
    """规范写入方 (trade_store) 与两个补修写方必须同值 —— 防日后单边改版本."""
    files = {
        "app/storage/trade_store.py": "'v2',?,?)",
        "scripts/extended_hours_entry_executor.py": "exit_time, config_version)",
        "app/core/position_sync.py": "close_futu_order_id, config_version)",
    }
    for rel, needle in files.items():
        text = (ROOT / rel).read_text(errors="ignore")
        assert needle in text, f"{rel} 的 config_version 写入锚点丢失: {needle!r}"
        assert WANT_VERSION in text, f"{rel} 未显式写 {WANT_VERSION}"