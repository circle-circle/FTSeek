#!/usr/bin/env python3
"""
实盘切换预检脚本 (Go-Live Pre-flight Checklist)

用途: 在切换到实盘交易前，系统性验证所有必要条件
运行: python3 scripts/go_live_checklist.py [--live] [--conservative]

退出码:
  0 = 全部通过，可以切换
  1 = 有阻断性问题，不可切换
  2 = 有警告，需人工确认
"""

import os
import sys
import json
import subprocess
from pathlib import Path
from datetime import datetime

# ─────────────────────────────────────────────────────────────────
# 配置路径
# ─────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "data" / "ftseek_us.db"
ENV_FILE = BASE_DIR / ".env"
ENV_US_FILE = BASE_DIR / ".env.us"
CONFIG_PATH = BASE_DIR / "config" / "unified_config.yaml"

# 颜色输出
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

_results = {"pass": 0, "warn": 0, "fail": 0}


def header(title: str):
    print(f"\n{BOLD}{CYAN}{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}{RESET}")


def ok(msg: str):
    _results["pass"] += 1
    print(f"  {GREEN}✅ {msg}{RESET}")


def warn(msg: str):
    _results["warn"] += 1
    print(f"  {YELLOW}⚠️  {msg}{RESET}")


def fail(msg: str):
    _results["fail"] += 1
    print(f"  {RED}❌ {msg}{RESET}")


def info(msg: str):
    print(f"  {CYAN}ℹ  {msg}{RESET}")


# ─────────────────────────────────────────────────────────────────
# 1. 环境变量
# ─────────────────────────────────────────────────────────────────

def check_env_vars(target_mode: str):
    header("1. 环境变量检查")

    trade_env = os.getenv("TRADE_ENV", "1")
    allow_real = os.getenv("ALLOW_REAL_TRADING", "false").lower()
    live_conservative = os.getenv("LIVE_CONSERVATIVE", "1")

    info(f"当前 TRADE_ENV={trade_env}")
    info(f"当前 ALLOW_REAL_TRADING={allow_real}")
    info(f"当前 LIVE_CONSERVATIVE={live_conservative}")

    if target_mode == "live_conservative":
        if trade_env != "0":
            fail(f"TRADE_ENV={trade_env}，需设为 0 才能实盘")
        else:
            ok("TRADE_ENV=0 (实盘)")

        if allow_real != "true":
            fail("ALLOW_REAL_TRADING 未设为 true — 安全拦截会阻止实盘下单")
        else:
            ok("ALLOW_REAL_TRADING=true")

        if live_conservative != "1":
            warn(f"LIVE_CONSERVATIVE={live_conservative}，建议设为 1 (保守模式)")
        else:
            ok("LIVE_CONSERVATIVE=1 (保守模式)")

    elif target_mode == "simulate":
        if trade_env != "1":
            warn(f"TRADE_ENV={trade_env}，模拟盘应为 1")
        else:
            ok("TRADE_ENV=1 (模拟盘)")

    # 检查关键 API keys
    required_keys = [
        "DASHSCOPE_API_KEY",
        "TWELVEDATA_API_KEY",
        "FINNHUB_API_KEY",
    ]
    missing_keys = [k for k in required_keys if not os.getenv(k)]
    if missing_keys:
        fail(f"缺失 API keys: {', '.join(missing_keys)}")
    else:
        ok("所有必要 API keys 已设置")

    # 富途账号
    futu_login = os.getenv("FUTU_LOGIN_ACCOUNT", "")
    if not futu_login:
        fail("FUTU_LOGIN_ACCOUNT 未设置")
    else:
        ok(f"FUTU_LOGIN_ACCOUNT={futu_login[:4]}***")

    # 实盘解锁密码 (关键阻断点)
    if target_mode != "simulate":
        futu_unlock_pwd_md5 = os.getenv("FUTU_UNLOCK_PWD_MD5", "")
        futu_unlock_pwd = os.getenv("FUTU_UNLOCK_PWD", "")
        if futu_unlock_pwd_md5:
            ok(f"FUTU_UNLOCK_PWD_MD5 已设置 (MD5模式，推荐)")
        elif futu_unlock_pwd:
            ok("FUTU_UNLOCK_PWD 已设置 (明文模式)")
            warn("建议使用 MD5 模式: echo -n \"密码\" | md5sum | awk '{print $1}'")
        else:
            fail("解锁密码未设置 — 富途实盘必须先解锁交易账户")
            info("推荐: FUTU_UNLOCK_PWD_MD5=<md5哈希>")
            info("  生成: echo -n \"你的密码\" | md5sum | awk '{print $1}'")
            info("备选: FUTU_UNLOCK_PWD=<明文密码>")

    return _results["fail"] == 0


