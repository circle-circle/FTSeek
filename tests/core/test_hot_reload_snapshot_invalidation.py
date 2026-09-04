"""热重载后进程内快照必须失效 (2026-08-28 全链路复查).

缺陷: ConfigHotReloader 监听 unified_config.yaml, 回调只同步 ConfigContainer;
而 PlaybookConfig._config / FeatureFlags._flags 在构造时就把同一份 yaml 抄进进程内
字典 (前者只在 playbook_horizon.yaml mtime 变化时重建, 后者的 reload() 全仓零调用方)。
后果: 改 playbook.execution_parameters.* 或 deployment.feature_flags.* 后日志报
"✅ 配置已热重载", 实盘入场/退出链路仍按旧值跑 —— 「日志正常但逻辑失效」。

回滚: 删 main.py 回调里的 invalidate_config_snapshots() 调用即恢复旧行为 (本组测试会红)。
"""

import sys

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))

from app.config_mgmt.hot_reloader import invalidate_config_snapshots  # noqa: E402


class _StubFlags:
    def __init__(self):
        self.reload_calls = 0

    def reload(self):
        self.reload_calls += 1


class _StubContainer:
    """冒充 ConfigContainer: 只暴露 PlaybookConfig._load 用到的 get_dotted。"""

    def __init__(self, playbook_section):
        self._playbook = playbook_section

    def get_dotted(self, path, default=None):
        return self._playbook if path == "playbook" else default


@pytest.fixture
def _clean_pbcache():
    """隔离 PlaybookConfig / FeatureFlags 两个全局, 测试后还原 (避免污染其他套件)。

    DEB-20260904: setup 段也必须清零 — 此前只在 teardown 还原, 若先跑的套件
    (如 tests/playbook 直接调真 get_playbook_config) 留下了非空单例, 本文件
    测试会读到陈旧真身而非 monkeypatch 的桩容器 (全量按字母序 core 先于
    playbook 运行所以不炸, 反向顺序才暴露)。
    """
    from app.core import feature_flags as ff
    from app.playbook import config as pbc

    saved_pbc = pbc._playbook_config
    saved_flags = ff.FeatureFlags._instance
    pbc._playbook_config = None
    ff.FeatureFlags._instance = None
    yield
    pbc._playbook_config = saved_pbc
    ff.FeatureFlags._instance = saved_flags


def test_invalidate_resets_both_consumers(_clean_pbcache, monkeypatch):
    """核心契约: 快照消费者都被失效, 且返回名列表可断言。"""
    from app.core import feature_flags as ff
    from app.playbook import config as pbc
    from app.utils import watchlist_manager as wlm

    pbc._playbook_config = pbc.PlaybookConfig()
    assert pbc._playbook_config is not None

    stub = _StubFlags()
    monkeypatch.setattr(ff, "get_feature_flags", lambda: stub)
    # DEB-20260903 #3: WatchlistManager 也纳入热重载失效, 打桩避免读真实 watchlist 文件
    wl_stub = _StubFlags()
    monkeypatch.setattr(wlm, "get_watchlist_manager", lambda: wl_stub)

    done = invalidate_config_snapshots()

    assert done == ["PlaybookConfig", "FeatureFlags", "WatchlistManager"]
    assert pbc._playbook_config is None, "PlaybookConfig 单例未被丢弃"
    assert stub.reload_calls == 1, "FeatureFlags.reload() 未被调用"
    assert wl_stub.reload_calls == 1, "WatchlistManager.reload() 未被调用"


def test_playbook_config_is_stale_without_invalidation(_clean_pbcache, monkeypatch):
    """机制证明: 只改 ConfigContainer 不失效快照 → 链路仍读旧值 (缺陷本身)。"""
    from app.core.config_container import ConfigContainer
    from app.playbook import config as pbc

    section = {"execution_parameters": {"daily_notional_cap_pct": 0.5}}
    monkeypatch.setattr(ConfigContainer, "get_instance", classmethod(lambda cls: _StubContainer(section)))

    cfg = pbc.get_playbook_config()
    assert cfg.get("daily_notional_cap_pct") == 0.5

    section["execution_parameters"]["daily_notional_cap_pct"] = 0.9  # 模拟"改了 yaml + ConfigContainer 已 reload"
    assert pbc.get_playbook_config().get("daily_notional_cap_pct") == 0.5, \
        "快照应当是陈旧的 — 若不陈旧, 说明已有别的失效路径, 本测试前提需重审"


def test_invalidation_makes_new_value_visible(_clean_pbcache, monkeypatch):
    """修复证明: 走 invalidate_config_snapshots() 后同一变更即刻可见。"""
    from app.core.config_container import ConfigContainer
    from app.playbook import config as pbc

    section = {"execution_parameters": {"daily_notional_cap_pct": 0.5}}
    monkeypatch.setattr(ConfigContainer, "get_instance", classmethod(lambda cls: _StubContainer(section)))

    assert pbc.get_playbook_config().get("daily_notional_cap_pct") == 0.5
    section["execution_parameters"]["daily_notional_cap_pct"] = 0.9

    done = invalidate_config_snapshots()
    assert "PlaybookConfig" in done
    assert pbc.get_playbook_config().get("daily_notional_cap_pct") == 0.9


def test_one_consumer_failing_does_not_block_the_other(_clean_pbcache, monkeypatch, caplog):
    """一个失效失败只降级为 WARNING, 其余照常, 且不向调用方抛异常。"""
    from app.core import feature_flags as ff
    from app.playbook import config as pbc
    from app.utils import watchlist_manager as wlm

    def _boom():
        raise RuntimeError("reset failed")

    monkeypatch.setattr(pbc, "reset_playbook_config", _boom)
    stub = _StubFlags()
    monkeypatch.setattr(ff, "get_feature_flags", lambda: stub)
    wl_stub = _StubFlags()
    monkeypatch.setattr(wlm, "get_watchlist_manager", lambda: wl_stub)

    done = invalidate_config_snapshots()

    # PlaybookConfig 失效失败 → 不在 done; 其余两个照常
    assert done == ["FeatureFlags", "WatchlistManager"]
    assert stub.reload_calls == 1


def test_main_callback_actually_wires_the_invalidator():
    """防"修了函数忘了接线" — main.py 的热重载回调必须调用 invalidate_config_snapshots。"""
    src = (__import__("pathlib").Path(__file__).resolve().parents[2] / "main.py").read_text(encoding="utf-8")
    start = src.index("def _on_config_reload()")
    body = src[start:start + 1600]
    assert "invalidate_config_snapshots()" in body, (
        "main.py 热重载回调未失效进程内快照 — 改 unified_config.yaml 后 PlaybookConfig/"
        "FeatureFlags 仍读旧值"
    )

    # 锁住顺序: 先同步 ConfigContainer, 再失效快照 (反序会让重建读到旧容器)
    assert "ConfigContainer().reload()" in body
    assert body.index("ConfigContainer().reload()") < body.index("invalidate_config_snapshots()")
