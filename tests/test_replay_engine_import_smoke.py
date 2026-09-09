"""官方回放引擎 import 冒烟钉子 (2026-09-07, DEB-20260907)。

为什么需要: `docs/REPLAY-SCRIPT-LEDGER.md` 的公约要求"新写回放脚本必须
`import exit_replay_backtest` 取原语", 但**没有任何测试钉住引擎本身可 import** ⇒
P2a 提交 14e9a544 (2026-09-06) 把 `_compute_dynamic_effective_sl` 本体逐字迁入
`app.playbook.exit_engine` 并撤掉 `app/playbook/pipeline.py` 侧转发器时, 引擎当场
ImportError, 而全量回归 0 红 (3200 passed), 无人发现。

被打穿的面 (实测):
  * 模块级硬断: limit_entry_counterfactual / entry_gate_replay / be_counterfactual_replay
  * 懒 import 断 (cron 活体, 只在补拉/补盲分支触发 ⇒ 静默失败):
      archetype_bars_ensure.py:59   (cron 35 5 * * 2-6)
      post_exit_path_shadow.py:97   (cron 45 5 * * 2-6)
      position_path_collector.py:70 (cron 15 5 * * 2-6)
    活体证据: data/shadow/post_exit_path.jsonl 末次成功写入 2026-09-05 05:45。

钉子只测"可 import + 原语在位", 不跑回放 (不碰网络、不碰生产 data/)。
回滚: 删本文件 (公约回到无牙状态)。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

MODULE_LEVEL = ["exit_replay_backtest", "limit_entry_counterfactual",
                "entry_gate_replay", "be_counterfactual_replay"]
LAZY_IMPORT = ["archetype_bars_ensure", "post_exit_path_shadow", "position_path_collector"]
PRIMITIVES = ["load_trades", "fetch_bars", "replay_trade", "aggregate", "_parse_et"]


@pytest.mark.parametrize("name", MODULE_LEVEL)
def test_module_level_consumers_import(name):
    importlib.import_module(name)


@pytest.mark.parametrize("name", LAZY_IMPORT)
def test_lazy_import_consumers_import(name):
    """懒 import 的消费方: 模块自身可 import, 且它依赖的引擎原语真实存在。

    这三处把 `from exit_replay_backtest import fetch_bars` 写在函数体内 ⇒ 引擎断掉时
    模块级 import 照样绿, 只在 cron 跑到补拉分支那一刻抛, 除日志外无人看见。
    """
    importlib.import_module(name)
    src = (REPO / "scripts" / f"{name}.py").read_text(encoding="utf-8")
    assert "exit_replay_backtest" in src, f"{name} 不再复用官方引擎 ⇒ 违反台账公约"
    erb = importlib.import_module("exit_replay_backtest")
    assert hasattr(erb, "fetch_bars"), f"{name} 依赖的原语 fetch_bars 不在引擎上"


def test_engine_primitives_present():
    """公约承诺的原语清单必须在位 (缺一个 = 复用方各自手搓 = 公约失效)。"""
    erb = importlib.import_module("exit_replay_backtest")
    missing = [p for p in PRIMITIVES if not hasattr(erb, p)]
    assert not missing, f"官方引擎缺原语: {missing}"


def test_engine_exit_math_is_the_ssot_object():
    """引擎用的退出数学必须是 exit_engine 本体对象, 不是某处副本或转发器。

    转发器是搬家期兼容层, 随时可能被撤 (P2a 已撤过一个) ⇒ 依赖它 = 定时炸弹。
    """
    erb = importlib.import_module("exit_replay_backtest")
    from app.playbook import exit_engine
    assert erb._compute_dynamic_effective_sl is exit_engine._compute_dynamic_effective_sl
    assert erb._get_factor_exit_config is exit_engine._get_factor_exit_config
    src = (REPO / "scripts" / "exit_replay_backtest.py").read_text(encoding="utf-8")
    assert "from app.playbook.exit_engine import" in src
