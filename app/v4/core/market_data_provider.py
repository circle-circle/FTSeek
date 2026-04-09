"""
Futu OpenD 市场数据提供者

提供从 Futu OpenD 获取历史 K 线数据的函数，用于技术指标计算。
"""
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger("ftseek.v4.core.market_data")
logger.propagate = False


def futu_market_data_provider(code: str, period: int = 50) -> List[Dict[str, Any]]:
    """
    从 Futu OpenD 获取历史 K 线数据

    通过 Futu OpenD API 获取指定股票的历史 K 线数据，用于 RSI/MACD 等技术指标计算。

    Args:
        code: 股票代码 (格式：HK.00700, US.AAPL, 或 0700.HK, AAPL.US)
        period: 需要获取的 K 线数量 (默认 50，用于计算 MACD 需要至少 35 根)

    Returns:
        List[Dict[str, Any]]: K 线数据列表，按时间正序排列
            每个元素包含：
            - open: 开盘价
            - high: 最高价
            - low: 最低价
            - close: 收盘价
            - volume: 成交量
            - turnover: 成交额
            - timestamp: 时间戳

    Raises:
        Exception: 获取数据失败时抛出异常

    Example:
        >>> klines = futu_market_data_provider("HK.00700", period=50)
        >>> closes = [k['close'] for k in klines]
        >>> rsi_val = rsi(closes, period=14)
    """
    from app import config
    from futu import OpenQuoteContext, KLType, RET_OK, AuType

    # 标准化股票代码格式
    # 支持格式：HK.00700, US.AAPL -> 00700.HK, AAPL.US
    code_standard = _standardize_code(code)

    quote_ctx = None
    try:
        # 创建行情上下文
        quote_ctx = OpenQuoteContext(host=config.OPEND_HOST, port=config.OPEND_PORT)

        # 获取日 K 线数据
        ret, data = quote_ctx.get_cur_kline(
            code_standard,
            num=period,
            ktype=KLType.K_DAY,
            autype=AuType.QFQ,  # 前复权
        )

        if ret != RET_OK:
            logger.warning(f"获取 K 线失败：code={code_standard}, error={data}")
            return []

        # 转换为字典列表
        klines = []
        for _, row in data.iterrows():
            kline = {
                'open': float(row.get('open', 0)),
                'high': float(row.get('high', 0)),
                'low': float(row.get('low', 0)),
                'close': float(row.get('close', 0)),
                'volume': int(row.get('volume', 0)),
                'turnover': float(row.get('turnover', 0)),
                'timestamp': int(row.get('time', 0)),
            }
            klines.append(kline)

        logger.debug(f"获取 K 线成功：code={code_standard}, count={len(klines)}")
        return klines

    except Exception as e:
        logger.exception(f"获取 K 线数据异常：code={code_standard}, error={e}")
        return []

    finally:
        # 关闭行情上下文
        if quote_ctx is not None:
            try:
                quote_ctx.close()
            except Exception:
                pass


def _standardize_code(code: str) -> str:
    """
    标准化股票代码格式

    将各种格式的股票代码转换为 Futu API 要求的格式：
    - HK.00700 -> HK.00700 (Futu 要求格式)
    - US.AAPL -> US.AAPL (Futu 要求格式)
    - 0700.HK -> HK.00700
    - AAPL.US -> US.AAPL
    - SH.600000 -> SH.600000
    - SZ.000001 -> SZ.000001

    Args:
        code: 股票代码

    Returns:
        str: 标准化后的股票代码 (Futu 格式：市场。代码)
    """
    code = code.strip().upper()

    # 已经是 Futu 标准格式 (HK.XXXXX 或 US.XXXXX 或 SH.XXXXX 或 SZ.XXXXX)
    if code.startswith('HK.') or code.startswith('US.') or code.startswith('SH.') or code.startswith('SZ.'):
        return code

    # 处理 XXXX.HK 格式 -> HK.XXXX
    if code.endswith('.HK') and '.' in code:
        parts = code.split('.')
        return f"HK.{parts[0]}"

    # 处理 XXXX.US 格式 -> US.XXXX
    if code.endswith('.US') and '.' in code:
        parts = code.split('.')
        return f"US.{parts[0]}"

    # 其他格式，尝试原样返回（让 Futu API 处理）
    return code


