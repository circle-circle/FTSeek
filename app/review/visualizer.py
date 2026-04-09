#!/usr/bin/env python3
"""
深度复盘可视化看板

功能:
- 展示交易复盘数据
- 显示策略表现
- 错误模式分析
- 心理状态追踪

执行:
    cd /opt/ftseek/FTSeek && python3 app/review/visualizer.py
"""

import sys
import os
from pathlib import Path
import json
import logging
from datetime import datetime

# 添加项目路径 - 从项目根目录开始
project_root = Path('/opt/ftseek/FTSeek')
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from app.storage.review_tables import get_review_database

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("visualizer")


# 全局数据目录
DATA_DIR = Path('/opt/ftseek/FTSeek/data')
REVIEW_DIR = Path('/opt/ftseek/FTSeek/data/daily_reviews')
OUTPUT_DIR = Path('/opt/ftseek/FTSeek/review_system/output')

REVIEW_DB = get_review_database()


class DashboardData:
    """仪表盘数据构建器"""
    
    def __init__(self):
        self.db = get_review_database()
    
    def get_summary(self) -> dict:
        """获取摘要数据"""
        today = datetime.now().strftime('%Y-%m-%d')
        
        # 从 trades_review 表获取数据
        stats = self.db.get_review_statistics(today, today)
        
        return {
            'date': today,
            'generated_at': datetime.now().isoformat(),
            'summary': {
                'total_trades': stats.get('total_trades', 0),
                'winning_trades': stats.get('winning_trades', 0),
                'losing_trades': stats.get('losing_trades', 0),
                'total_pnl': stats.get('total_pnl', 0),
                'win_rate': stats.get('win_rate', 0),
                'emotion_distribution': stats.get('emotion_distribution', {}),
                'avg_decision_confidence': stats.get('avg_decision_confidence', 0),
                'avg_entry_timing_score': stats.get('avg_entry_timing_score', 0),
            }
        }
    
    def get_trades(self, limit: int = 20) -> list:
        """获取最近交易"""
        today = datetime.now().strftime('%Y-%m-%d')
        
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, code, action, entry_price, exit_price, pnl, pnl_percent, 
                       strategy, entry_time, exit_reason, emotion_state
                FROM trades_review
                WHERE entry_time >= ?
                ORDER BY entry_time DESC
                LIMIT ?
            """, (today, limit))
            
            return [dict(row) for row in cursor.fetchall()]
    
    def get_patterns(self, limit: int = 10) -> list:
        """获取错误模式"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT pattern_name, pattern_category, severity_level, 
                       occurrence_count, first_occurrence
                FROM error_patterns
                WHERE is_resolved = 0
                ORDER BY occurrence_count DESC
                LIMIT ?
            """, (limit,))
            
            return [dict(row) for row in cursor.fetchall()]
    
    def get_improvements(self) -> list:
        """获取待处理改进行动"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT action_title, action_category, priority, status, 
                       created_date, target_date
                FROM improvement_actions
                WHERE status = 'pending'
                ORDER BY priority DESC, created_date DESC
            """)
            
            return [dict(row) for row in cursor.fetchall()]
    
    def get_psychology(self) -> dict:
        """获取心理统计"""
        today = datetime.now().strftime('%Y-%m-%d')
        
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT calm_count, anxious_count, excited_count, 
                       fearful_count, greedy_count, frustrated_count
                FROM psychology_stats
                WHERE stat_date = ?
            """, (today,))
            
            row = cursor.fetchone()
            return dict(row) if row else {}


