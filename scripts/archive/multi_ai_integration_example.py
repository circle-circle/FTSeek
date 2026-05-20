"""
多AI决策增强系统 - 集成使用示例

展示如何在scheduler_tasks.py中集成完整决策链路和推送功能
"""

# ============================================================================
# 示例1: 在信号扫描中使用完整决策管线
# ============================================================================

def process_symbol_with_full_decision(symbol: str, market_data: dict, notifier):
    """
    使用完整决策管线处理单个股票
    
    这个函数应该替换 scheduler_tasks.py 中的 process_symbol()
    """
    from app.agents.voting.voting_coordinator import VotingCoordinator
    from app.notifier.dingtalk import DingTalkNotifier
    from app.notifier.feishu import FeishuNotifier
    
    # 初始化协调器 (启用辩论)
    coordinator = VotingCoordinator(
        timeout_seconds=90,
        enable_debate=True,
    )
    
    # 获取账户状态 (从数据库或内存)
    account_state = {
        "daily_loss": 0,
        "total_position": 0.5,
        "consecutive_losses": 0,
    }
    
    # 运行完整决策管线
    final_decision = coordinator.run_full_decision_pipeline(
        market_data=market_data,
        account_state=account_state,
    )
    
    # 推送决策报告 (钉钉 + 飞书)
    if final_decision.action != "HOLD":
        try:
            # 钉钉推送
            dingtalk = DingTalkNotifier()
            dingtalk.notify_full_decision(final_decision)
            
            # 飞书推送
            feishu = FeishuNotifier()
            feishu.notify_full_decision(final_decision)
            
        except Exception as e:
            print(f"[推送失败] {e}")
    
    # 如果批准执行,提交订单
    if final_decision.approved and final_decision.trade_decision:
        td = final_decision.trade_decision
        
        # 调用执行引擎
        # execution_engine.submit_order(...)
        
        print(f"[决策执行] {symbol} {td.action} @ ${td.stop_loss:.2f} SL, ${td.take_profit[0]:.2f} TP")
    
    return final_decision


# ============================================================================
# 示例2: 盘前策略展望推送
# ============================================================================

def pre_market_strategy_push():
    """
    盘前策略展望推送 (美东 09:00)
    
    这个函数应该注册到 scheduler 的定时任务中
    """
    from app.reports.pre_market_strategy import PreMarketStrategyReport
    from app.notifier.dingtalk import DingTalkNotifier
    from app.notifier.feishu import FeishuNotifier
    
    print("[盘前推送] 开始生成盘前策略展望...")
    
    # 获取市场环境数据
    market_context = {
        "vix": 18.5,
        "spx_change": 0.003,
        "spy_ma200_ratio": 1.02,
        "symbols_preview": [
            # 这里应该调用快速评分 (规则fallback,不调用LLM)
            {"symbol": "AAPL", "strength": 72, "direction": "LONG", "model_scores": []},
            {"symbol": "NVDA", "strength": 68, "direction": "LONG", "model_scores": []},
        ],
        "current_positions": [
            {"symbol": "TSLA", "pnl_pct": 0.032},
        ],
        "today_events": ["14:00 美联储讲话"],
    }
    
    # 生成报告
    report_generator = PreMarketStrategyReport()
    report = report_generator.generate_report(market_context)
    
    # 推送 (钉钉 + 飞书)
    try:
        dingtalk = DingTalkNotifier()
        dingtalk.send_text(report)
        
        feishu = FeishuNotifier()
        feishu.send_text(report)
        
        print("[盘前推送] 推送成功")
    except Exception as e:
        print(f"[盘前推送] 推送失败: {e}")


# ============================================================================
# 示例3: 盘后总结推送
# ============================================================================

