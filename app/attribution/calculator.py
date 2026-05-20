#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 归因计算器 (Attribution Calculator)
Phase 3: Alpha/Beta 剥离与策略归因

功能:
1. 获取基准指数 (HSI, HSTECH) 数据
2. 计算单笔交易的 Alpha
3. 分析信号来源 (Source) 和 市场状态 (Regime) 的胜率
"""
import sqlite3
import json
import yaml
import logging
from datetime import datetime, timedelta
from typing import Dict, List
from futu import OpenQuoteContext, KLType, RET_OK

from app.storage.repository import get_db as _pool_get_db  # B3-13 连接池

logger = logging.getLogger("ftseek.attribution.calc")

DB_PATH = "/opt/ftseek/FTSeek/data/ftseek.db"
CONFIG_PATH = "/opt/ftseek/FTSeek/config/unified_config.yaml"

# 基准指数代码
BENCHMARKS = {
    "HSI": "HK.800000",       # 恒生指数
    "HSTECH": "HK.810000"     # 恒生科技指数
}

def get_opend_config():
    """直接读取 yaml 配置"""
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)
        return {
            "host": cfg.get('futu', {}).get('host', '127.0.0.1'),
            "port": cfg.get('futu', {}).get('port', 11111)
        }
    except Exception as e:
        logger.warning(f"读取配置失败，使用默认值: {e}")
        return {"host": "127.0.0.1", "port": 11111}

def get_benchmark_performance(start_date: str, end_date: str) -> Dict:
    """
    获取指定时间段内基准指数的表现 (涨跌幅)
    """
    results = {}
    cfg = get_opend_config()
    try:
        ctx = OpenQuoteContext(host=cfg['host'], port=cfg['port'])
        for name, code in BENCHMARKS.items():
            # 检查代码有效性 (HSTECH might be 810000 or similar)
            ret, data, _ = ctx.request_history_kline(code, ktype=KLType.K_DAY, start=start_date, end=end_date)
            if ret == RET_OK and not data.empty:
                start_price = data['close'].iloc[0]
                end_price = data['close'].iloc[-1]
                performance = (end_price - start_price) / start_price
                results[name] = performance
                logger.info(f"基准 {name} 表现: {performance:.2%} ({start_date} -> {end_date})")
            else:
                logger.warning(f"获取基准 {name} 数据失败: {data}")
                results[name] = 0.0
        ctx.close()
    except Exception as e:
        logger.error(f"获取基准数据异常: {e}")
        results = {k: 0.0 for k in BENCHMARKS}
    return results

def calculate_attribution(days: int = 7):
    """
    执行归因计算主流程
    """
    try:
        # 1. 获取待分析的交易记录 (FILLED 状态)
        with _pool_get_db(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 获取最近 N 天的已成交记录
            cursor.execute('''
                SELECT trace_id, code, signal_price, actual_price, audit_data, timestamp
                FROM trade_attribution 
                WHERE event_type = 'FILLED' 
                AND date(timestamp) >= date('now', ?)
                ORDER BY timestamp DESC
            ''', (f'-{days} days',))

            trades = cursor.fetchall()
        if not trades:
            logger.info(f"过去 {days} 天无成交记录，跳过归因计算。")
            return []

        # 2. 批量获取基准表现
        start_dt = datetime.now() - timedelta(days=days)
        end_dt = datetime.now()
        
        try:
            bmk_perf = get_benchmark_performance(start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"))
        except Exception:
            bmk_perf = {"HSI": 0.0, "HSTECH": 0.0}

        # 3. 逐笔计算 Alpha / Slippage
        analysis_results = []
        source_stats = {}

        for trade in trades:
            audit = json.loads(trade['audit_data']) if trade['audit_data'] else {}
            scores = audit.get('scores', {})
            
            # 计算滑点百分比: (Actual - Signal) / Signal
            # Note: For BUY, Actual > Signal is bad. For SELL, Actual < Signal is bad.
            # We currently don't store action in trade_attribution easily, 
            # but we can infer from context or just track absolute slippage quality.
            
            sig_price = trade.get('signal_price', 0)
            act_price = trade.get('actual_price', 0)
            
            slippage_pct = 0.0
            if sig_price > 0:
                slippage_pct = (act_price - sig_price) / sig_price
            
            source = scores.get('source', 'UNKNOWN')
            
            # Source 统计
            if source not in source_stats:
                source_stats[source] = {"count": 0, "total_slippage_pct": 0.0, "avg_confidence": 0.0}
            source_stats[source]["count"] += 1
            source_stats[source]["total_slippage_pct"] += abs(slippage_pct)
            source_stats[source]["avg_confidence"] += scores.get('consensus', 0)

            result = {
                "trace_id": trade['trace_id'],
                "code": trade['code'],
                "slippage_pct": slippage_pct,
                "source": source,
                "consensus": scores.get('consensus', 0),
                "benchmark_hsi": bmk_perf.get("HSI", 0),
            }
            analysis_results.append(result)
            
        # 计算均值
        for src in source_stats:
            stats = source_stats[src]
            if stats['count'] > 0:
                stats['avg_slippage_pct'] = stats['total_slippage_pct'] / stats['count']
                stats['avg_confidence'] = stats['avg_confidence'] / stats['count']
            else:
                stats['avg_slippage_pct'] = 0
            
        # 输出结论
        insights = []
        for src, stats in source_stats.items():
            # Threshold 0.5% slippage
            if stats['avg_slippage_pct'] > 0.005: 
                insights.append(f"⚠️ 来源 [{src}] 平均滑点较高 ({stats['avg_slippage_pct']:.2%})，建议降低权重或限价单")
            if stats['avg_confidence'] < 0.4 and stats['count'] > 5:
                insights.append(f"ℹ️ 来源 [{src}] 置信度较低，但交易频繁，建议审查信号质量")

        logger.info(f"✅ 归因计算完成 | 交易数: {len(analysis_results)}")
        logger.info(f"📊 来源统计: {source_stats}")
        for ins in insights:
            logger.warning(f"💡 归因洞察: {ins}")

        return analysis_results

    except Exception as e:
        logger.error(f"归因计算失败: {e}")
        import traceback
        traceback.print_exc()
        return []

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    res = calculate_attribution(30)
    print(f"Analysis Results Count: {len(res)}")