# ─────────────────────────────────────────────────────────────────
# 2. Futu OpenD 连接
# ─────────────────────────────────────────────────────────────────

def check_futu_connection():
    header("2. Futu OpenD 连接")

    try:
        from futu import OpenQuoteContext, OpenSecTradeContext, TrdEnv, RET_OK
        import futu

        opend_host = os.getenv("OPEND_HOST", "127.0.0.1")
        opend_port = int(os.getenv("OPEND_PORT", "11111"))

        # 行情连接测试
        info(f"连接行情服务器 {opend_host}:{opend_port}...")
        quote_ctx = OpenQuoteContext(host=opend_host, port=opend_port)

        ret, data = quote_ctx.get_global_state()
        if ret == RET_OK:
            ok(f"行情连接正常 (qot={data['qot_logined']}, trd={data['trd_logined']})")
        else:
            fail(f"行情连接异常: {data}")
            quote_ctx.close()
            return False

        # 交易连接测试 (模拟)
        info("测试模拟交易连接...")
        trd_ctx = OpenSecTradeContext(
            filter_trdmarket=futu.TrdMarket.NONE,
            host=opend_host,
            port=opend_port,
        )

        ret, acc_list = trd_ctx.get_acc_list()
        if ret == RET_OK:
            sim_accs = acc_list[acc_list["trd_env"] == TrdEnv.SIMULATE]
            real_accs = acc_list[acc_list["trd_env"] == TrdEnv.REAL]
            info(f"模拟账户: {len(sim_accs)} 个")
            info(f"真实账户: {len(real_accs)} 个")
            if len(real_accs) > 0:
                ok(f"真实账户已登录 (acc_id={real_accs.iloc[0]['acc_id']})")
            else:
                warn("未检测到真实账户 — 实盘需要登录真实账户")

            # 查询真实账户资金 (如果有)
            if len(real_accs) > 0:
                acc_id = real_accs.iloc[0]["acc_id"]
                ret, funds = trd_ctx.accinfo_query(
                    trd_env=TrdEnv.REAL,
                    acc_id=acc_id,
                )
                if ret == RET_OK:
                    cash = funds.iloc[0]["cash"] if "cash" in funds.columns else None
                    power = funds.iloc[0]["power"] if "power" in funds.columns else None
                    info(f"真实账户现金: ${cash:,.2f}" if cash else "现金数据不可用")
                    info(f"真实账户购买 power: ${power:,.2f}" if power else "power 数据不可用")
                    if power and power < 5000:
                        warn(f"购买力仅 ${power:,.0f}，建议 ≥ $10,000 再切实盘")
                else:
                    warn(f"无法查询真实账户资金 (可能未解锁): {funds}")
        else:
            fail(f"获取账户列表失败: {acc_list}")

        quote_ctx.close()
        trd_ctx.close()
        return True

    except ImportError:
        fail("futu 库未安装 — pip install futu")
        return False
    except Exception as e:
        fail(f"Futu 连接测试失败: {e}")
        return False


# ─────────────────────────────────────────────────────────────────
# 3. broker_adapter 实盘就绪
# ─────────────────────────────────────────────────────────────────

def check_broker_adapter():
    header("3. broker_adapter 实盘就绪")

    adapter_path = BASE_DIR / "app" / "execution" / "broker_adapter.py"
    if not adapter_path.exists():
        fail("broker_adapter.py 不存在")
        return False

    content = adapter_path.read_text()

    # 检查 unlock_trade 逻辑
    if "unlock_trade" in content:
        ok("broker_adapter 包含 unlock_trade 逻辑")
    else:
        fail("broker_adapter 缺少 unlock_trade 逻辑")
        info("富途实盘下单前必须调用 trade_ctx.unlock_trade(trd_env=TrdEnv.REAL, unlock_pwd=xxx)")
        info("修复: 在 broker_adapter.py connect() 方法中，trade_env=0 时添加解锁步骤")

    # 检查 _unlock_pwd 是否从环境变量读取
    if "FUTU_UNLOCK_PWD" in content or "unlock_pwd" in content:
        ok("unlock_pwd 有相关代码")
    else:
        fail("_unlock_pwd 硬编码为空字符串 — 实盘解锁密码未从环境变量读取")
        info("修复: 将 __init__ 中 self._unlock_pwd = '' 改为 os.getenv('FUTU_UNLOCK_PWD', '')")

    # 检查下单前是否有 unlock 步骤
    if "def place_order" in content:
        # 找 place_order 方法内是否有 unlock 调用
        import re
        place_order_match = re.search(
            r"def place_order.*?(?=\n    def |\Z)",
            content,
            re.DOTALL
        )
        if place_order_match and "unlock" in place_order_match.group():
            ok("place_order 中包含解锁步骤")
        else:
            warn("place_order 方法中未找到 unlock 调用 — 可能在 connect 时解锁")

    return True


