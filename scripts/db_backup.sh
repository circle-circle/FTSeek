#!/bin/bash
# FTSeek-US 每日数据库备份
# 备份所有活跃 SQLite DB 到 data/backups/YYYY-MM-DD/
set -e

PROJECT_DIR="/opt/ftseek/FTSeek-US"
DATA_DIR="$PROJECT_DIR/data"
BACKUP_DIR="$DATA_DIR/backups/$(date +%Y-%m-%d)"
LOG_FILE="$PROJECT_DIR/logs/db_backup.log"

mkdir -p "$BACKUP_DIR"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting DB backup → $BACKUP_DIR" >> "$LOG_FILE"

# 备份所有非空 .db 文件
for db in "$DATA_DIR"/*.db; do
    [ -f "$db" ] || continue
    [ -s "$db" ] || continue  # 跳过空文件
    
    name=$(basename "$db")
    target="$BACKUP_DIR/$name"
    
    # 使用 sqlite3 .backup 确保一致性 (WAL-safe)
    if command -v sqlite3 >/dev/null 2>&1; then
        sqlite3 "$db" ".backup '$target'" 2>>"$LOG_FILE"
    else
        cp "$db" "$target"
    fi
    
    size=$(stat -c%s "$target" 2>/dev/null || echo 0)
    echo "  ✅ $name → $(numfmt --to=iec $size)" >> "$LOG_FILE"
done

# 清理 7 天前的备份
find "$DATA_DIR/backups/" -maxdepth 1 -type d -mtime +7 -exec rm -rf {} + 2>/dev/null

# 统计
total_size=$(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Backup complete: $total_size" >> "$LOG_FILE"
echo "---" >> "$LOG_FILE"
