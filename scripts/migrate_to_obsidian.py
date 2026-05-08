#!/usr/bin/env python3
"""
FTSeek 历史数据迁移脚本 — SQLite/JSON → Obsidian Markdown (Phase 1)

用法:
    cd /opt/ftseek/FTSeek
    python3 scripts/migrate_to_obsidian.py          # 预览模式
    python3 scripts/migrate_to_obsidian.py --apply  # 实际写入
    python3 scripts/migrate_to_obsidian.py --days 30  # 只迁移最近30天

迁移范围:
  1. ftseek.db 中的 positions (CLOSED) → trades/
  2. trade_attribution 表 → attribution/
  3. hk_lessons.db → lessons/
  4. daily_battle_plan.json → daily/premarket/
  5. daily_reviews/*.json → daily/postmarket/
"""

import sys
import os
import json
import sqlite3
import argparse
from datetime import datetime, timedelta

# 添加项目路径
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.core.obsidian_writer import ObsidianWriter

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'ftseek.db')
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')


def get_db():
    if not os.path.exists(DB_PATH):
        print(f"❌ 数据库不存在: {DB_PATH}")
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def migrate_trades(writer, days=None, apply=False):
    """迁移已平仓交易 → trades/"""
    conn = get_db()
    if not conn:
        return 0
    
    query = """
        SELECT id, code, action, quantity, entry_price, exit_price,
               entry_time, exit_time, realized_pl, strategy, status,
               stop_loss, take_profit, highest_price, lowest_price, pl_ratio
        FROM positions
        WHERE status = 'CLOSED'
        AND entry_time IS NOT NULL
    """
    if days:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        query += f" AND entry_time >= '{cutoff}'"
    
    query += " ORDER BY entry_time DESC"
    
    cursor = conn.cursor()
    cursor.execute(query)
    rows = cursor.fetchall()
    
    count = 0
    for row in rows:
        pnl_rate = row['pl_ratio'] or 0
        data = {
            'id': str(row['id']),
            'code': row['code'],
            'market': 'HK',
            'entry_time': row['entry_time'],
            'exit_time': row['exit_time'],
            'entry_price': row['entry_price'],
            'exit_price': row['exit_price'],
            'quantity': row['quantity'],
            'pnl': row['realized_pl'] or 0,
            'pnl_pct': pnl_rate,
            'strategy': row['strategy'] or 'unknown',
            'action': row['action'],
            'exit_reason': '',
            'tags': [row['strategy'] or 'unknown', row['action'].lower()],
            'stop_loss': row['stop_loss'],
            'take_profit': row['take_profit'],
            'highest_price': row['highest_price'],
            'lowest_price': row['lowest_price'],
        }
        
        if apply:
            path = writer.write_trade(data)
            if path:
                count += 1
        else:
            count += 1
    
    conn.close()
    return count


def migrate_lessons(writer, days=None, apply=False):
    """迁移历史教训 → lessons/"""
    lessons_db = os.path.join(DATA_DIR, 'hk_lessons.db')
    if not os.path.exists(lessons_db):
        print(f"  ⚠️ hk_lessons.db 不存在，跳过")
        return 0
    
    conn = sqlite3.connect(lessons_db)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    query = "SELECT * FROM lessons"
    if days:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        query += f" WHERE created_at >= '{cutoff}'"
    query += " ORDER BY created_at DESC"
    
    cursor.execute(query)
    rows = cursor.fetchall()
    
    count = 0
    for row in rows:
        data = {
            'topic': row.get('topic', row.get('title', 'unknown')),
            'title': row.get('title', row.get('topic', 'Unknown Lesson')),
            'market': 'HK',
            'date': row.get('created_at', datetime.now().strftime("%Y-%m-%d"))[:10],
            'severity': 'P2',
            'category': 'unknown',
            'description': row.get('content', '') or row.get('lesson_text', ''),
            'root_cause': '',
            'fix_applied': '',
            'prevention': '',
        }
        
        if apply:
            path = writer.write_lesson(data, overwrite=True)
            if path:
                count += 1
        else:
            count += 1
    
    conn.close()
    return count


