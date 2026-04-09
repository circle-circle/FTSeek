#!/usr/bin/env python3
"""
下载富途历史 K 线数据用于回测

功能:
1. 下载恒生指数成分股历史 K 线数据 (日线 +60 分钟线)
2. 时间范围：2023-01-01 至 2026-03-31
3. 数据字段：开盘/最高/最低/收盘/成交量
4. 保存到 /opt/ftseek/FTSeek/data/historical_data/

使用方法:
    python3 scripts/download_historical_data.py
"""

import sys
import os
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional
import pandas as pd

# 设置路径
sys.path.insert(0, '/opt/ftseek/FTSeek')
os.environ["FUTU_LOG_LEVEL"] = "ERROR"

from futu import OpenQuoteContext, KLType, SubType, RET_OK, RET_ERROR

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/opt/ftseek/FTSeek/logs/download_historical.log')
    ]
)
logger = logging.getLogger("ftseek.download")

# 恒生指数成分股列表 (至少 20 只)
HSI_STOCKS = [
    # 科技互联网
    {"code": "HK.00700", "name": "腾讯控股"},
    {"code": "HK.09988", "name": "阿里巴巴"},
    {"code": "HK.09992", "name": "泡泡玛特"},
    {"code": "HK.03690", "name": "美团-W"},
    {"code": "HK.00981", "name": "中芯国际"},
    {"code": "HK.01810", "name": "小米集团"},
    {"code": "HK.01024", "name": "快手-W"},
    {"code": "HK.09618", "name": "京东健康"},
    
    # 金融
    {"code": "HK.00005", "name": "汇丰控股"},
    {"code": "HK.00003", "name": "香港中华煤气"},
    {"code": "HK.00001", "name": "长和"},
    {"code": "HK.00002", "name": "中电控股"},
    {"code": "HK.00006", "name": "电能实业"},
    {"code": "HK.00011", "name": "恒生银行"},
    {"code": "HK.00012", "name": "恒基地产"},
    {"code": "HK.00016", "name": "新鸿基地产"},
    
    # 消费
    {"code": "HK.00175", "name": "吉利汽车"},
    {"code": "HK.00669", "name": "创科实业"},
    {"code": "HK.00883", "name": "中国海洋石油"},
    {"code": "HK.00941", "name": "中国移动"},
    {"code": "HK.01928", "name": "金沙中国"},
    {"code": "HK.00027", "name": "银河娱乐"},
    
    # 医药
    {"code": "HK.00267", "name": "中信股份"},
    {"code": "HK.00388", "name": "香港交易所"},
    {"code": "HK.00857", "name": "中国石油股份"},
    {"code": "HK.00386", "name": "中国石油化工"},
]

# 日期范围
START_DATE = "2023-01-01"
END_DATE = "2026-03-31"

# 输出目录
OUTPUT_DIR = Path("/opt/ftseek/FTSeek/data/historical_data")


def download_klines(quote_ctx, code: str, name: str, kl_type: KLType, 
                    ktype_str: str) -> Optional[pd.DataFrame]:
    """
    下载单只股票的历史 K 线数据
    
    Args:
        quote_ctx: 富途报价上下文
        code: 股票代码
        name: 股票名称
        kl_type: K 线类型枚举
        ktype_str: K 线类型字符串 (用于文件名)
    
    Returns:
        DataFrame: K 线数据，失败返回 None
    """
    logger.info(f"  下载 {code} ({name}) {ktype_str} K 线...")
    
    all_data = []
    page_req_key = None
    max_retries = 3
    
    # 富途 API 每次最多返回 1000 条 K 线，需要分页获取
    for attempt in range(max_retries):
        try:
            ret, data, page_req_key = quote_ctx.request_history_kline(
                code=code,
                ktype=kl_type,
                autype='qfq',  # 前复权
                start=START_DATE,
                end=END_DATE,
                max_count=1000,
                page_req_key=page_req_key
            )
            
            if ret == RET_OK and data is not None and not data.empty:
                all_data.append(data)
                
                # 如果有更多数据，继续获取
                if page_req_key is not None:
                    logger.debug(f"    已获取 {len(data)} 条，继续获取下一页...")
                    continue
                else:
                    break
            else:
                logger.warning(f"    获取 K 线失败：{data}")
                if attempt < max_retries - 1:
                    logger.info(f"    重试 ({attempt + 1}/{max_retries})...")
                    import time
                    time.sleep(2)
                else:
                    return None
                    
        except Exception as e:
            logger.error(f"    获取 K 线异常：{e}")
            if attempt < max_retries - 1:
                logger.info(f"    重试 ({attempt + 1}/{max_retries})...")
                import time
                time.sleep(2)
            else:
                return None
    
    if not all_data:
        return None
    
    # 合并所有数据
    df = pd.concat(all_data, ignore_index=True)
    
    # 去重
    df = df.drop_duplicates(subset=['time_key'], keep='first')
    
    # 排序
    df = df.sort_values('time_key').reset_index(drop=True)
    
    logger.info(f"    ✓ 成功下载 {len(df)} 条 K 线")
    
    return df


def save_klines(df: pd.DataFrame, code: str, ktype_str: str):
    """保存 K 线数据到 CSV"""
    output_file = OUTPUT_DIR / f"{code.replace('.', '_')}_{ktype_str}.csv"
    
    # 选择需要的列
    columns = ['time_key', 'open', 'close', 'high', 'low', 'volume', 'turnover']
    df_save = df[columns].copy()
    
    # 保存到 CSV
    df_save.to_csv(output_file, index=False)
    
    logger.info(f"    保存到：{output_file}")


