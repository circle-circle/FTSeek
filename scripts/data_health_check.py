#!/usr/bin/env python3
"""数据源健康检查 — 盘前推送

检查项:
1. VIX缓存年龄 (< 1h = OK)
2. Finnhub API可达性
3. StockTwits API可达性
4. DegradationMonitor降级计数
5. signal_snapshots forward_return NULL率
6. capital_state一致性

输出: 飞书推送
"""
import sys, os, time, json, sqlite3, requests
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.degradation_monitor import get_degradation_monitor
from pathlib import Path

BASE = Path(__file__).parent.parent
TODAY = datetime.now().strftime("%Y-%m-%d")

def check_vix():
    """VIX缓存年龄"""
    try:
        cache_file = BASE / "data" / "vix_cache" / "vix_data.json"
        if not cache_file.exists():
            return "🔴 VIX缓存文件不存在"
        cache = json.loads(cache_file.read_text())
        if not cache:
            return "🟡 VIX缓存为空(下次会重新获取)"
        age_s = time.time() - cache.get("timestamp", 0)
        vix = cache.get("vix", "?")
        if age_s > 3600:
            return f"🔴 VIX缓存过期 {age_s/3600:.1f}h (值={vix}, 需刷新)"
        return f"✅ VIX={vix} (age={age_s/60:.0f}min)"
    except Exception as e:
        return f"🔴 VIX检查异常: {e}"

def check_finnhub():
    """Finnhub API可达性"""
    try:
        key = ""
        env_path = BASE / ".env.us"
        for line in env_path.read_text().splitlines():
            if line.startswith("FINNHUB_API_KEY=") and not line.startswith("#"):
                key = line.split("=", 1)[1].strip()
                break
        if not key:
            return "🔴 Finnhub API Key未配置"
        resp = requests.get(
            "https://finnhub.io/api/v1/quote",
            params={"symbol": "AAPL", "token": key},
            timeout=8,
        )
        if resp.status_code == 200:
            price = resp.json().get("c", 0)
            return f"✅ Finnhub OK (AAPL=${price})"
        return f"🔴 Finnhub status={resp.status_code}"
    except Exception as e:
        return f"🔴 Finnhub异常: {e}"

def check_stocktwits():
    """StockTwits API可达性"""
    try:
        resp = requests.get(
            "https://api.stocktwits.com/api/2/streams/symbol/AAPL.json",
            timeout=8,
            headers={"User-Agent": "FTSeek/1.0"},
        )
        if resp.status_code == 200:
            msgs = resp.json().get("messages", [])
            return f"✅ StockTwits OK ({len(msgs)} msgs)"
        if resp.status_code == 429:
            return "🔴 StockTwits Rate Limited (429)"
        return f"🟡 StockTwits status={resp.status_code}"
    except Exception as e:
        return f"🔴 StockTwits异常: {e}"

def check_degradation():
    """DegradationMonitor降级计数"""
    try:
        monitor = get_degradation_monitor()
        # 尝试获取降级状态
        if hasattr(monitor, 'get_status'):
            status = monitor.get_status()
            if status.get("is_any_degraded"):
                return f"🔴 有模块降级: {status}"
            return f"✅ 无降级 (total={status.get('total_events', 0)})"
        elif hasattr(monitor, '_events'):
            events = monitor._events
            recent = {k: v for k, v in events.items() if v}
            if recent:
                return f"🟡 降级模块: {list(recent.keys())}"
            return "✅ 无降级"
        return "✅ DegradationMonitor正常"
    except Exception as e:
        return f"🟡 DegradationMonitor检查异常: {e}"

def check_signal_snapshots():
    """signal_snapshots forward_return NULL率"""
    try:
        db_path = BASE / "data" / "signal_snapshots.db"
        if not db_path.exists():
            return "🔴 signal_snapshots.db不存在"
        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        
        # 最近7天的信号
        rows = db.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN fwd_ret_t1 IS NULL THEN 1 ELSE 0 END) as null_1d, "
            "SUM(CASE WHEN fwd_ret_t5 IS NULL THEN 1 ELSE 0 END) as null_5d "
            "FROM signal_snapshots WHERE created_at >= date('now', '-7 days')"
        ).fetchone()
        
        total = rows["total"]
        if total == 0:
            return "🟡 近7天无信号快照"
        
        null_1d_pct = (rows["null_1d"] / total * 100) if total else 0
        null_5d_pct = (rows["null_5d"] / total * 100) if total else 0
        
        # 5d NULL率高是正常的(还没到5天), 1d NULL率高说明回填机制有问题
        if null_1d_pct > 50 and total > 10:
            return f"🔴 forward_return_1d NULL率={null_1d_pct:.0f}% ({rows['null_1d']}/{total}) → IC回填可能异常"
        return f"✅ signal_snapshots: {total}条, 1d_null={null_1d_pct:.0f}%, 5d_null={null_5d_pct:.0f}%"
    except Exception as e:
        return f"🟡 signal_snapshots检查异常: {e}"

def check_capital_state():
    """capital_state一致性"""
    try:
        results = []
        for db_name in ["ftseek_us.db", "ftseek_us_live.db"]:
            db_path = BASE / "data" / db_name
            if not db_path.exists():
                continue
            db = sqlite3.connect(str(db_path))
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM capital_state ORDER BY rowid DESC LIMIT 1").fetchall()
            if rows:
                r = dict(rows[0])
                results.append(f"{db_name}: {len(rows)}行, updated={r.get('updated_at','?')}")
            else:
                results.append(f"{db_name}: 0行")
            db.close()
        return " | ".join(results) if results else "🔴 无capital_state"
    except Exception as e:
        return f"🟡 capital_state检查异常: {e}"

def main():
    checks = [
        ("VIX缓存", check_vix),
        ("Finnhub API", check_finnhub),
        ("StockTwits API", check_stocktwits),
        ("Degradation", check_degradation),
        ("Signal Snapshots", check_signal_snapshots),
        ("Capital State", check_capital_state),
    ]
    
    lines = [f"📊 数据源健康检查 | {TODAY}\n"]
    has_error = False
    for name, fn in checks:
        try:
            result = fn()
            if result.startswith("🔴"):
                has_error = True
            lines.append(f"  {name}: {result}")
        except Exception as e:
            lines.append(f"  {name}: 🔴 检查异常 {e}")
            has_error = True
    
    output = "\n".join(lines)
    print(output)
    
    # 飞书推送
    if has_error:
        try:
            from app.core.push_hub import PushHub
            hub = PushHub()
            hub.send_text("⚠️ 数据源健康检查发现问题\n\n" + output)
        except Exception:
            pass
    
    return output

if __name__ == "__main__":
    main()
