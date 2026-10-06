"""tests/playbook 共享隔离 — 生产运维态文件不得泄漏进测试.

2026-10-06 立项根因 (P0 lane 隔离修复期间实证):

`app/playbook/live_arm_gate.read_live_arm_halt()` 读模块级
`_HALT_FILE = <repo>/data/live_arm_halt.json` —— 那是**运维可写的生产止损态
文件** (值机人落 HALT 暂停 LIVE 臂用)。测试只要走到 LIVE gate 就会读它:

    2026-10-05 运维落 HALT (lane 隔离 P0 止血)
    ⇒ test_dual_arm_routing::test_allowed_live_gate_writes_mirror_once 恒红
       (LIVE_NEW_POSITIONS_HALTED)
    ⇒ test_m2_c2_live_quote_gate 11 条恒红
    ⇒ test_exit_compensation 2 条恒红
    共 14 条, 红因与任何代码改动无关 (实证: 临时移出该文件 → 43 passed)

这些用例已 monkeypatch 了 config / market_phase / open positions 等全部确定
输入, 唯独 HALT 走真实文件 ⇒ 生产运维动作能凭空运红 CI, 且红因极具误导性
(看起来像 gate 逻辑坏了)。

修法选 conftest 而非逐文件加 fixture: 同型缺口散布 3+ 个文件, 且走 LIVE gate
的测试有 13 个文件 (grep 实证), 逐个补是复制粘贴。隔离手法与既有
tests/playbook/test_live_arm_p0_contracts.py:116 同款 (指向 tmp_path 空文件),
`read_live_arm_halt()` 遇 FileNotFoundError 返回 {} = 未 HALT。

作用域刻意限 tests/playbook/: 上层 tests/conftest.py 当时是并行 agent WIP,
不代改; 实测受影响的 14 条全部在本目录。
"""

import json
from datetime import timedelta

import pytest

from app.utils.timezone import now_et


@pytest.fixture(autouse=True)
def _isolate_live_arm_halt_file(monkeypatch, tmp_path):
    """把 LIVE 臂 HALT 态文件指向空 tmp_path, 使测试与生产运维态解耦."""
    monkeypatch.setattr(
        "app.playbook.live_arm_gate._HALT_FILE",
        tmp_path / "live_arm_halt.json",
    )


@pytest.fixture(autouse=True)
def _isolate_live_probe_waiver_file(monkeypatch, tmp_path):
    """把四锁破例预注册载体指向 tmp_path, 并默认写成**有效**破例.

    2026-10-06 立项 (RFC-135, 用户裁 A): gate 新增 LIVE_PROBE_WAIVER_INACTIVE 判据 —
    allow_real_trading=True 时必须有**有效的**破例文件才放行。两个坑都要防:

    (a) 生产载体 data/live_probe_lock_waiver.json 带 expires=2026-10-12T16:00:00-04:00,
        若测试读它, 所有传 allow_real_trading=True 的用例会在该时刻起集体转红
        (与 tests/test_daily_violation_scan.py 曾有的"硬编码日期定时炸弹"同型)。
    (b) 若默认指向**不存在**的文件 (fail-close 语义), 走 LIVE 臂的 14 条用例会
        因 gate 拒绝而在下游断言处炸 (mirror_id=None / KeyError error_code),
        红因与用例意图无关 ⇒ 实测 2026-10-06: 14 failed。

    ⇒ 默认写"有效破例", 让 LIVE 臂用例照原意图跑; 到期/缺失/坏文件语义由
      tests/playbook/test_live_probe_lock_waiver.py 显式覆盖 (那里自写文件)。
    到期日用**相对当前时间**算 (+3650天), 不写死绝对日期 ⇒ 永不过期, 无定时炸弹。
    """
    waiver_path = tmp_path / "live_probe_lock_waiver.json"
    expires = (now_et() + timedelta(days=3650)).isoformat(timespec="seconds")
    waiver_path.write_text(json.dumps({
        "waiver_id": "TEST-WAIVER",
        "authorization_id": "TEST-AUTH",
        "granted": "2026-10-06",
        "granted_by": "tests/playbook/conftest.py (测试夹具, 非生产授权)",
        "locks_waived": {"ALLOW_REAL_TRADING": "true"},
        "expires": expires,
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr("app.playbook.live_arm_gate._WAIVER_FILE", waiver_path)
