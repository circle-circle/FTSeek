#!/usr/bin/env python3
"""Phase 1a: 数据质量修复 - 清理重复记录 + 修复异常值"""
import sqlite3
import sys

DB_PATH = '/opt/ftseek/FTSeek/data/ftseek.db'

def main():
    db = sqlite3.connect(DB_PATH)
    c = db.cursor()
    
    print("=" * 60)
    print("Phase 1a: 数据质量修复")
    print("=" * 60)
    
    # ================================================================
    # Step 1: 清理重复记录 (止损/止盈的双重写入)
    # ================================================================
    print("\n[Step 1] 清理重复记录...")
    c.execute("SELECT COUNT(*) FROM trades WHERE strategy IN ('止损', '止盈')")
    dup_count = c.fetchone()[0]
    print(f"  发现 {dup_count} 条重复记录 (止损/止盈)")
    
    if dup_count > 0:
        c.execute("DELETE FROM trades WHERE strategy IN ('止损', '止盈')")
        print(f"  已删除 {c.rowcount} 条")
    else:
        print("  无重复记录")
    
    # ================================================================
    # Step 2: 修复 pnl=1.00 异常值 (占位符)
    # ================================================================
    print("\n[Step 2] 修复 pnl=1.00 异常值...")
    c.execute("SELECT COUNT(*) FROM trades WHERE pnl = 1.00")
    abnormal_count = c.fetchone()[0]
    print(f"  发现 {abnormal_count} 条 pnl=1.00 异常值")
    
    if abnormal_count > 0:
        # 尝试重新计算 PnL = (exit_price - entry_price) * quantity
        c.execute("""
            SELECT id, code, entry_price, exit_price, quantity, strategy
            FROM trades WHERE pnl = 1.00
        """)
        rows = c.fetchall()
        fixed = 0
        for row in rows:
            tid, code, ep, xp, qty, strat = row
            if ep and xp and qty and qty > 0:
                calc_pnl = (xp - ep) * qty
                c.execute("UPDATE trades SET pnl = ? WHERE id = ?", (round(calc_pnl, 2), tid))
                fixed += 1
                print(f"    {code}: {ep:.2f} -> {xp:.2f}, qty={qty}, calc_pnl={calc_pnl:.2f}")
            else:
                # 无法计算，设为0
                c.execute("UPDATE trades SET pnl = 0 WHERE id = ?", (tid,))
                fixed += 1
                print(f"    {code}: 无法计算 (ep={ep}, xp={xp}, qty={qty}), 设为0")
        print(f"  已修复 {fixed} 条")
    
    # ================================================================
    # Step 3: 补充 stop_loss 字段 (从 entry_price 推算)
    # ================================================================
    print("\n[Step 3] 补充 stop_loss 字段...")
    c.execute("SELECT COUNT(*) FROM trades WHERE (stop_loss IS NULL OR stop_loss = 0)")
    sl_missing = c.fetchone()[0]
    print(f"  发现 {sl_missing} 条 stop_loss 为空/0")
    
    if sl_missing > 0:
        # 默认止损 = entry_price * 0.97 (3% 止损)
        c.execute("""
            UPDATE trades 
            SET stop_loss = ROUND(entry_price * 0.97, 2)
            WHERE (stop_loss IS NULL OR stop_loss = 0) AND entry_price > 0
        """)
        print(f"  已补充 {c.rowcount} 条 (默认 3% 止损)")
    
    # ================================================================
    # Step 4: 补充 fees 字段 (从交易金额推算)
    # ================================================================
    print("\n[Step 4] 补充 fees 字段...")
    c.execute("SELECT COUNT(*) FROM trades WHERE (fees IS NULL OR fees = 0) AND pnl IS NOT NULL")
    fees_missing = c.fetchone()[0]
    print(f"  发现 {fees_missing} 条 fees 为空/0")
    
    if fees_missing > 0:
        # 费用 = max(1.0, 交易金额 * 0.1%)
        c.execute("""
            UPDATE trades 
            SET fees = ROUND(MAX(1.0, ABS(entry_price * quantity) * 0.001), 2)
            WHERE (fees IS NULL OR fees = 0) AND entry_price > 0 AND quantity > 0
        """)
        print(f"  已补充 {c.rowcount} 条 (交易金额 * 0.1%)")
    
    # ================================================================
    # Step 5: 验证修复结果
    # ================================================================
    print("\n[Step 5] 验证修复结果...")
    
    c.execute("SELECT COUNT(*) FROM trades WHERE strategy IN ('止损', '止盈')")
    dup_remaining = c.fetchone()[0]
    print(f"  重复记录剩余: {dup_remaining}")
    
    c.execute("SELECT COUNT(*) FROM trades WHERE pnl = 1.00")
    abnormal_remaining = c.fetchone()[0]
    print(f"  pnl=1.00 剩余: {abnormal_remaining}")
    
    c.execute("SELECT COUNT(*) FROM trades WHERE (stop_loss IS NULL OR stop_loss = 0)")
    sl_remaining = c.fetchone()[0]
    print(f"  stop_loss 空/0 剩余: {sl_remaining}")
    
    c.execute("SELECT COUNT(*) FROM trades WHERE (fees IS NULL OR fees = 0) AND pnl IS NOT NULL")
    fees_remaining = c.fetchone()[0]
    print(f"  fees 空/0 剩余: {fees_remaining}")
    
    # 统计修复后的整体数据
    c.execute("""
        SELECT COUNT(*) as total, 
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
               SUM(pnl) as total_pnl
        FROM trades 
        WHERE quantity > 0 AND strategy NOT IN ('止损', '止盈') AND pnl IS NOT NULL
    """)
    stats = c.fetchone()
    total, wins, losses, total_pnl = stats
    win_rate = wins / total * 100 if total > 0 else 0
    print(f"\n  修复后统计: {total}笔交易, 胜率{win_rate:.1f}%, 总PnL={total_pnl:.2f}")
    
    # ================================================================
    # 提交事务
    # ================================================================
    db.commit()
    print("\n✅ 数据修复完成!")
    
    # 检查是否需要回滚
    if dup_remaining > 0 or abnormal_remaining > 5:
        print("\n⚠️ 警告: 仍有残留问题，请检查!")
        sys.exit(1)
    
    db.close()

if __name__ == '__main__':
    main()
