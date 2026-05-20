#!/usr/bin/env python3
"""
数据库归档脚本 — P2 2026-05-20

职责:
  1. 归档超过 30 天的 signals / signal_outcomes / events 记录
  2. 压缩归档文件
  3. 保留最近 7 天的完整数据

使用:
  python scripts/archive_database.py [--days 30] [--dry-run]
"""

from __future__ import annotations

import argparse
import gzip
import logging
import os
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

# 默认路径
DEFAULT_DB_PATH = Path("/opt/ftseek/FTSeek-US/data/ftseek_us.db")
DEFAULT_ARCHIVE_DIR = Path("/opt/ftseek/FTSeek-US/data/archives")

# 归档表配置
ARCHIVE_TABLES = [
    {
        "name": "signals",
        "date_column": "signal_date",
        "date_format": "%Y-%m-%d %H:%M:%S",
    },
    {
        "name": "signal_outcomes",
        "date_column": "timestamp",
        "date_format": "%Y-%m-%d %H:%M:%S",
    },
    {
        "name": "events",
        "date_column": "timestamp",
        "date_format": "%Y-%m-%dT%H:%M:%S",
    },
]


def archive_table(
    db_path: Path,
    table_config: dict,
    cutoff_date: str,
    archive_dir: Path,
    dry_run: bool = False,
) -> int:
    """
    归档指定表的数据

    Args:
        db_path: 数据库路径
        table_config: 表配置 (name, date_column, date_format)
        cutoff_date: 截止日期 (YYYY-MM-DD)
        archive_dir: 归档目录
        dry_run: 是否模拟运行

    Returns:
        归档的记录数
    """
    table_name = table_config["name"]
    date_col = table_config["date_column"]

    logger.info(f"[归档] 表={table_name} 截止={cutoff_date}")

    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        # 查询待归档记录数
        cursor.execute(f"SELECT COUNT(*) FROM {table_name} WHERE {date_col} < ?", (cutoff_date,))
        count = cursor.fetchone()[0]
        logger.info(f"[归档] {table_name}: 找到 {count} 条待归档记录")

        if count == 0:
            conn.close()
            return 0

        if dry_run:
            logger.info(f"[归档·模拟] {table_name}: 将归档 {count} 条记录")
            conn.close()
            return count

        # 导出到 CSV
        archive_file = archive_dir / f"{table_name}_{cutoff_date}.sql"
        with open(archive_file, 'w') as f:
            f.write(f"-- Archive: {table_name} < {cutoff_date}\n")
            f.write(f"-- Generated: {datetime.now().isoformat()}\n\n")

            # 导出 INSERT 语句
            cursor.execute(f"SELECT * FROM {table_name} WHERE {date_col} < ?", (cutoff_date,))
            columns = [desc[0] for desc in cursor.description]

            for row in cursor:
                values = ", ".join(f"'{v}'" if isinstance(v, str) else str(v) for v in row)
                f.write(f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({values});\n")

        logger.info(f"[归档] {table_name}: 导出到 {archive_file} ({archive_file.stat().st_size} bytes)")

        # 压缩
        gz_file = archive_file.with_suffix('.sql.gz')
        with open(archive_file, 'rb') as f_in:
            with gzip.open(gz_file, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
        os.remove(archive_file)
        logger.info(f"[归档] {table_name}: 压缩到 {gz_file} ({gz_file.stat().st_size} bytes)")

        # 删除已归档记录
        cursor.execute(f"DELETE FROM {table_name} WHERE {date_col} < ?", (cutoff_date,))
        conn.commit()
        deleted = cursor.rowcount
        logger.info(f"[归档] {table_name}: 删除 {deleted} 条已归档记录")

        conn.close()
        return count

    except Exception as e:
        logger.error(f"[归档] {table_name} 失败: {e}")
        return 0


def main():
    parser = argparse.ArgumentParser(description="FTSeek-US 数据库归档脚本")
    parser.add_argument("--db", type=str, default=str(DEFAULT_DB_PATH), help="数据库路径")
    parser.add_argument("--archive-dir", type=str, default=str(DEFAULT_ARCHIVE_DIR), help="归档目录")
    parser.add_argument("--days", type=int, default=30, help="归档天数阈值")
    parser.add_argument("--dry-run", action="store_true", help="模拟运行")
    args = parser.parse_args()

    db_path = Path(args.db)
    archive_dir = Path(args.archive_dir)
    cutoff_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")

    logger.info(f"=== FTSeek-US 数据库归档 ===")
    logger.info(f"数据库: {db_path}")
    logger.info(f"归档目录: {archive_dir}")
    logger.info(f"截止日期: {cutoff_date} ({args.days} 天前)")
    logger.info(f"模拟运行: {args.dry_run}")

    if not db_path.exists():
        logger.error(f"数据库不存在: {db_path}")
        return 1

    # 创建归档目录
    archive_dir.mkdir(parents=True, exist_ok=True)

    # 归档各表
    total_archived = 0
    for table_config in ARCHIVE_TABLES:
        count = archive_table(db_path, table_config, cutoff_date, archive_dir, args.dry_run)
        total_archived += count

    logger.info(f"=== 归档完成: 总计 {total_archived} 条记录 ===")

    # VACUUM 优化
    if not args.dry_run and total_archived > 0:
        logger.info("[优化] 执行 VACUUM 压缩数据库...")
        try:
            conn = sqlite3.connect(str(db_path))
            conn.execute("VACUUM")
            conn.close()
            logger.info(f"[优化] VACUUM 完成，数据库大小: {db_path.stat().st_size} bytes")
        except Exception as e:
            logger.error(f"[优化] VACUUM 失败: {e}")

    return 0


if __name__ == "__main__":
    exit(main())
