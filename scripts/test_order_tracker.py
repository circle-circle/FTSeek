#!/usr/bin/env python3
"""
测试 OrderTracker 初始化并创建表
"""
import sys
sys.path.insert(0, "/opt/ftseek/FTSeek-US")

from app.risk.order_tracker import get_order_tracker

def main():
    print("=== 测试 OrderTracker 初始化 ===")
    try:
        tracker = get_order_tracker()
        print(f"✅ 初始化成功")
        print(f"待追踪订单数：{len(tracker._pending_orders)}")
        print(f"数据库路径：{tracker.db_path}")
        
        # 检查表是否创建
        import sqlite3
        conn = sqlite3.connect(tracker.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='order_tracking'")
        result = cursor.fetchone()
        if result:
            print(f"✅ order_tracking 表已创建")
            
            # 查看表结构
            cursor.execute("PRAGMA table_info(order_tracking)")
            cols = cursor.fetchall()
            print(f"表字段：{[c[1] for c in cols]}")
        else:
            print(f"❌ order_tracking 表未创建")
        conn.close()
        
    except Exception as e:
        print(f"❌ 初始化失败：{e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