def validate_data(df: pd.DataFrame, code: str, ktype_str: str) -> Dict:
    """
    验证数据完整性
    
    Returns:
        验证报告字典
    """
    report = {
        'code': code,
        'ktype': ktype_str,
        'total_records': len(df),
        'date_range': {'start': '', 'end': ''},
        'missing_days': 0,
        'price_anomalies': 0,
        'valid': True,
        'issues': []
    }
    
    if df.empty:
        report['valid'] = False
        report['issues'].append("数据为空")
        return report
    
    # 检查日期范围
    report['date_range']['start'] = df['time_key'].min()
    report['date_range']['end'] = df['time_key'].max()
    
    # 检查价格异常 (涨跌停超过 30%)
    df_sorted = df.sort_values('time_key')
    price_changes = df_sorted['close'].pct_change().abs()
    anomalies = (price_changes > 0.30).sum()
    report['price_anomalies'] = int(anomalies)
    
    if anomalies > 0:
        report['issues'].append(f"发现 {anomalies} 次价格异常波动 (>30%)")
    
    # 检查数据连续性 (仅日线)
    if ktype_str == 'DAY':
        dates = pd.to_datetime(df_sorted['time_key'])
        expected_days = len(pd.date_range(start=dates.min(), end=dates.max(), freq='B'))  # 工作日
        actual_days = len(dates)
        missing = expected_days - actual_days
        
        if missing > expected_days * 0.1:  # 允许 10% 的缺失 (节假日)
            report['missing_days'] = missing
            report['issues'].append(f"缺失 {missing} 个交易日")
    
    # 检查成交量异常
    if 'volume' in df.columns:
        zero_volume = (df_sorted['volume'] == 0).sum()
        if zero_volume > 0:
            report['issues'].append(f"发现 {zero_volume} 条零成交量记录")
    
    # 判断是否有效
    report['valid'] = len(report['issues']) == 0 or len(report['issues']) <= 2
    
    return report


def main():
    """主函数"""
    logger.info("=" * 80)
    logger.info("富途历史 K 线数据下载")
    logger.info("=" * 80)
    logger.info(f"时间范围：{START_DATE} 至 {END_DATE}")
    logger.info(f"股票数量：{len(HSI_STOCKS)}")
    logger.info(f"输出目录：{OUTPUT_DIR}")
    logger.info("")
    
    # 创建输出目录
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    # 连接富途 API
    logger.info("连接富途 OpenD...")
    quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    
    # 测试连接
    ret, data = quote_ctx.get_market_snapshot('HK.00700')
    if ret != RET_OK:
        logger.error(f"连接富途 API 失败：{data}")
        logger.error("请确保 OpenD 已启动 (端口 11111)")
        quote_ctx.close()
        sys.exit(1)
    
    logger.info("✓ 连接成功")
    logger.info("")
    
    # 下载统计
    stats = {
        'total_stocks': len(HSI_STOCKS),
        'success_day': 0,
        'success_60m': 0,
        'failed_day': [],
        'failed_60m': [],
        'validation': []
    }
    
    # 遍历所有股票
    for stock in HSI_STOCKS:
        code = stock['code']
        name = stock['name']
        
        logger.info(f"[{HSI_STOCKS.index(stock) + 1}/{len(HSI_STOCKS)}] {code} {name}")
        
        # 下载日线数据
        df_day = download_klines(quote_ctx, code, name, KLType.K_DAY, 'DAY')
        if df_day is not None:
            save_klines(df_day, code, 'DAY')
            stats['success_day'] += 1
            
            # 验证数据
            report_day = validate_data(df_day, code, 'DAY')
            stats['validation'].append(report_day)
        else:
            stats['failed_day'].append(code)
            logger.warning(f"  ✗ 日线数据下载失败")
        
        # 下载 60 分钟线数据
        df_60m = download_klines(quote_ctx, code, name, KLType.K_60M, '60M')
        if df_60m is not None:
            save_klines(df_60m, code, '60M')
            stats['success_60m'] += 1
            
            # 验证数据
            report_60m = validate_data(df_60m, code, '60M')
            stats['validation'].append(report_60m)
        else:
            stats['failed_60m'].append(code)
            logger.warning(f"  ✗ 60 分钟线数据下载失败")
        
        logger.info("")
    
    # 关闭连接
    quote_ctx.close()
    
    # 生成统计报告
    logger.info("=" * 80)
    logger.info("下载统计报告")
    logger.info("=" * 80)
    logger.info(f"日线数据：成功 {stats['success_day']}/{stats['total_stocks']}")
    logger.info(f"60 分钟线：成功 {stats['success_60m']}/{stats['total_stocks']}")
    
    if stats['failed_day']:
        logger.warning(f"日线失败：{stats['failed_day']}")
    if stats['failed_60m']:
        logger.warning(f"60 分钟线失败：{stats['failed_60m']}")
    
    # 保存验证报告
    validation_report = {
        'download_time': datetime.now().isoformat(),
        'date_range': {'start': START_DATE, 'end': END_DATE},
        'total_stocks': len(HSI_STOCKS),
        'success_day': stats['success_day'],
        'success_60m': stats['success_60m'],
        'validation_details': stats['validation']
    }
    
    report_file = OUTPUT_DIR / "validation_report.json"
    with open(report_file, 'w', encoding='utf-8') as f:
        json.dump(validation_report, f, ensure_ascii=False, indent=2)
    
    logger.info(f"验证报告已保存：{report_file}")
    logger.info("")
    
    # 判断是否成功
    success = stats['success_day'] >= 20 and stats['success_60m'] >= 20
    if success:
        logger.info("✅ 数据下载完成！满足回测要求 (20+ 股票)")
    else:
        logger.warning("⚠️ 数据下载不完整，可能影响回测结果")
    
    logger.info("=" * 80)
    
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