def generate_html():
    """生成仪表盘 HTML"""
    
    data = DashboardData()
    
    # 获取数据
    summary = data.get_summary()
    trades = data.get_trades(limit=10)
    patterns = data.get_patterns(limit=10)
    improvements = data.get_improvements()
    psychology = data.get_psychology()
    
    # 生成 HTML
    today = datetime.now().strftime('%Y-%m-%d')
    html_content = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FTSeek 深度复盘仪表盘</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: #f5f7fa;
            color: #333;
            padding: 20px;
        }}
        
        .container {{
            max-width: 1400px;
            margin: 0 auto;
        }}
        
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 12px;
            margin-bottom: 20px;
        }}
        
        .header h1 {{
            font-size: 28px;
            margin-bottom: 10px;
        }}
        
        .header p {{
            opacity: 0.9;
        }}
        
        .grid {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 20px;
            margin-bottom: 20px;
        }}
        
        .card {{
            background: white;
            padding: 20px;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
        }}
        
        .card-title {{
            font-size: 14px;
            color: #666;
            margin-bottom: 10px;
        }}
        
        .card-value {{
            font-size: 28px;
            font-weight: bold;
            color: #333;
        }}
        
        .card-value.positive {{ color: #67c23a; }}
        .card-value.negative {{ color: #f56c6c; }}
        
        .section {{
            margin-bottom: 30px;
        }}
        
        .section-title {{
            font-size: 18px;
            font-weight: bold;
            margin-bottom: 15px;
            color: #333;
        }}
        
        .table {{
            width: 100%;
            border-collapse: collapse;
            background: white;
            border-radius: 8px;
            overflow: hidden;
        }}
        
        .table th {{
            background: #f5f7fa;
            padding: 12px;
            text-align: left;
            font-weight: 600;
            color: #666;
        }}
        
        .table td {{
            padding: 12px;
            border-bottom: 1px solid #eee;
        }}
        
        .table tr:last-child td {{
            border-bottom: none;
        }}
        
        .table tr:hover {{
            background: #f5f7fa;
        }}
        
        .badge {{
            display: inline-block;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 600;
        }}
        
        .badge-high {{ background: #fef0f0; color: #f56c6c; }}
        .badge-medium {{ background: #fdf6ec; color: #e6a23c; }}
        .badge-low {{ background: #f4f4f5; color: #909399; }}
        
        .emotion-bar {{
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }}
        
        .emotion-item {{
            background: #f5f7fa;
            padding: 6px 12px;
            border-radius: 4px;
            font-size: 13px;
        }}
        
        .emotion-item span {{
            font-weight: bold;
            color: #667eea;
        }}
        
        .empty-state {{
            text-align: center;
            padding: 40px;
            color: #999;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📊 FTSeek 深度复盘仪表盘</h1>
            <p>系统日期: {today} &nbsp;|&nbsp; 最后更新: {datetime.now().strftime('%H:%M:%S')}</p>
        </div>
        
        <!-- 摘要卡片 -->
        <div class="grid">
            <div class="card">
                <div class="card-title">总交易笔数</div>
                <div class="card-value">{summary['summary']['total_trades']}</div>
            </div>
            <div class="card">
                <div class="card-title">盈利交易</div>
                <div class="card-value positive">{summary['summary']['winning_trades']}</div>
            </div>
            <div class="card">
                <div class="card-title">亏损交易</div>
                <div class="card-value negative">{summary['summary']['losing_trades']}</div>
            </div>
            <div class="card">
                <div class="card-title">胜率</div>
                <div class="card-value {"positive" if summary["summary"]["win_rate"] >= 0.5 else "negative"}">
                    {summary["summary"]["win_rate"] * 100:.1f}%
                </div>
            </div>
        </div>
        
        <!-- 盈亏总结 -->
        <div class="grid">
            <div class="card">
                <div class="card-title">总盈亏</div>
                <div class="card-value {"positive" if summary["summary"]["total_pnl"] >= 0 else "negative"}">
                    {summary["summary"]["total_pnl"]:.2f}
                </div>
            </div>
            <div class="card">
                <div class="card-title">平均决策信心</div>
                <div class="card-value">{summary["summary"]["avg_decision_confidence"]:.1f}</div>
            </div>
            <div class="card">
                <div class="card-title">平均入场时机</div>
                <div class="card-value">{summary["summary"]["avg_entry_timing_score"]:.1f}</div>
            </div>
            <div class="card">
                <div class="card-title">情绪分布</div>
                <div class="emotion-bar">
                    {"".join([f'<div class="emotion-item">{k}: <span>{v}</span></div>' for k, v in summary["summary"]["emotion_distribution"].items()])}
                </div>
            </div>
        </div>
        
        <!-- 最近交易 -->
        <div class="section">
            <div class="section-title">📝 最近交易</div>
            {generate_table_html(trades)}
        </div>
        
        <!-- 错误模式 -->
        <div class="section">
            <div class="section-title">⚠️ 错误模式</div>
            {generate_patterns_html(patterns)}
        </div>
        
        <!-- 改进行动 -->
        <div class="section">
            <div class="section-title">🚀 待处理改进行动</div>
            {generate_improvements_html(improvements)}
        </div>
    </div>
    
    <script>
        // 自动刷新
        setTimeout(() => {{
            location.reload();
        }}, 300000); // 5 分钟刷新一次
    </script>
</body>
</html>'''
    
    return html_content


def generate_table_html(trades):
    """生成表格 HTML"""
    if not trades:
        return '<div class="empty-state">暂无交易记录</div>'
    
    html = '<table class="table"><thead><tr><th>ID</th><th>代码</th><th>方向</th><th>Entry price</th><th>Exit price</th><th>PnL</th><th>盈亏率</th><th>策略</th><th>Entry time</th><th>Exit reason</th><th>情绪</th></tr></thead><tbody>'
    
    for t in trades:
        pnl_class = "positive" if t.get("pnl", 0) > 0 else ("negative" if t.get("pnl", 0) < 0 else "")
        pnl_value = t.get("pnl") if t.get("pnl") is not None else "-"
        exit_price = t.get("exit_price") if t.get("exit_price") is not None else "-"
        
        html += f'''<tr>
            <td>{t["id"]}</td>
            <td>{t["code"]}</td>
            <td>{t["action"]}</td>
            <td>{t["entry_price"]}</td>
            <td>{exit_price}</td>
            <td class="{pnl_class}">{pnl_value}</td>
            <td>{t["pnl_percent"] * 100:.1f}%</td>
            <td>{t["strategy"]}</td>
            <td>{t["entry_time"]}</td>
            <td>{t["exit_reason"] or "-"}</td>
            <td>{t["emotion_state"] or "-"}</td>
        </tr>'''
    
    html += '</tbody></table>'
    return html


def generate_patterns_html(patterns):
    """生成错误模式表格 HTML"""
    if not patterns:
        return '<div class="empty-state">暂无错误模式</div>'
    
    html = '<table class="table"><thead><tr><th>模式名称</th><th>类别</th><th>严重性</th><th>出现次数</th><th>首次出现</th></tr></thead><tbody>'
    
    for p in patterns:
        severity_class = "badge-high" if p.get("severity_level") in ["high", "critical"] else "badge-medium"
        html += f'''<tr>
            <td>{p["pattern_name"]}</td>
            <td>{p["pattern_category"] or "-"}</td>
            <td><span class="badge {severity_class}">{p["severity_level"]}</span></td>
            <td>{p["occurrence_count"]}</td>
            <td>{p["first_occurrence"]}</td>
        </tr>'''
    
    html += '</tbody></table>'
    return html


def generate_improvements_html(improvements):
    """生成改进行动表格 HTML"""
    if not improvements:
        return '<div class="empty-state">暂有待处理行动</div>'
    
    html = '<table class="table"><thead><tr><th>行动标题</th><th>类别</th><th>优先级</th><th>创建日期</th><th>目标日期</th></tr></thead><tbody>'
    
    for a in improvements:
        priority_class = "badge-high" if a.get("priority") in ["high", "critical"] else "badge-medium"
        html += f'''<tr>
            <td>{a["action_title"]}</td>
            <td>{a["action_category"] or "-"}</td>
            <td><span class="badge {priority_class}">{a["priority"]}</span></td>
            <td>{a["created_date"]}</td>
            <td>{a["target_date"] or "-"}</td>
        </tr>'''
    
    html += '</tbody></table>'
    return html


def main():
    """主函数"""
    logger.info("生成仪表盘 HTML...")
    
    html = generate_html()
    
    # 保存到文件
    output_file = Path('/opt/ftseek/FTSeek/app/review/dashboard.html')
    output_file.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html)
    
    logger.info(f"仪表盘已生成: {output_file}")
    
    # 打印预览
    print(html[:1000] + '...')
    
    return True


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