# ─────────────────────────────────────────────────────────────────
# 4. 风控参数 (live_conservative 模式)
# ─────────────────────────────────────────────────────────────────

def check_risk_params():
    header("4. 风控参数验证 (live_conservative 目标)")

    try:
        import yaml
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
    except Exception as e:
        fail(f"无法读取 unified_config.yaml: {e}")
        return False

    # TradingMode 硬编码倍数 (app/core/trading_mode.py)
    info("TradingMode 硬编码参数 (代码中，非yaml):")
    info("  live_conservative risk_multiplier = 0.5 (仓位/风险减半)")
    info("  live_conservative max_daily_loss = base × 0.3 (日亏损仅放行30%)")

    # yaml 中的 mode_adaptation (当前未被代码读取 — 需修复)
    mode_adapt = cfg.get("risk", {}).get("mode_adaptation", {})
    if mode_adapt:
        info("yaml risk.mode_adaptation 配置 (当前⚠️未被代码读取):")
        lc = mode_adapt.get("live_conservative", {})
        for k, v in lc.items():
            info(f"  {k}: {v}")
        warn("yaml 中 risk.mode_adaptation 参数当前未被任何代码读取")
        info("实际生效的是 TradingMode.get_risk_multiplier() 硬编码值 (0.5x)")

    # 当前模拟盘参数 (会被 0.5x 倍数影响)
    risk = cfg.get("risk", {})
    per_trade = risk.get("per_trade_risk_pct", 0)
    max_pos = risk.get("position_sizing", {}).get("max_position_pct", 0)

    info("当前基础参数 (模拟盘):")
    info(f"  per_trade_risk_pct: {per_trade:.3f} → live_conservative 实际: {per_trade*0.5:.3f}")
    info(f"  max_position_pct:   {max_pos:.3f} → live_conservative 实际: {max_pos*0.5:.3f}")

    if per_trade > 0.005:
        warn(f"per_trade_risk_pct={per_trade} 偏高，live_conservative 0.5x后={per_trade*0.5:.3f}")
        info("建议: live 前将 per_trade_risk_pct 改为 0.005 (0.5%)，0.5x后实际=0.0025")
    else:
        ok(f"per_trade_risk_pct={per_trade} 在合理范围")

    # 日亏损限制
    max_dd = risk.get("account", {}).get("max_daily_drawdown", 0)
    info(f"  max_daily_drawdown: {max_dd:.3f} → live_conservative get_max_daily_loss 实际: {max_dd*0.3:.3f}")

    # 全局黑名单
    blacklist = risk.get("global_blacklist", [])
    info(f"  全局黑名单: {len(blacklist)} 项 ({', '.join(blacklist[:5])}...)")

    return True


# ─────────────────────────────────────────────────────────────────
# 5. 持仓状态
# ─────────────────────────────────────────────────────────────────

def check_positions():
    header("5. 当前持仓状态")

    try:
        import sqlite3
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row

        cursor = conn.execute("SELECT symbol, direction, size, avg_entry_price, entry_time, owner_strategy FROM open_positions")
        positions = cursor.fetchall()

        if not positions:
            ok("无持仓 (干净状态)")
            conn.close()
            return True

        info(f"当前持仓: {len(positions)} 个")

        etf_in_main = []
        old_positions = []
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=3)).isoformat()

        for p in positions:
            sym = p["symbol"]
            direction = p["direction"]
            size = p["size"]
            entry = p["entry_time"] or ""
            strategy = p["owner_strategy"] or ""

            is_etf = any(sym.endswith(e) for e in [".SPY", ".QQQ", ".IWM", ".DIA", ".XLE", ".XLF", ".XLK", ".XLV", ".XLY", ".XLI"])
            is_old = entry < cutoff if entry else False

            flag = ""
            if is_etf and strategy != "orb_engine":
                flag = " ⚠️ETF不在ORB"
                etf_in_main.append(sym)
            if is_old:
                flag += " ⚠️>3天"
                old_positions.append(sym)

            print(f"  {sym:12} {direction:5} {size:>8.0f}股 @ ${p['avg_entry_price']:.2f}  ({strategy}) {entry[:10]}{flag}")

        if etf_in_main:
            warn(f"主线持有 ETF 仓位 (应归 ORB): {', '.join(etf_in_main)}")
            info("ADR-006: ETF 全部归 ORB 引擎管理，主线不应持有 ETF")
        if old_positions:
            warn(f"超过3天的老仓位: {', '.join(old_positions)}")
            info("建议: 切实盘前清理老仓位，避免跨模式持仓混淆")

        conn.close()
        return len(etf_in_main) == 0

    except Exception as e:
        fail(f"持仓查询失败: {e}")
        return False


