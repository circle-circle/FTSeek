#!/usr/bin/env python3
"""
新闻源健康看板 (2026-05-14)

显示各源:
- 当前是否启用 + paused
- 当日条数
- 平均延迟 (新闻发布 → 入库)
- 最近一条时间

用法:
  python scripts/news_sources_health.py            # 一次性快照
  python scripts/news_sources_health.py --live     # 持续刷新 30s
  python scripts/news_sources_health.py --probe    # 主动 ping 各源 (检查可达性)
"""
from __future__ import annotations
import argparse
import asyncio
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def cmd_snapshot():
    """从 news db 读各源当日条数 + 最近一条时间"""
    print(f"\n[{datetime.now().isoformat(timespec='seconds')}] 新闻源快照")
    print("=" * 80)

    # 1. 配置态: 哪些源 enabled
    print("\n=== 源配置 ===")
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        sources = cfg.get_dotted("news_sources", {}) or {}
        for category, items in sources.items():
            if not isinstance(items, dict):
                continue
            for name, c in items.items():
                if not isinstance(c, dict):
                    continue
                enabled = c.get("enabled", False)
                interval = c.get("interval_sec", "-")
                mark = "✅" if enabled else "❌"
                print(f"  {mark} {category}.{name:20s} interval={interval}s")
    except Exception as e:
        print(f"  [WARN] 读 yaml 失败: {e}")

    # 2. DB 当日统计
    print("\n=== 当日新闻数 (按源) ===")
    try:
        from app.core.paths import get_data_dir
        # rsshub 老 db
        legacy_db = get_data_dir() / "rsshub_us" / "news_us.db"
        if legacy_db.exists():
            conn = sqlite3.connect(str(legacy_db))
            try:
                rows = conn.execute(
                    "SELECT source_id, COUNT(*) as n, MAX(timestamp) as latest "
                    "FROM news_items WHERE timestamp > strftime('%s','now','-24 hours') "
                    "GROUP BY source_id ORDER BY n DESC"
                ).fetchall()
                print(f"  {'source_id':25s} {'n_24h':>6s} {'latest':>20s}")
                for src, n, latest in rows:
                    latest_str = datetime.fromtimestamp(latest).strftime("%Y-%m-%d %H:%M:%S") if latest else "-"
                    print(f"  {(src or '?'):25s} {n:>6d} {latest_str:>20s}")
                if not rows:
                    print("  (无数据)")
            except Exception as e:
                print(f"  [INFO] 表 news_items 不可读: {e}")
            conn.close()
        else:
            print(f"  [INFO] {legacy_db} 不存在")
    except Exception as e:
        print(f"  [WARN] 读 DB 失败: {e}")

    # 3. L1 分类 DB (如果开了)
    print("\n=== L1 LLM 分类落库 (24h) ===")
    try:
        from app.core.paths import get_data_dir
        cls_db = get_data_dir() / "news_quality.db"
        if cls_db.exists():
            conn = sqlite3.connect(str(cls_db))
            try:
                rows = conn.execute(
                    "SELECT raw_lang, event_type, COUNT(*) as n "
                    "FROM news_classifications "
                    "WHERE classified_at > strftime('%s','now','-24 hours') "
                    "GROUP BY raw_lang, event_type ORDER BY n DESC LIMIT 20"
                ).fetchall()
                if rows:
                    print(f"  {'lang':>6s} {'event_type':25s} {'n':>5s}")
                    for lang, et, n in rows:
                        print(f"  {(lang or '?'):>6s} {(et or '?'):25s} {n:>5d}")
                else:
                    print("  (L1 未启用或无数据)")
            except Exception as e:
                print(f"  [INFO] 表 news_classifications 不可读: {e}")
            conn.close()
    except Exception:
        pass


async def cmd_probe():
    """主动 ping 国产 + 国际源, 测试可达性"""
    print(f"\n[{datetime.now().isoformat(timespec='seconds')}] 主动可达性探测")
    print("=" * 80)

    targets = [
        # (name, url, expect_status, timeout)
        ("财联社快讯",        "https://www.cls.cn/nodeapi/telegraphList?app=CailianpressWeb&rn=1", 200, 10),
        ("华尔街见闻",        "https://api-prod.wallstreetcn.com/apiv1/content/articles?channel=us-stock&limit=1", 200, 15),
        ("东方财富搜索",      "https://search-api.eastmoney.com/news/getNews?keyword=NVDA&pageindex=1&pagesize=1", 200, 15),
        ("雪球美股",          "https://xueqiu.com/", 200, 10),
        ("Finnhub (US)",      "https://finnhub.io/api/v1/news?category=general", 200, 15),
        ("SEC EDGAR",         "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent", 200, 20),
        ("Yahoo Finance",     "https://finance.yahoo.com/", 200, 15),
        ("RSSHub rssforever", "https://rsshub.rssforever.com/", 200, 15),
    ]

    try:
        import requests
    except ImportError:
        print("[ERR] requests 库不可用")
        return

    print(f"\n  {'源':25s} {'状态':6s} {'延迟(ms)':>10s}  {'结论':10s}")
    print("  " + "-" * 70)

    for name, url, expect_status, timeout in targets:
        start = time.time()
        try:
            r = requests.get(url, timeout=timeout, headers={
                "User-Agent": "Mozilla/5.0 (probe)",
            })
            elapsed_ms = (time.time() - start) * 1000
            status_ok = r.status_code == expect_status
            verdict = "✅ OK" if status_ok else f"⚠ {r.status_code}"
            print(f"  {name:25s} {r.status_code:>6d} {elapsed_ms:>10.0f}  {verdict}")
        except requests.Timeout:
            elapsed_ms = (time.time() - start) * 1000
            print(f"  {name:25s} {'TO':>6s} {elapsed_ms:>10.0f}  🚫 timeout")
        except requests.ConnectionError:
            print(f"  {name:25s} {'CONN':>6s} {'-':>10s}  🚫 不可达")
        except Exception as e:
            print(f"  {name:25s} {'ERR':>6s} {'-':>10s}  ⚠ {str(e)[:30]}")


def cmd_live(refresh_sec: int = 30):
    """持续刷新"""
    try:
        while True:
            os.system("clear" if os.name == "posix" else "cls")
            cmd_snapshot()
            print(f"\n[live] 下次刷新 {refresh_sec}s 后, Ctrl+C 退出")
            time.sleep(refresh_sec)
    except KeyboardInterrupt:
        print("\n[live] 退出")


def main():
    ap = argparse.ArgumentParser(description="新闻源健康看板")
    ap.add_argument("--live", action="store_true", help="持续刷新 30s")
    ap.add_argument("--probe", action="store_true", help="主动 ping 各源")
    ap.add_argument("--refresh", type=int, default=30, help="--live 刷新间隔 (默认 30s)")
    args = ap.parse_args()

    if args.probe:
        asyncio.run(cmd_probe())
        return
    if args.live:
        cmd_live(refresh_sec=args.refresh)
        return
    cmd_snapshot()


if __name__ == "__main__":
    main()
