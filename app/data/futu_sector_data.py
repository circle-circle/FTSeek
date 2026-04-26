#!/usr/bin/env python3
"""
富途板块数据获取模块

功能:
- 板块资金流向
- 板块涨跌排名
- 热门概念板块
- 监控股票行情快照

用法:
    from app.data.futu_sector_data import FutuSectorData
    sector_data = FutuSectorData()
    flow = sector_data.get_sector_capital_flow()
    ranking = sector_data.get_sector_ranking(limit=10)
"""
import logging
import time
from typing import List, Dict, Optional
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("ftseek.data.sector_data")
logger.propagate = False


class FutuSectorData:
    """富途板块数据获取"""
    
    # 港股主要板块代码（富途板块编码）
    SECTOR_CODES = {
        '科技': 'HK.BK1001',
        '金融': 'HK.BK1002',
        '地产': 'HK.BK1003',
        '消费': 'HK.BK1004',
        '医药': 'HK.BK1005',
        '能源': 'HK.BK1006',
        '工业': 'HK.BK1007',
        '电信': 'HK.BK1008',
        '公用事业': 'HK.BK1009',
        '综合企业': 'HK.BK1010',
    }
    
    # 概念板块代码（示例）
    CONCEPT_CODES = {
        '新能源汽车': 'HK.BK2001',
        '半导体': 'HK.BK2002',
        '人工智能': 'HK.BK2003',
        '生物医药': 'HK.BK2004',
        '互联网': 'HK.BK2005',
        '光伏': 'HK.BK2006',
        '锂电池': 'HK.BK2007',
        '元宇宙': 'HK.BK2008',
    }
    
    def __init__(self, host: str = '127.0.0.1', port: int = 11111):
        """
        Args:
            host: OpenD主机
            port: OpenD端口
        """
        self.host = host
        self.port = port
        self.quote_ctx = None
        self._last_update = {}
        self._cache_ttl = 300  # 5分钟缓存
        
        logger.info("FutuSectorData 初始化完成")
    
    def _get_ctx(self):
        """获取或创建行情上下文"""
        if self.quote_ctx is None:
            try:
                from futu import OpenQuoteContext
                self.quote_ctx = OpenQuoteContext(host=self.host, port=self.port)
                logger.debug("富途行情连接已建立")
            except Exception as e:
                logger.error(f"富途行情连接失败: {e}")
                raise
        return self.quote_ctx
    
    def _check_cache(self, key: str) -> bool:
        """检查缓存是否有效"""
        if key in self._last_update:
            age = time.time() - self._last_update[key]
            return age < self._cache_ttl
        return False
    
    def get_sector_capital_flow(self) -> List[Dict]:
        """
        获取板块资金流向
        
        Returns:
            [
                {
                    'sector_name': '科技',
                    'sector_code': 'HK.BK1001',
                    'change_pct': 2.3,
                    'turnover': 152.5,  # 成交额（亿）
                    'advance_count': 35,  # 上涨家数
                    'decline_count': 15,  # 下跌家数
                    'top_stocks': [
                        {'code': 'HK.00700', 'name': '腾讯', 'change_pct': 3.1},
                        ...
                    ]
                }
            ]
        """
        cache_key = 'sector_capital_flow'
        if self._check_cache(cache_key):
            logger.debug("使用板块资金流向缓存")
            return self._cache_data.get(cache_key, [])
        
        result = []
        
        try:
            ctx = self._get_ctx()
            
            # 2026-04-20: 接入全局限流器
            from app.utils.futu_limiter import get_global_limiter
            limiter = get_global_limiter()
            
            for sector_name, sector_code in self.SECTOR_CODES.items():
                try:
                    # 获取板块下的股票列表
                    if not limiter.acquire(timeout=10):
                        logger.warning("[限流] 板块数据获取被拒绝")
                        break
                    ret, stock_list = ctx.get_plate_stock(sector_code)
                    limiter.record()
                    
                    if ret != 0 or stock_list is None or stock_list.empty:
                        logger.debug(f"板块 {sector_name} 无数据")
                        continue
                    
                    # 获取这些股票的行情快照
                    codes = stock_list['code'].tolist()[:50]  # 限制50只
                    if not limiter.acquire(timeout=10):
                        logger.warning("[限流] 板块快照被拒绝")
                        break
                    ret, snapshot = ctx.get_market_snapshot(codes)
                    limiter.record()
                    
                    if ret != 0 or snapshot is None or snapshot.empty:
                        continue
                    
                    # 计算板块统计
                    change_pcts = snapshot['change_percent'].tolist()
                    turnovers = snapshot['turnover'].tolist()
                    
                    avg_change = sum(change_pcts) / len(change_pcts) if change_pcts else 0
                    total_turnover = sum(turnovers) / 1e8  # 转换为亿
                    
                    advance_count = sum(1 for c in change_pcts if c > 0)
                    decline_count = sum(1 for c in change_pcts if c < 0)
                    
                    # 获取Top 3股票
                    snapshot_sorted = snapshot.sort_values('change_percent', ascending=False)
                    top_stocks = []
                    for _, row in snapshot_sorted.head(3).iterrows():
                        top_stocks.append({
                            'code': row['code'],
                            'name': row.get('stock_name', ''),
                            'change_pct': float(row['change_percent']),
                        })
                    
                    result.append({
                        'sector_name': sector_name,
                        'sector_code': sector_code,
                        'change_pct': round(avg_change, 2),
                        'turnover': round(total_turnover, 2),
                        'advance_count': advance_count,
                        'decline_count': decline_count,
                        'top_stocks': top_stocks,
                    })
                    
                except Exception as e:
                    logger.debug(f"获取板块 {sector_name} 数据失败: {e}")
                    continue
            
            # 按涨跌幅排序
            result.sort(key=lambda x: x['change_pct'], reverse=True)
            
            # 缓存
            self._cache_data[cache_key] = result
            self._last_update[cache_key] = time.time()
            
            logger.info(f"获取板块资金流向完成: {len(result)}个板块")
            return result
            
        except Exception as e:
            logger.error(f"获取板块资金流向失败: {e}")
            return []
    
    def get_sector_ranking(self, limit: int = 10) -> List[Dict]:
        """
        获取板块涨跌排名
        
        Args:
            limit: 返回数量
        
        Returns:
            按涨跌幅排序的板块列表
        """
        cache_key = f'sector_ranking_{limit}'
        if self._check_cache(cache_key):
            logger.debug("使用板块排名缓存")
            return self._cache_data.get(cache_key, [])
        
        # 获取所有板块数据
        all_sectors = self.get_sector_capital_flow()
        
        # 排序
        ranked = sorted(all_sectors, key=lambda x: x['change_pct'], reverse=True)
        
        # 限制数量
        result = ranked[:limit]
        
        # 缓存
        self._cache_data[cache_key] = result
        self._last_update[cache_key] = time.time()
        
        logger.info(f"获取板块排名完成: Top {len(result)}")
        return result
    
    def get_concept_hot(self) -> List[Dict]:
        """
        获取热门概念板块
        
        Returns:
            [
                {
                    'concept_name': '新能源汽车',
                    'concept_code': 'HK.BK2001',
                    'change_pct': 3.5,
                    'turnover': 85.2,
                    'stock_count': 25,
                }
            ]
        """
        cache_key = 'concept_hot'
        if self._check_cache(cache_key):
            logger.debug("使用概念板块缓存")
            return self._cache_data.get(cache_key, [])
        
        result = []
        
        try:
            ctx = self._get_ctx()
            from app.utils.futu_limiter import get_global_limiter
            limiter = get_global_limiter()
            
            for concept_name, concept_code in self.CONCEPT_CODES.items():
                try:
                    if not limiter.acquire(timeout=10):
                        logger.warning("[限流] 概念板块数据获取被拒绝")
                        break
                    ret, stock_list = ctx.get_plate_stock(concept_code)
                    limiter.record()
                    
                    if ret != 0 or stock_list is None or stock_list.empty:
                        continue
                    
                    stock_count = len(stock_list)
                    
                    # 获取行情快照
                    codes = stock_list['code'].tolist()[:50]
                    if not limiter.acquire(timeout=10):
                        logger.warning("[限流] 概念板块快照被拒绝")
                        break
                    ret, snapshot = ctx.get_market_snapshot(codes)
                    limiter.record()
                    
                    if ret != 0 or snapshot is None or snapshot.empty:
                        continue
                    
                    # 计算统计
                    change_pcts = snapshot['change_percent'].tolist()
                    turnovers = snapshot['turnover'].tolist()
                    
                    avg_change = sum(change_pcts) / len(change_pcts) if change_pcts else 0
                    total_turnover = sum(turnovers) / 1e8
                    
                    result.append({
                        'concept_name': concept_name,
                        'concept_code': concept_code,
                        'change_pct': round(avg_change, 2),
                        'turnover': round(total_turnover, 2),
                        'stock_count': stock_count,
                    })
                    
                except Exception as e:
                    logger.debug(f"获取概念板块 {concept_name} 失败: {e}")
                    continue
            
            # 按成交额排序（热度）
            result.sort(key=lambda x: x['turnover'], reverse=True)
            
            # 缓存
            self._cache_data[cache_key] = result
            self._last_update[cache_key] = time.time()
            
            logger.info(f"获取热门概念板块完成: {len(result)}个概念")
            return result
            
        except Exception as e:
            logger.error(f"获取热门概念板块失败: {e}")
            return []
    
    def get_watchlist_snapshot(self, codes: List[str]) -> List[Dict]:
        """
        获取监控股票行情快照
        
        Args:
            codes: 股票代码列表 ["HK.00700", "HK.09988", ...]
        
        Returns:
            [
                {
                    'code': 'HK.00700',
                    'name': '腾讯控股',
                    'price': 300.5,
                    'change_pct': 2.3,
                    'volume': 15000000,
                    'turnover': 4.5e9,
                    'high': 305.0,
                    'low': 298.0,
                    'open': 299.0,
                }
            ]
        """
        if not codes:
            return []
        
        cache_key = f'watchlist_{",".join(codes[:10])}'
        if self._check_cache(cache_key):
            logger.debug("使用监控股票快照缓存")
            return self._cache_data.get(cache_key, [])
        
        result = []
        
        try:
            ctx = self._get_ctx()
            from app.utils.futu_limiter import get_global_limiter
            limiter = get_global_limiter()
            
            if not limiter.acquire(timeout=10):
                logger.warning("[限流] 股票快照被拒绝")
                return []
            ret, snapshot = ctx.get_market_snapshot(codes)
            limiter.record()
            
            if ret != 0 or snapshot is None or snapshot.empty:
                logger.warning("获取股票快照失败")
                return []
            
            for _, row in snapshot.iterrows():
                result.append({
                    'code': row['code'],
                    'name': row.get('stock_name', ''),
                    'price': float(row.get('last_price', 0)),
                    'change_pct': float(row.get('change_percent', 0)),
                    'volume': float(row.get('volume', 0)),
                    'turnover': float(row.get('turnover', 0)),
                    'high': float(row.get('high', 0)),
                    'low': float(row.get('low', 0)),
                    'open': float(row.get('open', 0)),
                })
            
            # 缓存
            self._cache_data[cache_key] = result
            self._last_update[cache_key] = time.time()
            
            logger.info(f"获取监控股票快照完成: {len(result)}只")
            return result
            
        except Exception as e:
            logger.error(f"获取监控股票快照失败: {e}")
            return []
    
    def close(self):
        """关闭连接"""
        if self.quote_ctx:
            try:
                self.quote_ctx.close()
                logger.info("富途行情连接已关闭")
            except Exception as e:
                logger.error(f"关闭连接失败: {e}")
            finally:
                self.quote_ctx = None


