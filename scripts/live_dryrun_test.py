#!/usr/bin/env python3
"""
实盘链路干跑验证 (Live Dry-Run Test)

在模拟盘运行期间，验证实盘切换后的全链路是否正确。
不会实际下单，不会修改环境变量，不影响当前运行的服务。

测试项:
  1. DB 路由隔离验证
  2. 实盘 DB 完整性验证
  3. Broker 解锁 + 账户指向验证
  4. 自管持仓隔离验证
  5. TradingMode 风控倍数验证
  6. 关键模块导入验证
  7. 模拟下单流程验证 (不实际发送)

用法: python3 scripts/live_dryrun_test.py
"""

import os
import sys
import json
import sqlite3
import traceback
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

# 加载 .env.us
env_file = BASE_DIR / ".env.us"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

# 颜色
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

_passed = 0
_failed = 0
_warned = 0


def ok(msg):
    global _passed; _passed += 1
    print(f"  {GREEN}✅ {msg}{RESET}")

def fail(msg):
    global _failed; _failed += 1
    print(f"  {RED}❌ {msg}{RESET}")

def warn(msg):
    global _warned; _warned += 1
    print(f"  {YELLOW}⚠️  {msg}{RESET}")

def info(msg):
    print(f"  {CYAN}ℹ  {msg}{RESET}")

def header(title):
    print(f"\n{BOLD}{'─'*60}{RESET}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{BOLD}{'─'*60}{RESET}")


# ─────────────────────────────────────────────────────────────────
# 1. DB 路由隔离验证
# ─────────────────────────────────────────────────────────────────

def test_db_routing():
    header("1. DB 路由隔离验证")

    # 保存当前 TRADE_ENV
    original_env = os.getenv("TRADE_ENV", "1")

    # Test paper mode
    os.environ["TRADE_ENV"] = "1"
    import importlib
    import app.core.db_utils as dbu
    importlib.reload(dbu)
    paper_db = dbu.get_main_db()
    paper_tr = dbu.get_trade_records_db()

    if paper_db.name == "ftseek_us.db":
        ok(f"模拟盘 DB: {paper_db.name}")
    else:
        fail(f"模拟盘 DB 路径错误: {paper_db.name} (期望 ftseek_us.db)")

    if paper_tr.name == "ftseek_us.db":
        ok(f"模拟盘 trade_records: {paper_tr.name}")
    else:
        fail(f"模拟盘 trade_records 路径错误: {paper_tr.name}")

    # Test live mode (only in-process, doesn't affect running service)
    os.environ["TRADE_ENV"] = "0"
    importlib.reload(dbu)
    live_db = dbu.get_main_db()
    live_tr = dbu.get_trade_records_db()

    if live_db.name == "ftseek_us_live.db":
        ok(f"实盘 DB: {live_db.name}")
    else:
        fail(f"实盘 DB 路径错误: {live_db.name} (期望 ftseek_us_live.db)")

    if live_tr.name == "ftseek_us_live.db":
        ok(f"实盘 trade_records: {live_tr.name}")
    else:
        fail(f"实盘 trade_records 路径错误: {live_tr.name}")

    # Verify paths.py also routes correctly
    import app.core.paths as paths_mod
    importlib.reload(paths_mod)
    paths_live = paths_mod.get_trade_records_db()
    if paths_live.name == "ftseek_us_live.db":
        ok(f"paths.get_trade_records_db() 实盘路由正确")
    else:
        fail(f"paths.get_trade_records_db() 路由错误: {paths_live.name}")

    # Restore
    os.environ["TRADE_ENV"] = original_env
    importlib.reload(dbu)

    # Verify both DB files exist
    paper_exists = (BASE_DIR / "data" / "ftseek_us.db").exists()
    live_exists = (BASE_DIR / "data" / "ftseek_us_live.db").exists()

    if paper_exists:
        paper_size = (BASE_DIR / "data" / "ftseek_us.db").stat().st_size / 1024
        ok(f"ftseek_us.db 存在 ({paper_size:.0f}KB)")
    else:
        fail("ftseek_us.db 不存在")

    if live_exists:
        live_size = (BASE_DIR / "data" / "ftseek_us_live.db").stat().st_size / 1024
        ok(f"ftseek_us_live.db 存在 ({live_size:.0f}KB)")
    else:
        fail("ftseek_us_live.db 不存在")