def migrate_daily_reviews(writer, days=None, apply=False):
    """迁移每日复盘 JSON → daily/postmarket/"""
    review_dir = os.path.join(DATA_DIR, 'daily_reviews')
    if not os.path.exists(review_dir):
        print(f"  ⚠️ daily_reviews 目录不存在，跳过")
        return 0
    
    count = 0
    for filename in os.listdir(review_dir):
        if not filename.endswith('.json'):
            continue
        
        filepath = os.path.join(review_dir, filename)
        try:
            with open(filepath) as f:
                data = json.load(f)
            
            date_str = data.get('date', filename.replace('daily_review_', '').replace('.json', '')[:10])
            
            if days:
                cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
                if date_str < cutoff:
                    continue
            
            review_data = {
                'date': date_str,
                'market': 'HK',
                'total_trades': data.get('total_trades', data.get('trade_count', 0)),
                'win_rate': data.get('win_rate', 0),
                'total_pnl': data.get('total_pnl', 0),
                'max_drawdown': data.get('max_drawdown', 0),
                'trades': data.get('trades', []),
            }
            
            if apply:
                path = writer.write_daily_postmarket(review_data)
                if path:
                    count += 1
            else:
                count += 1
        except Exception as e:
            print(f"  ⚠️ 读取 {filename} 失败: {e}")
    
    return count


def migrate_battle_plans(writer, days=None, apply=False):
    """迁移作战计划 JSON → daily/premarket/"""
    plan_path = os.path.join(DATA_DIR, 'daily_battle_plan.json')
    if not os.path.exists(plan_path):
        print(f"  ⚠️ daily_battle_plan.json 不存在，跳过")
        return 0
    
    try:
        with open(plan_path) as f:
            data = json.load(f)
        
        date_str = data.get('date', datetime.now().strftime("%Y-%m-%d"))
        
        if days:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            if date_str < cutoff:
                return 0
        
        plan_data = {
            'date': date_str,
            'market': 'HK',
            'targets': [t.get('symbol', '') for t in data.get('targets', [])],
            'restricted': [t.get('symbol', '') for t in data.get('restricted', [])],
            'overall_bias': data.get('overall_bias', '中性'),
            'regime': data.get('market_regime', 'unknown'),
            'generated_at': data.get('generated_at', ''),
        }
        
        if apply:
            path = writer.write_daily_premarket(plan_data)
            return 1 if path else 0
        return 1
    except Exception as e:
        print(f"  ⚠️ 读取 battle plan 失败: {e}")
        return 0


def main():
    parser = argparse.ArgumentParser(description='FTSeek → Obsidian 历史数据迁移')
    parser.add_argument('--apply', action='store_true', help='实际写入 (默认仅预览)')
    parser.add_argument('--days', type=int, default=None, help='只迁移最近 N 天')
    args = parser.parse_args()
    
    writer = ObsidianWriter()
    
    mode = "✅ 写入模式" if args.apply else "👁️ 预览模式"
    days_filter = f" (最近 {args.days} 天)" if args.days else ""
    
    print(f"\n{'='*60}")
    print(f"FTSeek → Obsidian 历史数据迁移 {mode}{days_filter}")
    print(f"{'='*60}\n")
    
    # 1. 交易记录
    print("1. 迁移交易记录 (positions → trades/)...")
    trade_count = migrate_trades(writer, args.days, args.apply)
    print(f"   {'✅' if args.apply else '📋'} {trade_count} 笔交易")
    
    # 2. 经验教训
    print("\n2. 迁移经验教训 (hk_lessons → lessons/)...")
    lesson_count = migrate_lessons(writer, args.days, args.apply)
    print(f"   {'✅' if args.apply else '📋'} {lesson_count} 条教训")
    
    # 3. 每日复盘
    print("\n3. 迁移每日复盘 (daily_reviews → daily/postmarket/)...")
    review_count = migrate_daily_reviews(writer, args.days, args.apply)
    print(f"   {'✅' if args.apply else '📋'} {review_count} 份复盘")
    
    # 4. 作战计划
    print("\n4. 迁移作战计划 (daily_battle_plan → daily/premarket/)...")
    plan_count = migrate_battle_plans(writer, args.days, args.apply)
    print(f"   {'✅' if args.apply else '📋'} {plan_count} 份计划")
    
    total = trade_count + lesson_count + review_count + plan_count
    
    print(f"\n{'='*60}")
    if args.apply:
        print(f"✅ 迁移完成: {total} 条记录已写入 Obsidian vault")
        print(f"📁 Vault 路径: {writer.vault}")
    else:
        print(f"👁️ 预览完成: 将迁移 {total} 条记录")
        print(f"💡 使用 --apply 参数执行实际写入")
    print(f"{'='*60}\n")


if __name__ == '__main__':
    main()
