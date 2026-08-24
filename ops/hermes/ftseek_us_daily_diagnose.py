#!/usr/bin/env python3
"""
FTSeek-US 每日盘后诊断脚本 (2026-05-21)

监控目标:
1. 期权 PRICE_FAIL 循环: price_fail_closed.json 条目数 + 新增
2. 交易归因完整性: trade_attribution 表 vs trade_records 覆盖率
3. 新增交易 hook 日志: on_position_opened 调用记录
4. 交易质量: 当日胜率/PnL 趋势
5. 服务健康: systemd 状态 + 最近重启次数
6. Futu 连接: ret=-1 重置频率
7. Scalping 策略表现
"""

import json
import sqlite3
import subprocess
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"
LOG_PATH = "/opt/ftseek/FTSeek-US/logs/ftseek_us.log"
PRICE_FAIL_PATH = "/opt/ftseek/FTSeek-US/data/price_fail_closed.json"
SERVICE_NAME = "ftseek-us.service"

def run_cmd(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except Exception as e:
        return f"ERROR: {e}", -1

def check_price_fail_closed():
    """检查 PRICE_FAIL 阻断集合"""
    if not os.path.exists(PRICE_FAIL_PATH):
        return {"count": 0, "symbols": [], "status": "✅ 无阻断记录"}
    
    try:
        with open(PRICE_FAIL_PATH) as f:
            data = json.load(f)
        symbols = list(data.keys()) if isinstance(data, dict) else data
        count = len(symbols)
        
        # 检查新增: 对比文件修改时间
        mtime = os.path.getmtime(PRICE_FAIL_PATH)
        mod_time = datetime.fromtimestamp(mtime)
        now = datetime.now()
        hours_ago = (now - mod_time).total_seconds() / 3600
        
        if hours_ago < 24:
            status = f"⚠️ {count} 条 (最后更新 {hours_ago:.1f}h 前)"
        else:
            status = f"✅ {count} 条 (无新增, 最后更新 {hours_ago:.0f}h 前)"
        
        return {"count": count, "symbols": symbols[:5], "status": status}
    except Exception as e:
        return {"count": -1, "symbols": [], "status": f"❌ 读取失败: {e}"}

def check_trade_attribution():
    """检查交易归因覆盖率"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        # 总已平仓交易数
        total = conn.execute("SELECT COUNT(*) FROM trade_records WHERE exit_time IS NOT NULL").fetchone()[0]
        
        # 有归因记录的交易数
        try:
            attr_count = conn.execute("SELECT COUNT(DISTINCT trade_id) FROM trade_attribution").fetchone()[0]
        except:
            attr_count = 0  # 表可能不存在
        
        # 近 7 天交易
        since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        recent_total = conn.execute(
            "SELECT COUNT(*) FROM trade_records WHERE exit_time IS NOT NULL AND exit_time >= ?", (since,)
        ).fetchone()[0]
        
        # 近 7 天有归因
        try:
            recent_attr = conn.execute(
                "SELECT COUNT(DISTINCT ta.trade_id) FROM trade_attribution ta "
                "JOIN trade_records tr ON ta.trade_id = tr.id "
                "WHERE tr.exit_time IS NOT NULL AND tr.exit_time >= ?", (since,)
            ).fetchone()[0]
        except:
            recent_attr = 0
        
        conn.close()
        
        coverage = f"{attr_count}/{total}" if total > 0 else "0/0"
        pct = (attr_count / total * 100) if total > 0 else 0
        recent_pct = (recent_attr / recent_total * 100) if recent_total > 0 else 0
        
        if pct >= 80:
            status = f"✅ {coverage} ({pct:.0f}%)"
        elif pct >= 50:
            status = f"⚠️ {coverage} ({pct:.0f}%)"
        else:
            status = f"❌ {coverage} ({pct:.0f}%)"
        
        return {
            "total": total, "attr_count": attr_count, "pct": pct,
            "recent_total": recent_total, "recent_attr": recent_attr,
            "recent_pct": recent_pct, "status": status
        }
    except Exception as e:
        return {"status": f"❌ {e}"}

def check_hook_logs():
    """检查 on_position_opened 日志调用"""
    if not os.path.exists(LOG_PATH):
        return {"status": "❌ 日志文件不存在"}
    
    output, rc = run_cmd(f"grep -c 'on_position_opened' {LOG_PATH}")
    opened_count = int(output) if rc == 0 and output.isdigit() else 0
    
    output, rc = run_cmd(f"grep -c 'on_position_closed' {LOG_PATH}")
    closed_count = int(output) if rc == 0 and output.isdigit() else 0
    
    # 检查最近 24h 内的调用
    output, rc = run_cmd(
        f"tail -5000 {LOG_PATH} | grep -c 'on_position_opened\\|on_position_closed'"
    )
    recent = int(output) if rc == 0 and output.isdigit() else 0
    
    status = f"opened={opened_count} closed={closed_count} recent_24h={recent}"
    return {"opened": opened_count, "closed": closed_count, "recent": recent, "status": status}

def check_trade_quality():
    """检查当日交易质量"""
    try:
        conn = sqlite3.connect(DB_PATH)
        today = datetime.now().strftime("%Y-%m-%d")
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        
        # 今日交易
        today_trades = conn.execute("""
            SELECT COUNT(*) as cnt,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                   COALESCE(SUM(realized_pnl), 0) as total_pnl,
                   COALESCE(AVG(realized_pnl), 0) as avg_pnl
            FROM trade_records
            WHERE exit_time IS NOT NULL AND date(exit_time) = ?
        """, (today,)).fetchone()
        
        # 近 7 天
        week_trades = conn.execute("""
            SELECT COUNT(*) as cnt,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                   COALESCE(SUM(realized_pnl), 0) as total_pnl,
                   COALESCE(AVG(realized_pnl), 0) as avg_pnl
            FROM trade_records
            WHERE exit_time IS NOT NULL AND date(exit_time) >= ?
        """, (week_ago,)).fetchone()
        
        # 总累计
        all_trades = conn.execute("""
            SELECT COUNT(*) as cnt,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                   COALESCE(SUM(realized_pnl), 0) as total_pnl,
                   COALESCE(AVG(realized_pnl), 0) as avg_pnl
            FROM trade_records
            WHERE exit_time IS NOT NULL
        """).fetchone()
        
        # Scalping 策略表现
        try:
            scalp = conn.execute("""
                SELECT COUNT(*) as cnt,
                       SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                       COALESCE(SUM(realized_pnl), 0) as total_pnl
                FROM trade_records
                WHERE exit_time IS NOT NULL AND strategy_id = 'scalping'
            """).fetchone()
            scalp_str = f"scalping: {scalp[0]}笔 胜率{scalp[1]/scalp[0]*100:.0f}% PnL=${scalp[2]:.2f}" if scalp[0] > 0 else "scalping: 无交易"
        except:
            scalp_str = "scalping: 查询失败"
        
        conn.close()
        
        def fmt(row, label):
            cnt, wins, pnl, avg = row
            if cnt == 0:
                return f"{label}: 无交易"
            wr = wins / cnt * 100
            return f"{label}: {cnt}笔 胜率{wr:.0f}% PnL=${pnl:.2f} avg=${avg:.2f}"
        
        return {
            "today": fmt(today_trades, "今日"),
            "week": fmt(week_trades, "近7天"),
            "all": fmt(all_trades, "累计"),
            "scalping": scalp_str,
        }
    except Exception as e:
        return {"error": str(e)}

def check_service_health():
    """检查服务健康状态"""
    output, rc = run_cmd(f"systemctl is-active {SERVICE_NAME}")
    active = output == "active"
    
    output, rc = run_cmd(f"systemctl show {SERVICE_NAME} --property=NRestarts")
    restarts = output.split("=")[-1] if "=" in output else "?"
    
    output, rc = run_cmd(f"systemctl show {SERVICE_NAME} --property=ActiveEnterTimestamp")
    since = output.split("=")[-1] if "=" in output else "?"
    
    # 检查 Futu 连接重置
    if os.path.exists(LOG_PATH):
        output, rc = run_cmd(f"grep -c 'ret=-1' {LOG_PATH}")
        futu_resets = int(output) if rc == 0 and output.isdigit() else 0
        
        output, rc = run_cmd(f"tail -500 {LOG_PATH} | grep -c 'ret=-1'")
        recent_resets = int(output) if rc == 0 and output.isdigit() else 0
    else:
        futu_resets = 0
        recent_resets = 0
    
    # 检查 ERROR 日志
    if os.path.exists(LOG_PATH):
        output, rc = run_cmd(f"tail -2000 {LOG_PATH} | grep -c 'ERROR'")
        errors = int(output) if rc == 0 and output.isdigit() else 0
    else:
        errors = 0
    
    status = "🟢 running" if active else "🔴 stopped"
    return {
        "active": active, "status": status,
        "restarts": restarts, "since": since,
        "futu_resets": futu_resets, "recent_futu_resets": recent_resets,
        "errors_recent": errors,
    }

def check_option_trades():
    """检查期权交易情况"""
    try:
        conn = sqlite3.connect(DB_PATH)
        
        # 期权交易统计
        opts = conn.execute("""
            SELECT COUNT(*) as cnt,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                   COALESCE(SUM(realized_pnl), 0) as total_pnl
            FROM trade_records
            WHERE exit_time IS NOT NULL AND (strategy_id LIKE '%option%' OR symbol LIKE '%P%')
        """).fetchone()
        
        # 最近期权交易
        recent = conn.execute("""
            SELECT symbol, realized_pnl, exit_time
            FROM trade_records
            WHERE exit_time IS NOT NULL AND (strategy_id LIKE '%option%' OR symbol LIKE '%P%')
            ORDER BY exit_time DESC LIMIT 3
        """).fetchall()
        
        conn.close()
        
        return {
            "count": opts[0],
            "wins": opts[1] or 0,
            "pnl": opts[2] or 0,
            "recent": [(r[0], r[1], r[2]) for r in recent],
        }
    except Exception as e:
        return {"error": str(e)}

def main():
    now = datetime.now()
    print(f"# 🔍 FTSeek-US 盘后诊断报告")
    print(f"**时间**: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    # 1. 服务健康
    svc = check_service_health()
    print(f"## 1️⃣ 服务健康: {svc['status']}")
    print(f"- 重启次数: {svc['restarts']} | 启动于: {svc['since']}")
    print(f"- Futu 连接重置: 累计{svc['futu_resets']}次, 近期{svc['recent_futu_resets']}次")
    print(f"- 近期 ERROR 日志: {svc['errors_recent']}条")
    print()
    
    # 2. 期权 PRICE_FAIL
    pf = check_price_fail_closed()
    print(f"## 2️⃣ 期权 PRICE_FAIL 阻断: {pf['status']}")
    if pf.get("symbols"):
        for s in pf["symbols"]:
            print(f"  - {s}")
    print()
    
    # 3. 期权交易
    opt = check_option_trades()
    if "error" not in opt:
        wr = (opt["wins"] / opt["count"] * 100) if opt["count"] > 0 else 0
        emoji = "🟢" if opt["pnl"] >= 0 else "🔴"
        print(f"## 3️⃣ 期权交易: {opt['count']}笔 胜率{wr:.0f}% {emoji} PnL=${opt['pnl']:.2f}")
        if opt["recent"]:
            for s, p, t in opt["recent"]:
                print(f"  - {s}: ${p:.2f} ({t})")
    else:
        print(f"## 3️⃣ 期权交易: {opt['error']}")
    print()
    
    # 4. 交易归因
    attr = check_trade_attribution()
    print(f"## 4️⃣ 交易归因覆盖率: {attr.get('status', 'N/A')}")
    if "recent_pct" in attr:
        print(f"- 近7天: {attr['recent_attr']}/{attr['recent_total']} ({attr['recent_pct']:.0f}%)")
    print()
    
    # 5. Hook 日志
    hook = check_hook_logs()
    print(f"## 5️⃣ PostTrade Hook 日志: {hook.get('status', 'N/A')}")
    print()
    
    # 6. 交易质量
    tq = check_trade_quality()
    print(f"## 6️⃣ 交易质量")
    if "error" in tq:
        print(f"- ❌ {tq['error']}")
    else:
        for k in ["today", "week", "all", "scalping"]:
            if k in tq:
                print(f"- {tq[k]}")
    print()
    
    # 7. 诊断建议
    print(f"## 📋 诊断建议")
    issues = []
    
    if not svc.get("active"):
        issues.append("🔴 服务未运行，需立即检查")
    if svc.get("errors_recent", 0) > 100:
        issues.append(f"⚠️ 近期 ERROR 日志 {svc['errors_recent']} 条，需排查")
    if svc.get("recent_futu_resets", 0) > 20:
        issues.append(f"⚠️ Futu 连接频繁重置 ({svc['recent_futu_resets']}次)，需检查")
    
    pf_count = pf.get("count", 0)
    if pf_count > 10:
        issues.append(f"⚠️ PRICE_FAIL 阻断集合 {pf_count} 条，确认是否持续增长")
    
    if "pct" in attr and attr["pct"] < 50:
        issues.append(f"⚠️ 归因覆盖率仅 {attr['pct']:.0f}%，检查 hook 调用链")
    
    if "error" not in tq:
        pass  # 可在此添加更多质量判断
    
    if not issues:
        print("- ✅ 无异常，继续观察")
    else:
        for issue in issues:
            print(f"- {issue}")

if __name__ == "__main__":
    main()