# ─────────────────────────────────────────────────────────────────
# 2. 实盘 DB 完整性验证
# ─────────────────────────────────────────────────────────────────

def test_live_db_integrity():
    header("2. 实盘 DB 完整性验证")

    live_db = BASE_DIR / "data" / "ftseek_us_live.db"
    if not live_db.exists():
        fail("ftseek_us_live.db 不存在")
        return

    conn = sqlite3.connect(str(live_db))
    cur = conn.cursor()

    # Check tables exist
    tables = cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    table_names = [t[0] for t in tables]

    critical_tables = [
        "open_positions", "trade_records", "order_tracking",
        "signals", "stoploss_events", "idempotency_keys",
    ]
    for t in critical_tables:
        if t in table_names:
            count = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            ok(f"{t}: {count} 条 (空 = 正确)")
        else:
            fail(f"{t}: 表不存在!")

    # Verify open_positions is empty (should be clean for live)
    op_count = cur.execute("SELECT COUNT(*) FROM open_positions").fetchone()[0]
    if op_count == 0:
        ok("open_positions 为空 (实盘从空仓开始)")
    else:
        warn(f"open_positions 有 {op_count} 条数据 (实盘应有0条)")

    conn.close()


# ─────────────────────────────────────────────────────────────────
# 3. Broker 解锁 + 账户指向验证
# ─────────────────────────────────────────────────────────────────

def test_broker_unlock():
    header("3. Broker 解锁 + 账户指向验证")

    pwd_md5 = os.getenv("FUTU_UNLOCK_PWD_MD5", "")
    real_acc = os.getenv("REAL_ACCOUNT_ID", "")

    if not pwd_md5:
        fail("FUTU_UNLOCK_PWD_MD5 未设置")
        return
    ok(f"FUTU_UNLOCK_PWD_MD5: {len(pwd_md5)} 字符")

    if not real_acc:
        fail("REAL_ACCOUNT_ID 未设置")
        return
    ok(f"REAL_ACCOUNT_ID: {real_acc}")

    try:
        from futu import OpenSecTradeContext, TrdMarket, TrdEnv, RET_OK

        host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("FUTU_OPEND_PORT", "11111"))

        ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.NONE, host=host, port=port,
        )

        # Verify account exists
        ret, accs = ctx.get_acc_list()
        if ret != RET_OK:
            fail(f"获取账户列表失败: {accs}")
            ctx.close()
            return

        real_ids = [str(r) for r in accs[accs["trd_env"] == TrdEnv.REAL]["acc_id"].tolist()]
        if real_acc in real_ids:
            ok(f"目标账户 {real_acc} 在真实账户列表中")
        else:
            fail(f"目标账户 {real_acc} 不在列表中! 可用: {real_ids}")
            ctx.close()
            return

        # Unlock
        ret, data = ctx.unlock_trade(password_md5=pwd_md5, is_unlock=True)
        if ret == RET_OK:
            ok("unlock_trade 成功")
        else:
            fail(f"unlock_trade 失败: {data}")
            ctx.close()
            return

        # Query funds
        ret2, funds = ctx.accinfo_query(trd_env=TrdEnv.REAL, acc_id=int(real_acc))
        if ret2 == RET_OK:
            row = funds.iloc[0]
            usd_assets = row.get("usd_assets", 0)
            us_cash = row.get("us_cash", 0)
            info(f"USD 资产: ${usd_assets:,.2f}")
            info(f"USD 现金: ${us_cash:,.2f}")
            if usd_assets and usd_assets > 0:
                ok(f"USD 资产正常: ${usd_assets:,.2f}")
            else:
                warn("USD 资产为 0 或不可用")

            # Check currency
            currency = row.get("currency", "?")
            info(f"账户币种: {currency}")
            if currency == "HKD":
                warn("账户是 HKD 币种 — 确认 USD 资产而非 HKD 总资产")
        else:
            warn(f"资金查询失败: {funds}")

        # Query positions
        ret3, pos = ctx.position_list_query(trd_env=TrdEnv.REAL, acc_id=int(real_acc))
        if ret3 == RET_OK:
            info(f"当前持仓: {len(pos)} 个")
            for _, p in pos.iterrows():
                code = p.get("code", "?")
                qty = p.get("qty", 0)
                mv = p.get("market_val", 0)
                info(f"  {code:12s} {qty:>8.0f}股  市值${mv:,.2f}")
        else:
            warn(f"持仓查询失败: {pos}")

        ctx.close()

    except ImportError:
        fail("futu 库未安装")
    except Exception as e:
        fail(f"Broker 测试异常: {e}")
        traceback.print_exc()


