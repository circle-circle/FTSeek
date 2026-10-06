import importlib.util
import math
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "data_freshness_watchdog.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("data_freshness_watchdog", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_price_fail_closed_is_missing_only(monkeypatch, tmp_path):
    module = _load_module()
    entry = next(item for item in module.WATCHLIST
                 if item[0] == "data/price_fail_closed.json")
    assert entry[1] == math.inf

    monkeypatch.setattr(module, "WATCHLIST", [entry])
    monkeypatch.setattr(module, "_resolve", lambda rel: tmp_path / "price_fail_closed.json")
    monkeypatch.setattr(module, "_age_hours", lambda path: 1445.0)
    (tmp_path / "price_fail_closed.json").write_text("[]", encoding="utf-8")
    stale, missing, ok = module.check(dry=True)

    assert stale == []
    assert missing == []
    assert ok == [(entry[0], 1445.0)]


def test_price_fail_closed_missing_still_alerts(monkeypatch, tmp_path):
    module = _load_module()
    entry = next(item for item in module.WATCHLIST
                 if item[0] == "data/price_fail_closed.json")
    path = tmp_path / "missing.json"

    monkeypatch.setattr(module, "WATCHLIST", [entry])
    monkeypatch.setattr(module, "_resolve", lambda rel: path)
    stale, missing, ok = module.check(dry=True)

    assert stale == []
    assert missing == [(entry[0], entry[2])]
    assert ok == []