# ─────────────────────────────────────────────────────────────────
# 6. 服务状态
# ─────────────────────────────────────────────────────────────────

def check_service():
    header("6. 服务运行状态")

    try:
        result = subprocess.run(
            ["systemctl", "status", "ftseek-us", "--no-pager"],
            capture_output=True, text=True, timeout=5
        )
        if "active (running)" in result.stdout:
            ok("ftseek-us 服务运行中")
        else:
            warn("ftseek-us 服务未运行")
            info("启动: systemctl start ftseek-us")
    except Exception as e:
        warn(f"systemctl 检查失败 (可能非systemd环境): {e}")

    # 健康端点
    try:
        import urllib.request
        response = urllib.request.urlopen("http://localhost:8899/health", timeout=3)
        data = json.loads(response.read())
        ok(f"健康端点正常: {data.get('status', '?')}")
    except Exception:
        warn("健康端点 (8899) 无响应 — HK共享服务可能未运行")

    try:
        import urllib.request
        response = urllib.request.urlopen("http://localhost:8901/health", timeout=3)
        data = json.loads(response.read())
        ok(f"US API端点正常: {data.get('status', '?')}")
    except Exception:
        warn("US API端点 (8901) 无响应")


# ─────────────────────────────────────────────────────────────────
# 7. 切换步骤输出
# ─────────────────────────────────────────────────────────────────

def print_switch_instructions():
    header("📋 实盘切换步骤 (LIVE_CONSERVATIVE)")

    print(f"""
{BOLD}Step 1: 修改 .env.us (或 .env){RESET}
  将以下变量添加/修改:

  TRADE_ENV=0
  LIVE_CONSERVATIVE=1
  ALLOW_REAL_TRADING=true

  # 富途解锁密码 (二选一，推荐MD5):
  # 生成MD5: echo -n "你的密码" | md5sum | awk '{{print $1}}'
  FUTU_UNLOCK_PWD_MD5=<md5哈希值>
  # 或明文 (不推荐):
  # FUTU_UNLOCK_PWD=<明文密码>

{BOLD}Step 2: 修改 unified_config.yaml{RESET}
  # 收紧模拟盘放宽的参数 (这些在 live 模式下仍会被读取):
  per_trade_risk_pct: 0.005    # 从 0.010 改回 0.5%
  max_positions: 5             # 从 12 改回 5
  signal_cooldown: 120         # 从 60 改回 120秒
  consensus_threshold: 0.55    # 从 0.4 改回 0.55

{BOLD}Step 3: 重启服务{RESET}
  systemctl restart ftseek-us

{BOLD}Step 4: 验证{RESET}
  python3 scripts/go_live_checklist.py --live
  tail -f logs/ftseek_us.log | grep -i "TradingMode\|trade_env\|REAL"

{BOLD}回滚 (如需立即切回模拟盘):{RESET}
  # 在 .env.us 中改回:
  TRADE_ENV=1
  ALLOW_REAL_TRADING=false
  systemctl restart ftseek-us
""")


# ─────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="FTSeek-US 实盘切换预检")
    parser.add_argument("--live", action="store_true", help="检查实盘模式条件")
    parser.add_argument("--conservative", action="store_true", help="目标为 live_conservative (默认)")
    args = parser.parse_args()

    target_mode = "live_conservative" if (args.live or args.conservative) else "simulate"

    print(f"\n{BOLD}{'='*60}")
    print(f"  FTSeek-US 实盘切换预检 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  目标模式: {target_mode.upper()}")
    print(f"{'='*60}{RESET}")

    check_env_vars(target_mode)
    check_service()
    check_positions()
    check_risk_params()
    check_broker_adapter()

    if target_mode != "simulate":
        check_futu_connection()
        print_switch_instructions()

    # 总结
    header("检查结果汇总")
    ok_count = _results["pass"]
    warn_count = _results["warn"]
    fail_count = _results["fail"]

    print(f"  ✅ 通过: {ok_count}  ⚠️ 警告: {warn_count}  ❌ 阻断: {fail_count}")

    if fail_count > 0:
        print(f"\n  {RED}{BOLD}⛔ 存在 {fail_count} 个阻断性问题，不可切换到实盘{RESET}")
        print(f"  请先修复上述问题，再运行: python3 scripts/go_live_checklist.py --live")
        sys.exit(1)
    elif warn_count > 0:
        print(f"\n  {YELLOW}⚠  有 {warn_count} 个警告，建议处理后再切换{RESET}")
        sys.exit(2)
    else:
        print(f"\n  {GREEN}{BOLD}✅ 全部通过，可以切换到 {target_mode} 模式{RESET}")
        sys.exit(0)


if __name__ == "__main__":
    main()