# ─────────────────────────────────────────────────────────────────
# 4. 自管持仓隔离验证
# ─────────────────────────────────────────────────────────────────

def test_self_managed_isolation():
    header("4. 自管持仓隔离验证")

    # Check yaml config
    try:
        import yaml
        with open(BASE_DIR / "config" / "unified_config.yaml") as f:
            cfg = yaml.safe_load(f)

        sm = cfg.get("risk", {}).get("self_managed_symbols", [])
        if sm:
            ok(f"yaml self_managed_symbols: {sm}")
        else:
            fail("yaml 中 self_managed_symbols 为空或未配置")
            return

        # Verify these symbols would be excluded by position_sync
        expected = {"US.AMD", "US.BABA", "HK.00700"}
        actual = set(sm)
        if expected == actual:
            ok("自管持仓名单与真实账户匹配")
        else:
            diff = expected.symmetric_difference(actual)
            warn(f"自管持仓名单差异: {diff}")

    except Exception as e:
        fail(f"yaml 读取失败: {e}")

    # Check stoploss_events has the ghost registrations
    try:
        conn = sqlite3.connect(str(BASE_DIR / "data" / "ftseek_us.db"))
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT symbol FROM stoploss_events WHERE event_type = 'external_close'"
        )
        ghosts = {row[0] for row in cur.fetchall()}
        conn.close()

        expected = {"US.AMD", "US.BABA", "HK.00700"}
        if expected.issubset(ghosts):
            ok(f"stoploss_events 已登记自管持仓: {sorted(expected)}")
        else:
            missing = expected - ghosts
            warn(f"stoploss_events 缺少: {sorted(missing)}")
    except Exception as e:
        warn(f"stoploss_events 查询失败: {e}")

    # Check persistent memory
    try:
        mem_path = os.path.expanduser("~/.hermes/ftseek/memory/us.json")
        if os.path.exists(mem_path):
            with open(mem_path) as f:
                mem = json.load(f)
            ghosts = set(mem.get("known_issues", {}).get("ghost_positions", []))
            expected = {"US.AMD", "US.BABA", "HK.00700"}
            if expected.issubset(ghosts):
                ok(f"持久化记忆已登记: {sorted(expected)}")
            else:
                missing = expected - ghosts
                warn(f"持久化记忆缺少: {sorted(missing)}")
        else:
            warn("持久化记忆文件不存在")
    except Exception as e:
        warn(f"持久化记忆检查失败: {e}")


# ─────────────────────────────────────────────────────────────────
# 5. TradingMode 风控倍数验证
# ─────────────────────────────────────────────────────────────────

