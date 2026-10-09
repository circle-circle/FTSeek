# -*- coding: utf-8 -*-
"""topup 补量链 TechnicalFallbackAdapter 作用域回归钉 (2026-10-09).

事故: pipeline 拆分把 TechnicalFallbackAdapter 类迁去 bridge_adapters 后,
run_playbook_generate 加了局部 import 而 run_playbook_topup_supplement 漏带
(旧 pipeline 类在模块级, 旧码无此坑)。屏幕通过分支必 NameError, 且被外层
except Exception 吞掉返回 None ⇒ 补量静默失效。提交治理钩 (check_undefined_names)
实证抓到。本钉驱动真实函数到该分支, 防再断。
"""

from types import SimpleNamespace

import app.playbook.pipeline_parts.generation_flow as gf
from app.playbook.pipeline_parts.bridge_adapters import TechnicalFallbackAdapter


def _fake_cfg(monkeypatch):
    import app.core.config_container as cc
    monkeypatch.setattr(cc, "get_config", lambda: SimpleNamespace(
        get_dotted=lambda k, d=None: {"enabled": True, "max_attempts_per_window": 2,
                                      "retry_interval_min": 15, "target_min_instructions": 25,
                                      "max_add_per_day": 12} if k == "playbook.topup" else d))


def test_topup_screen_pass_branch_receives_technical_adapter(monkeypatch):
    _fake_cfg(monkeypatch)

    # PlaybookDB → 空当日指令 (走 tmp 库太重, 直接假类)
    class _FakeDB:
        def ensure_tables(self): pass
        def get_instructions_by_date(self, today): return []
        def save_instruction(self, d): pass

    monkeypatch.setattr("app.playbook.db.PlaybookDB", lambda: _FakeDB())
    monkeypatch.setattr("app.playbook.market_regime.MarketRegimeGate",
                        lambda: SimpleNamespace(assess=lambda: SimpleNamespace(regime="neutral")))
    monkeypatch.setattr("app.playbook.screener.PlaybookScreener",
                        lambda collector: SimpleNamespace(
                            screen=lambda watchlist, regime, positions: ["row-US.TEST"]))
    monkeypatch.setattr("app.utils.watchlist_manager.get_symbols_by_role",
                        lambda role: ["US.TEST"])

    seen = {}

    def _spy_build(*, screen_results, voting_coordinator, regime_context,
                   total_equity, data_router, today):
        seen["vc"] = voting_coordinator
        return []   # 零指令即可, 关键在 voting_coordinator 解析成功

    runtime = SimpleNamespace(collector=None, positions=[], data_router=None,
                              total_equity=100000.0)
    result = gf.run_playbook_topup_supplement(
        None,
        now_et_provider=lambda: __import__("zoneinfo").ZoneInfo("America/New_York"),
        today_provider=lambda: "2026-10-09",
        topup_window=lambda hhmm, cfg: "premarket",
        attempts_cache={},
        build_runtime_context=lambda s: runtime,
        build_instructions_from_votes=_spy_build,
        instruction_to_db_dict=lambda inst, ctx, today: {"raw_instruction": {}},
        gather_intraday_evidence=lambda router, sym, direction: None,
        logger=__import__("logging").getLogger("test"),
    )
    # 修复前: NameError 被外层 except 吞 ⇒ result is None; 修复后走到返回
    assert result == {"added": 0, "pool_screened": 1, "timing": "premarket"}
    assert isinstance(seen["vc"], TechnicalFallbackAdapter)