def post_market_summary_push():
    """
    盘后总结推送 (美东 16:05)
    
    这个函数应该注册到 scheduler 的定时任务中
    """
    from app.reports.post_market_summary import PostMarketSummaryReport
    from app.notifier.dingtalk import DingTalkNotifier
    from app.notifier.feishu import FeishuNotifier
    
    print("[盘后推送] 开始生成盘后总结...")
    
    # 获取今日交易统计 (从数据库)
    trading_stats = {
        "date": "2026-04-05",
        "total_trades": 5,
        "wins": 3,
        "losses": 2,
        "total_pnl": 250.50,
        "win_rate": 0.6,
        "trades_detail": [
            {"symbol": "AAPL", "action": "BUY", "pnl": 120.0, "pnl_pct": 0.032},
            {"symbol": "NVDA", "action": "SELL", "pnl": -50.0, "pnl_pct": -0.015},
        ],
        "current_positions": [
            {"symbol": "TSLA", "pnl_pct": 0.025, "holding_days": 2},
        ],
        "best_trade": {"symbol": "AAPL", "pnl_pct": 0.032},
        "worst_trade": {"symbol": "NVDA", "pnl_pct": -0.015},
        "strategy_performance": {
            "voting_llm": {"trades": 5, "win_rate": 0.6, "pnl": 250.50},
        },
        "improvements": ["建议提高入场标准", "止损执行良好"],
    }
    
    # 生成报告
    report_generator = PostMarketSummaryReport()
    report = report_generator.generate_report(trading_stats)
    
    # 推送 (钉钉 + 飞书)
    try:
        dingtalk = DingTalkNotifier()
        dingtalk.send_text(report)
        
        feishu = FeishuNotifier()
        feishu.send_text(report)
        
        print("[盘后推送] 推送成功")
    except Exception as e:
        print(f"[盘后推送] 推送失败: {e}")


# ============================================================================
# 示例4: 注册定时任务 (在 scheduler_core.py 中)
# ============================================================================

"""
在 TradingScheduler.__init__() 或 _setup_jobs() 中添加:

from apscheduler.triggers.cron import CronTrigger
from app.core.scheduler_tasks import (
    pre_market_strategy_push,
    post_market_summary_push,
)

# 盘前推送 (美东 09:00 = 北京 21:00/22:00)
self.scheduler.add_job(
    pre_market_strategy_push,
    CronTrigger(hour=21, minute=0, timezone='Asia/Shanghai'),  # 冬季
    # CronTrigger(hour=22, minute=0, timezone='Asia/Shanghai'),  # 夏季
    id='pre_market_strategy_push',
    name='盘前策略展望',
    replace_existing=True,
)

# 盘后推送 (美东 16:05 = 北京 04:05/05:05)
self.scheduler.add_job(
    post_market_summary_push,
    CronTrigger(hour=4, minute=5, timezone='Asia/Shanghai'),  # 冬季
    # CronTrigger(hour=5, minute=5, timezone='Asia/Shanghai'),  # 夏季
    id='post_market_summary_push',
    name='盘后总结',
    replace_existing=True,
)
"""

# ============================================================================
# 示例5: 在信号扫描中推送完整决策
# ============================================================================

"""
在 process_symbol() 函数中替换原有逻辑:

def process_symbol(symbol: str, market_data: dict):
    '''处理单个股票 - 使用完整决策管线'''
    
    # 原有逻辑: 获取市场数据
    # ...
    
    # 新逻辑: 使用完整决策管线
    from app.agents.voting.voting_coordinator import VotingCoordinator
    from app.notifier.dingtalk import DingTalkNotifier
    
    coordinator = VotingCoordinator(enable_debate=True)
    final_decision = coordinator.run_full_decision_pipeline(market_data)
    
    # 推送完整决策报告
    if final_decision.action != "HOLD":
        notifier = DingTalkNotifier()
        notifier.notify_full_decision(final_decision)
    
    # 原有逻辑: 如果批准则执行
    if final_decision.approved:
        # 提交订单
        pass
    
    return final_decision
"""


if __name__ == "__main__":
    print("=" * 80)
    print("多AI决策增强系统 - 集成使用示例")
    print("=" * 80)
    print("\n请查看代码注释了解集成方法")
    print("\n关键步骤:")
    print("1. 在 process_symbol() 中使用 VotingCoordinator.run_full_decision_pipeline()")
    print("2. 使用 DingTalkNotifier.notify_full_decision() 推送决策报告")
    print("3. 在 scheduler 中注册盘前/盘后定时任务")
    print("\n测试盘前推送:")
    pre_market_strategy_push()
    print("\n测试盘后推送:")
    post_market_summary_push()