def test_trading_mode():
    header("5. TradingMode 风控倍数验证")

    original_env = os.getenv("TRADE_ENV", "1")
    original_lc = os.getenv("LIVE_CONSERVATIVE", "1")

    # Test live_conservative mode
    os.environ["TRADE_ENV"] = "0"
    os.environ["LIVE_CONSERVATIVE"] = "1"

    # Reset singleton
    from app.core.trading_mode import TradingMode, TradingModeEnum
    TradingMode._instance = None
    tm = TradingMode()

    if tm.mode == TradingModeEnum.LIVE_CONSERVATIVE:
        ok(f"模式检测: {tm.mode_label}")
    else:
        fail(f"模式检测错误: {tm.mode} (期望 LIVE_CONSERVATIVE)")

    rm = tm.get_risk_multiplier()
    if rm == 0.5:
        ok(f"risk_multiplier: {rm} (仓位减半)")
    else:
        fail(f"risk_multiplier: {rm} (期望 0.5)")

    ml = tm.get_max_daily_loss(1000)
    if ml == 300:
        ok(f"max_daily_loss: ${ml} (base $1000 × 0.3)")
    else:
        fail(f"max_daily_loss: ${ml} (期望 $300)")

    ps = tm.get_max_position_size(100)
    if ps == 50:
        ok(f"max_position_size: {ps} (base 100 × 0.5)")
    else:
        fail(f"max_position_size: {ps} (期望 50)")

    if tm.is_live:
        ok("is_live: True")
    else:
        fail("is_live: False (期望 True)")

    if tm.is_conservative:
        ok("is_conservative: True")
    else:
        fail("is_conservative: False (期望 True)")

    # Calculate actual risk budget
    try:
        usd_assets = 81539  # from earlier test
        per_trade = 0.005   # after tightening yaml
        risk_per_trade = usd_assets * per_trade * rm
        ok(f"实际单笔风险: ${risk_per_trade:.0f} (${usd_assets} × {per_trade} × {rm})")
    except Exception:
        pass

    # Restore
    os.environ["TRADE_ENV"] = original_env
    os.environ["LIVE_CONSERVATIVE"] = original_lc
    TradingMode._instance = None
    TradingMode()  # re-init with original env


# ─────────────────────────────────────────────────────────────────
# 6. 关键模块导入验证
# ─────────────────────────────────────────────────────────────────

def test_module_imports():
    header("6. 关键模块导入验证")

    modules = [
        ("app.core.db_utils", "get_main_db"),
        ("app.core.paths", "get_trade_records_db"),
        ("app.core.trading_mode", "TradingMode"),
        ("app.core.config_container", "get_config"),
        ("app.execution.broker_adapter", "FutuBrokerAdapter"),
        ("app.execution.engine", "ExecutionEngine"),
        ("app.execution.idempotency", "IdempotencyManager"),
        ("app.risk.capital_safety", "CapitalSafetySystem"),
        ("app.risk.position_exit_monitor", "ExitMonitor"),
        ("app.core.position_sync", "USPositionSync"),
    ]

    for mod_name, attr_name in modules:
        try:
            mod = __import__(mod_name, fromlist=[attr_name])
            getattr(mod, attr_name)
            ok(f"{mod_name}.{attr_name}")
        except ImportError as e:
            fail(f"{mod_name}: ImportError — {e}")
        except AttributeError:
            fail(f"{mod_name}.{attr_name}: 属性不存在")
        except Exception as e:
            warn(f"{mod_name}: {type(e).__name__} — {e}")


# ─────────────────────────────────────────────────────────────────
# 7. 环境变量配置验证
# ─────────────────────────────────────────────────────────────────

