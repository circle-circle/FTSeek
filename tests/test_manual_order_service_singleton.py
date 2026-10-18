"""并发单例回归钉: 5 线程筛选池不得建出多个实盘连接 (2026-10-18)."""
import time
from concurrent.futures import ThreadPoolExecutor

import app.web_dashboard.manual_order_service as mod


def test_concurrent_get_returns_single_instance(monkeypatch):
    class SlowService:
        created = 0

        def __init__(self):
            type(self).created += 1
            time.sleep(0.01)

    original = mod._instance
    try:
        mod._instance = None
        monkeypatch.setattr(mod, "ManualOrderService", SlowService)
        with ThreadPoolExecutor(max_workers=16) as pool:
            ids = list(pool.map(lambda _: id(mod.get_manual_order_service()), range(64)))
        assert len(set(ids)) == 1
        assert SlowService.created == 1
    finally:
        mod._instance = original
