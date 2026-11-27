"""RFC-218 §22 (P2): 黑名单 getter 内部吞异常的可见性契约。

病灶 (独立审计 2026-11-27): _get_global_blacklist 内部异常只打 debug 并返回 [] ⇒
外层守卫把「配置异常」当「正常空名单」, 外层 warning 接不到真实异常。
修复 = warning + exc_info (栈); **行为保持 fail-open 返回空列表** (异常即停新仓须另行裁决)。
钉子: 真实 getter + 真实 get_config 抛异常路径 (不是桩 _get_global_blacklist 本身)。
"""
import logging

from app.utils.watchlist_manager import WatchlistManager


def _bare_manager():
    # _get_global_blacklist 不依赖实例状态; 跳过 __init__ 的名单 IO
    return WatchlistManager.__new__(WatchlistManager)


def test_internal_exception_warns_with_stack_and_stays_fail_open(monkeypatch, caplog):
    def _boom():
        raise RuntimeError("config container down")
    monkeypatch.setattr("app.core.config_container.get_config", _boom)
    with caplog.at_level(logging.DEBUG, logger="app.utils.watchlist_manager"):
        out = _bare_manager()._get_global_blacklist()
    assert out == []                                   # fail-open 行为不变 (本批不改 fail-close)
    recs = [r for r in caplog.records if "[BLACKLIST]" in r.getMessage()]
    assert recs, "内部吞异常必须对外可见 (旧实现只有 debug)"
    assert recs[0].levelno == logging.WARNING          # 不是 debug
    assert recs[0].exc_info is not None                # 带栈, 外层可接真实异常
    assert "config container down" in recs[0].getMessage()


def test_get_dotted_exception_also_visible(monkeypatch, caplog):
    class _Cfg:
        def get_dotted(self, key, default=None):
            raise ValueError("yaml corrupt")
    monkeypatch.setattr("app.core.config_container.get_config", lambda: _Cfg())
    with caplog.at_level(logging.DEBUG, logger="app.utils.watchlist_manager"):
        out = _bare_manager()._get_global_blacklist()
    assert out == []
    recs = [r for r in caplog.records if "[BLACKLIST]" in r.getMessage()]
    assert recs and recs[0].levelno == logging.WARNING and recs[0].exc_info is not None


def test_normal_path_unchanged(monkeypatch, caplog):
    class _Cfg:
        def get_dotted(self, key, default=None):
            assert key == "risk.global_blacklist"
            return ["US.QCOM", "LCID", "US.HK.00700"]
    monkeypatch.setattr("app.core.config_container.get_config", lambda: _Cfg())
    with caplog.at_level(logging.DEBUG, logger="app.utils.watchlist_manager"):
        out = _bare_manager()._get_global_blacklist()
    assert out == ["QCOM", "LCID", "HK.00700"]         # 剥 US. 口径不变
    assert not [r for r in caplog.records if "[BLACKLIST]" in r.getMessage()]


def test_empty_config_is_silent_normal_empty(monkeypatch, caplog):
    class _Cfg:
        def get_dotted(self, key, default=None):
            return []
    monkeypatch.setattr("app.core.config_container.get_config", lambda: _Cfg())
    with caplog.at_level(logging.DEBUG, logger="app.utils.watchlist_manager"):
        out = _bare_manager()._get_global_blacklist()
    assert out == []
    assert not [r for r in caplog.records if "[BLACKLIST]" in r.getMessage()]