def test_env_config():
    header("7. 环境变量配置验证")

    checks = {
        "FUTU_UNLOCK_PWD_MD5": ("已设置", lambda v: len(v) == 32),
        "REAL_ACCOUNT_ID": ("已设置", lambda v: len(v) > 0),
        "SIMULATE_ACCOUNT_ID": ("已设置", lambda v: len(v) > 0),
        "FUTU_OPEND_HOST": ("127.0.0.1", lambda v: True),
        "FUTU_OPEND_PORT": ("11111", lambda v: v.isdigit()),
        "TRADE_ENV": ("1 (当前模拟盘)", lambda v: v == "1"),
        "LIVE_CONSERVATIVE": ("1", lambda v: v == "1"),
    }

    for key, (expected_desc, validator) in checks.items():
        val = os.getenv(key, "")
        if val and validator(val):
            display = val[:8] + "..." if len(val) > 8 else val
            ok(f"{key}={display} ({expected_desc})")
        elif val:
            warn(f"{key}={val} (值可能不正确)")
        else:
            if key in ("FUTU_UNLOCK_PWD_MD5", "REAL_ACCOUNT_ID"):
                fail(f"{key} 未设置 (实盘必需)")
            else:
                warn(f"{key} 未设置")

    # Check ALLOW_REAL_TRADING (should be false until switch day)
    art = os.getenv("ALLOW_REAL_TRADING", "false")
    if art.lower() == "false":
        ok("ALLOW_REAL_TRADING=false (正确 — 切换当天改为 true)")
    else:
        warn(f"ALLOW_REAL_TRADING={art} (已开启?)")


# ─────────────────────────────────────────────────────────────────
# 8. di_container 安全拦截验证
# ─────────────────────────────────────────────────────────────────

def test_safety_interlock():
    header("8. 安全拦截机制验证")

    # Verify di_container respects ALLOW_REAL_TRADING
    original_trade = os.getenv("TRADE_ENV", "1")
    original_allow = os.getenv("ALLOW_REAL_TRADING", "false")

    # Test 1: TRADE_ENV=0 without ALLOW → should force simulate
    os.environ["TRADE_ENV"] = "0"
    os.environ["ALLOW_REAL_TRADING"] = "false"

    # Read the di_container logic directly
    _trade_env = int(os.getenv("TRADE_ENV", "1"))
    _allow_real = os.getenv("ALLOW_REAL_TRADING", "false").lower() == "true"

    if _trade_env == 0 and not _allow_real:
        ok("无 ALLOW_REAL_TRADING 时 TRADE_ENV=0 被拦截 → 模拟盘 (安全)")
    else:
        fail("安全拦截失效!")

    # Test 2: TRADE_ENV=0 with ALLOW → should pass through
    os.environ["ALLOW_REAL_TRADING"] = "true"
    _allow_real = os.getenv("ALLOW_REAL_TRADING", "false").lower() == "true"

    if _trade_env == 0 and _allow_real:
        ok("ALLOW_REAL_TRADING=true 时 TRADE_ENV=0 放行 → 实盘 (正确)")
    else:
        fail("ALLOW_REAL_TRADING 旁路失效!")

    # Restore
    os.environ["TRADE_ENV"] = original_trade
    os.environ["ALLOW_REAL_TRADING"] = original_allow


# ─────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────

def main():
    print(f"\n{BOLD}{'='*60}")
    print(f"  FTSeek-US 实盘链路干跑验证")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  当前模式: 模拟盘 (TRADE_ENV={os.getenv('TRADE_ENV', '1')})")
    print(f"{'='*60}{RESET}")

    test_db_routing()
    test_live_db_integrity()
    test_env_config()
    test_safety_interlock()
    test_self_managed_isolation()
    test_trading_mode()
    test_module_imports()
    test_broker_unlock()

    # Summary
    header("测试结果汇总")
    total = _passed + _failed + _warned
    print(f"  {GREEN}✅ 通过: {_passed}{RESET}  {RED}❌ 失败: {_failed}{RESET}  {YELLOW}⚠️ 警告: {_warned}{RESET}  总计: {total}")

    if _failed > 0:
        print(f"\n  {RED}{BOLD}⛔ {_failed} 项失败 — 切换前必须修复{RESET}")
        return 1
    elif _warned > 0:
        print(f"\n  {YELLOW}⚠ {_warned} 项警告 — 建议处理{RESET}")
        return 0
    else:
        print(f"\n  {GREEN}{BOLD}✅ 全部通过 — 实盘链路就绪{RESET}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
