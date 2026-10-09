"""RuntimeContext 阶段 2b 契约钉: 懒装配、调用顺序与 pipeline patch 缝。"""
from types import SimpleNamespace

from app.playbook import pipeline
from app.playbook.pipeline_parts.bridge_adapters import NullVotingAdapter
from app.playbook.pipeline_parts.runtime_context import (
    _get_voting_coordinator,
    build_runtime_context,
)


def test_runtime_context_is_lazy_and_caches_dependencies():
    calls = []
    scheduler = SimpleNamespace(data_router="router")

    def watchlist_provider(sched):
        assert sched is scheduler
        calls.append("watchlist")
        return ["US.TEST"]

    def collector_factory():
        calls.append("collector")
        return object()

    def positions_provider(sched):
        assert sched is scheduler
        calls.append("positions")
        return [{"symbol": "US.TEST"}]

    def voting_coordinator_provider(sched):
        assert sched is scheduler
        calls.append("voting")
        return object()

    def total_equity_provider(sched):
        assert sched is scheduler
        calls.append("equity")
        return 100_000.0

    context = build_runtime_context(
        scheduler,
        watchlist_provider=watchlist_provider,
        collector_factory=collector_factory,
        positions_provider=positions_provider,
        voting_coordinator_provider=voting_coordinator_provider,
        total_equity_provider=total_equity_provider,
    )

    assert context.watchlist == ["US.TEST"]
    assert context.collector is not None
    assert context.positions == [{"symbol": "US.TEST"}]
    assert context.voting_coordinator is not None
    assert context.total_equity == 100_000.0
    assert context.data_router == "router"
    assert calls == ["watchlist", "collector", "positions", "voting", "equity"]

    context.watchlist
    context.collector
    context.positions
    context.voting_coordinator
    context.total_equity
    context.data_router
    assert calls == ["watchlist", "collector", "positions", "voting", "equity"]


def test_pipeline_facade_providers_remain_patchable(monkeypatch):
    scheduler = SimpleNamespace()
    monkeypatch.setattr(pipeline, "_get_total_equity", lambda sched: 123.0)
    monkeypatch.setattr(
        pipeline, "_get_voting_coordinator", lambda sched: "patched-coordinator"
    )

    context = pipeline._build_runtime_context(scheduler)

    assert context.total_equity == 123.0
    assert context.voting_coordinator == "patched-coordinator"


def test_missing_voting_coordinator_falls_back_to_null_adapter():
    assert isinstance(_get_voting_coordinator(SimpleNamespace()), NullVotingAdapter)
