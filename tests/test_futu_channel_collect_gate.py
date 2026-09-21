"""采集闸契约测试 (2026-09-21): "无效就停"必须**显式**、且不能从判据状态反推。

背景: 四路富途影子采集原先只有总闸 + 三条零散子开关, capital_flow 连独立开关都没有。
复核时发现两个方向都踩过坑:
  · iv_hv / 异动榜: 判据已出结论却继续花 IO;
  · capital_flow: "判据 FAIL" 被误当成 "数据没用" ⇒ 差点停掉**有生产消费者**的料
    (喂 app/playbook/intraday_reentry.py 盘中资金背离门, 停采 = 门 fail-open 退化)。
本测试把三条语义钉死。
"""
import importlib.util
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "fcs_collect_under_test", REPO / "scripts" / "futu_channel_shadow_collect.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _Cfg:
    def __init__(self, vals):
        self.vals = vals

    def get_dotted(self, key, default=None):
        return self.vals.get(key, default)


def _patch_cfg(monkeypatch, vals):
    import app.core.config_container as cc
    monkeypatch.setattr(cc, "get_config", lambda: _Cfg(vals))


def test_gate_blocks_when_collection_retired(monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "_registry_entry",
                        lambda cid: types.SimpleNamespace(collection_retired=True))
    assert m._collector_gate("futu_channel_iv_hv",
                             "futu_channels.iv_collect_enabled") is False


def test_gate_blocks_when_switch_off(monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "_registry_entry",
                        lambda cid: types.SimpleNamespace(collection_retired=False))
    _patch_cfg(monkeypatch, {"futu_channels.iv_collect_enabled": False})
    assert m._collector_gate("futu_channel_iv_hv",
                             "futu_channels.iv_collect_enabled") is False


def test_gate_allows_when_default_on_and_not_retired(monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "_registry_entry",
                        lambda cid: types.SimpleNamespace(collection_retired=False))
    _patch_cfg(monkeypatch, {})
    assert m._collector_gate("futu_channel_ofi_baseline",
                             "futu_channels.ofi_collect_enabled") is True


def test_terminal_status_alone_must_not_stop_collection(monkeypatch):
    """⚠️ 反推禁令: 登记状态 KILLED (= 研究判据退役) **不等于**采集该停。

    实锤: capital_flow 的原料在喂生产门; 若按状态自动停采 → 静默断料。
    """
    m = _load()
    monkeypatch.setattr(
        m, "_registry_entry",
        lambda cid: types.SimpleNamespace(collection_retired=False, status="KILLED"))
    _patch_cfg(monkeypatch, {})
    assert m._collector_gate("futu_channel_capital_flow",
                             "futu_channels.capital_flow_collect_enabled") is True


def test_run_collect_applies_gate_per_channel(monkeypatch):
    """端到端: run_collect 只调用过闸的采集函数 (停采的通道一次都不碰 OpenD)。"""
    m = _load()
    called = []
    monkeypatch.setattr(m, "_enabled", lambda: True)
    monkeypatch.setattr(m, "_load_state", lambda: {})
    monkeypatch.setattr(m, "_save_state", lambda st: None)
    monkeypatch.setattr(m, "_interval_sec", lambda: 300)
    monkeypatch.setattr(m, "_active_symbols", lambda sch: ["US.AAPL"])
    monkeypatch.setattr(m, "_connect_ctx", lambda: types.SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(m, "_collect_capital_flow", lambda ctx, s: called.append("flow") or 1)
    monkeypatch.setattr(m, "_collect_ofi_baseline", lambda ctx, s: called.append("ofi") or 1)
    monkeypatch.setattr(m, "_collect_iv_hv", lambda ctx, s: called.append("iv") or 1)
    monkeypatch.setattr(m, "_collect_top_movers", lambda ctx: called.append("movers") or 1)
    # 只留 capital_flow 开, 其余三路停采
    _patch_cfg(monkeypatch, {
        "futu_channels.capital_flow_collect_enabled": True,
        "futu_channels.ofi_collect_enabled": False,
        "futu_channels.iv_collect_enabled": False,
        "futu_channels.movers_shadow_enabled": False,
    })
    m.run_collect(phase="TRADING")
    assert called == ["flow"], f"只应调 capital_flow, 实际 {called}"

    # 全开时四路都调 (确认闸没把该采的也拦了)
    called.clear()
    _patch_cfg(monkeypatch, {
        "futu_channels.capital_flow_collect_enabled": True,
        "futu_channels.ofi_collect_enabled": True,
        "futu_channels.iv_collect_enabled": True,
        "futu_channels.movers_shadow_enabled": True,
    })
    m.run_collect(phase="TRADING")
    assert called == ["flow", "ofi", "movers"], f"TRADING 段应调 flow/ofi/movers, 实际 {called}"
    called.clear()
    m.run_collect(phase="AFTER_HOURS")
    assert called == ["flow", "ofi", "iv"], f"AFTER_HOURS 段应调 flow/ofi/iv, 实际 {called}"