# 单例模式
_instance = None

def get_sector_data() -> FutuSectorData:
    """获取FutuSectorData单例"""
    global _instance
    if _instance is None:
        _instance = FutuSectorData()
    return _instance


if __name__ == '__main__':
    # 测试
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    sector_data = get_sector_data()
    
    print("\n" + "="*80)
    print("富途板块数据测试")
    print("="*80)
    
    try:
        # 测试板块资金流向
        print("\n【1】板块资金流向")
        flow = sector_data.get_sector_capital_flow()
        for sector in flow[:5]:
            print(f"  {sector['sector_name']}: {sector['change_pct']:+.2f}% "
                  f"成交额{sector['turnover']}亿 "
                  f"涨{sector['advance_count']}/跌{sector['decline_count']}")
        
        # 测试板块排名
        print("\n【2】板块涨跌排名 Top 5")
        ranking = sector_data.get_sector_ranking(limit=5)
        for i, sector in enumerate(ranking, 1):
            print(f"  {i}. {sector['sector_name']}: {sector['change_pct']:+.2f}%")
        
        # 测试概念板块
        print("\n【3】热门概念板块")
        concepts = sector_data.get_concept_hot()
        for concept in concepts[:5]:
            print(f"  {concept['concept_name']}: {concept['change_pct']:+.2f}% "
                  f"成交额{concept['turnover']}亿")
        
        # 测试监控股票
        print("\n【4】监控股票快照")
        codes = ['HK.00700', 'HK.09988', 'HK.03690']
        snapshot = sector_data.get_watchlist_snapshot(codes)
        for stock in snapshot:
            print(f"  {stock['code']} {stock['name']}: {stock['price']} "
                  f"({stock['change_pct']:+.2f}%)")
        
    except Exception as e:
        print(f"\n测试失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        sector_data.close()