def get_kline_data(
    code: str,
    period: int = 50,
    ktype: str = "K_DAY",
    auto_retry: bool = True,
    max_retries: int = 3
) -> Optional[List[Dict[str, Any]]]:
    """
    获取 K 线数据 (带重试机制)

    增强版的 K 线获取函数，支持自动重试和多种 K 线类型。

    Args:
        code: 股票代码
        period: K 线数量
        ktype: K 线类型 (K_DAY, K_1M, K_5M, K_15M, K_30M, K_60M, K_WEEK, K_MONTH)
        auto_retry: 是否自动重试
        max_retries: 最大重试次数

    Returns:
        List[Dict[str, Any]]: K 线数据列表，失败返回 None

    Example:
        >>> # 获取日 K 线
        >>> klines = get_kline_data("HK.00700", period=100, ktype="K_DAY")
        >>>
        >>> # 获取 60 分钟 K 线
        >>> klines_60m = get_kline_data("HK.00700", period=100, ktype="K_60M")
    """
    from app import config
    from futu import OpenQuoteContext, KLType, RET_OK, AuType

    # K 线类型映射
    ktype_map = {
        "K_DAY": KLType.K_DAY,
        "K_1M": KLType.K_1M,
        "K_5M": KLType.K_5M,
        "K_15M": KLType.K_15M,
        "K_30M": KLType.K_30M,
        "K_60M": KLType.K_60M,
        "K_WEEK": KLType.K_WEEK,
        "K_MONTH": KLType.K_MONTH,
    }

    kl_type = ktype_map.get(ktype, KLType.K_DAY)
    code_standard = _standardize_code(code)

    quote_ctx = None
    last_error = None

    for attempt in range(max_retries if auto_retry else 1):
        try:
            quote_ctx = OpenQuoteContext(host=config.OPEND_HOST, port=config.OPEND_PORT)
            ret, data = quote_ctx.get_cur_kline(
                code_standard,
                num=period,
                ktype=kl_type,
                autype=AuType.QFQ,  # 前复权
            )

            if ret == RET_OK:
                klines = []
                for _, row in data.iterrows():
                    kline = {
                        'open': float(row.get('open', 0)),
                        'high': float(row.get('high', 0)),
                        'low': float(row.get('low', 0)),
                        'close': float(row.get('close', 0)),
                        'volume': int(row.get('volume', 0)),
                        'turnover': float(row.get('turnover', 0)),
                        'timestamp': int(row.get('time', 0)),
                    }
                    klines.append(kline)

                logger.debug(f"获取 K 线成功：code={code_standard}, type={ktype}, count={len(klines)}")
                return klines
            else:
                last_error = f"API 返回错误：{data}"
                logger.warning(f"获取 K 线失败 (尝试 {attempt+1}/{max_retries}): {last_error}")

        except Exception as e:
            last_error = str(e)
            logger.exception(f"获取 K 线异常 (尝试 {attempt+1}/{max_retries}): {e}")

        finally:
            if quote_ctx is not None:
                try:
                    quote_ctx.close()
                except Exception:
                    pass

        # 重试前等待
        if auto_retry and attempt < max_retries - 1:
            import time
            time.sleep(1 * (attempt + 1))  # 指数退避

    logger.error(f"获取 K 线最终失败：code={code_standard}, error={last_error}")
    return None


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(level=logging.DEBUG)

    print("测试 Futu OpenD 市场数据提供者...")

    # 测试获取腾讯控股 (HK.00700) K 线
    klines = futu_market_data_provider("HK.00700", period=50)
    print(f"\n获取 HK.00700 K 线：{len(klines)} 条")

    if klines:
        print(f"最新收盘价：{klines[-1]['close']:.2f}")
        print(f"最早日期：{klines[0].get('timestamp')}")
        print(f"最新日期：{klines[-1].get('timestamp')}")

        # 测试指标计算
        from app.strategy.indicators import rsi, macd
        closes = [k['close'] for k in klines]

        rsi_val = rsi(closes, period=14)
        macd_result = macd(closes, fast=12, slow=26, signal=9)

        print(f"\n技术指标:")
        print(f"  RSI(14): {rsi_val:.2f}" if rsi_val else "  RSI(14): None")
        if macd_result:
            print(f"  MACD: line={macd_result[0]:.4f}, signal={macd_result[1]:.4f}, hist={macd_result[2]:.4f}")
        else:
            print("  MACD: None")
