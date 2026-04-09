#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
账户选择路由器 (Account Router)

根据证券代码自动选择正确的交易账户

功能:
1. 港股正股/ETF/窝轮 → 正股账户 (18820895)
2. 港股期权 → 已禁用 (2026-04-02: 模拟账号不支持卖空操作)
3. 美股 → 美股账户 (待配置)

规则:
- 期权代码（包含 C/P + 6 位日期 + 行权价数字）→ 期权账户
- 正股/ETF/窝轮代码（HK. + 5 位数字）→ 正股账户
- 无法识别 → 抛出异常

示例:
    >>> select_account_for_code("HK.09992")
    '18820895'
    >>> select_account_for_code("HK.TCH260327C470000")
    '8246818'
    >>> select_account_for_code("HK.02800")  # ETF
    '18820895'
    >>> select_account_for_code("HK.02500")  # 窝轮
    '18820895'
"""
import re
import logging
from typing import Optional, Tuple, List, Dict, Any
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger("ftseek.account_router")
logger.propagate = False


# ============================================================
# 账户常量配置
# ============================================================

class AccountType(str, Enum):
    """账户类型枚举"""
    HK_STOCK = "hk_stock"       # 港股正股/ETF/窝轮
    HK_OPTION = "hk_option"     # 港股期权
    US_STOCK = "us_stock"       # 美股正股
    US_OPTION = "us_option"     # 美股期权


@dataclass
class AccountConfig:
    """账户配置"""
    account_id: str
    account_type: AccountType
    description: str
    trd_env: int = 1  # 1=模拟，0=真实


# 默认账户配置
DEFAULT_ACCOUNTS = {
    AccountType.HK_STOCK: AccountConfig(
        account_id="18820895",
        account_type=AccountType.HK_STOCK,
        description="港股正股/ETF/窝轮账户 (CASH)",
        trd_env=1,
    ),
    AccountType.HK_OPTION: AccountConfig(
        account_id="8246818",
        account_type=AccountType.HK_OPTION,
        description="港股期权账户 (MARGIN)",
        trd_env=1,
    ),
}


# ============================================================
# 账户选择核心逻辑
# ============================================================

def select_account_for_code(
    code: str,
    stock_account: str = "18820895",
    option_account: str = "8246818"
) -> str:
    """
    根据港股标的代码自动选择正确的账户
    
    Args:
        code: 交易标的代码（如 HK.09992, HK.TCH260327C470000）
        stock_account: 正股交易账户 ID（默认：18820895）
        option_account: 期权交易账户 ID（默认：8246818，已禁用）
    
    Returns:
        账户 ID 字符串
    
    规则:
        - 期权代码（包含 C/P + 6 位日期 + 行权价数字）→ 已禁用
        - 正股/ETF/窝轮代码（HK. + 5 位数字）→ 正股账户 18820895
        - 无法识别 → 抛出异常
    
    示例:
        >>> select_account_for_code("HK.09992")
        '18820895'
        >>> select_account_for_code("HK.TCH260327C470000")
        '8246818'
        >>> select_account_for_code("HK.02800")  # ETF
        '18820895'
        >>> select_account_for_code("HK.02500")  # 窝轮
        '18820895'
    """
    if not code or not isinstance(code, str):
        raise ValueError("代码必须是非空字符串")
    
    if '.' not in code:
        raise ValueError(f"代码格式错误：{code}，缺少市场前缀（如 HK.）")
    
    # 提取市场代码后的部分
    parts = code.split('.')
    if len(parts) < 2:
        raise ValueError(f"代码格式错误：{code}")
    
    symbol = parts[-1]
    
    # 港股期权正则：HK. + 字母 + 6 位日期 + C/P + 行权价数字
    # 示例：HK.TCH260327C470000, HK.AAPL260429C190000
    option_pattern = r'^[A-Z]+\d{6}[CP]\d+$'
    
    if re.match(option_pattern, symbol, re.IGNORECASE):
        # 港股期权交易已禁用 (2026-04-02: 模拟账号不支持卖空操作)
        logger.warning(f"[账户选择] {code} -> 期权交易已禁用，跳过")
        return None
    
    # 港股正股/ETF/窝轮正则：HK. + 5 位数字
    # 示例：HK.09992, HK.02800, HK.02500
    stock_pattern = r'^\d{5}$'
    
    if re.match(stock_pattern, symbol, re.IGNORECASE):
        logger.info(f"[账户选择] {code} -> 正股账户 {stock_account}")
        return stock_account
    
    # 无法识别的代码类型
    raise ValueError(
        f"无法识别的代码类型：{code} (symbol={symbol})。"
        f"支持的格式：正股 HK.XXXXX (5 位数字), 期权 HK.XXXYYYYYCZZZZZ"
    )


def is_option_code(code: str) -> bool:
    """
    判断是否为期权代码
    
    Args:
        code: 证券代码（如 HK.TCH260327C470000）
    
    Returns:
        True 表示是期权代码，False 表示不是
    
    期权代码特征:
    - HK.POP260429C190000 (泡泡玛特 Call 190)
    - HK.TCH260429C510000 (腾讯 Call 510)
    - 格式：市场代码 + 底层资产 + 到期日 (6 位) + C/P + 行权价
    
    正股代码特征:
    - HK.09992 (5 位数字)
    - HK.00700 (5 位数字)
    """
    if not code or '.' not in code:
        return False
    
    parts = code.split('.')
    if len(parts) < 2:
        return False
    
    symbol = parts[-1]
    
    # 期权特征：包含 6 位日期 + C/P
    if re.search(r'\d{6}[CP]', symbol, re.IGNORECASE):
        return True
    
    # 正股特征：纯数字，通常 5 位
    if re.match(r'^\d{5}$', symbol):
        return False
    
    # 默认：如果包含 C 或 P 且长度>10，认为是期权
    if ('C' in symbol.upper() or 'P' in symbol.upper()) and len(symbol) > 10:
        return True
    
    return False


def get_account_type(code: str) -> AccountType:
    """
    根据证券代码获取账户类型
    
    Args:
        code: 证券代码
    
    Returns:
        AccountType 枚举值
    """
    if not code:
        raise ValueError("代码不能为空")
    
    # 判断市场
    if code.startswith("US."):
        # 美股（暂不支持期权）
        return AccountType.US_STOCK
    elif code.startswith("HK."):
        # 港股
        if is_option_code(code):
            return AccountType.HK_OPTION
        else:
            return AccountType.HK_STOCK
    else:
        raise ValueError(f"不支持的市场代码：{code}")


def get_account_config(
    code: str,
    custom_accounts: Optional[dict] = None
) -> AccountConfig:
    """
    获取账户配置
    
    Args:
        code: 证券代码
        custom_accounts: 自定义账户配置（可选）
    
    Returns:
        AccountConfig 账户配置对象
    """
    # 使用自定义配置或默认配置
    accounts = custom_accounts if custom_accounts else DEFAULT_ACCOUNTS
    
    # 获取账户类型
    account_type = get_account_type(code)
    
    # 返回对应配置
    if account_type in accounts:
        return accounts[account_type]
    
    # 默认返回港股正股账户
    logger.warning(f"[账户配置] 未找到 {code} 的配置，使用默认港股正股账户")
    return accounts[AccountType.HK_STOCK]


# ============================================================
# 辅助函数
# ============================================================

def parse_option_code(code: str) -> Optional[dict]:
    """
    解析期权代码，提取详细信息
    
    Args:
        code: 期权代码（如 HK.TCH260327C470000）
    
    Returns:
        包含期权信息的字典，解析失败返回 None
    
    返回格式:
        {
            'market': 'HK',
            'underlying': 'TCH',
            'expiry': '260327',
            'type': 'C',  # C=Call, P=Put
            'strike': 470000,
            'strike_formatted': '470.00'
        }
    """
    if not is_option_code(code):
        return None
    
    parts = code.split('.')
    if len(parts) < 2:
        return None
    
    market = parts[0]
    symbol = parts[1]
    
    # 提取到期日和类型
    match = re.search(r'(\d{6})([CP])(\d+)', symbol, re.IGNORECASE)
    if not match:
        return None
    
    expiry = match.group(1)
    option_type = match.group(2).upper()
    strike = int(match.group(3))
    
    # 提取底层资产（到期日前的字母部分）
    underlying_match = re.match(r'^([A-Z]+)\d{6}', symbol, re.IGNORECASE)
    underlying = underlying_match.group(1) if underlying_match else ""
    
    # 格式化行权价（假设最后 2 位是小数）
    strike_formatted = f"{strike / 1000:.2f}"
    
    return {
        'market': market,
        'underlying': underlying,
        'expiry': expiry,
        'type': option_type,
        'strike': strike,
        'strike_formatted': strike_formatted,
    }


def validate_code_format(code: str) -> Tuple[bool, str]:
    """
    验证证券代码格式
    
    Args:
        code: 证券代码
    
    Returns:
        (是否有效，错误信息)
    """
    if not code:
        return False, "代码不能为空"
    
    if not isinstance(code, str):
        return False, "代码必须是字符串"
    
    if '.' not in code:
        return False, "代码格式错误，缺少市场前缀（如 HK. 或 US.）"
    
    parts = code.split('.')
    if len(parts) != 2:
        return False, "代码格式错误，应为 MARKET.SYMBOL 格式"
    
    market, symbol = parts
    
    # 验证市场
    if market not in ['HK', 'US']:
        return False, f"不支持的市场：{market}"
    
    # 验证股票代码格式
    if market == 'HK':
        # 港股：5 位数字 或 期权代码
        if not (re.match(r'^\d{5}$', symbol) or is_option_code(code)):
            return False, f"港股代码格式错误：{symbol}"
    elif market == 'US':
        # 美股：字母 + 数字组合
        if not re.match(r'^[A-Z0-9.]+$', symbol, re.IGNORECASE):
            return False, f"美股代码格式错误：{symbol}"
    
    return True, ""


# ============================================================
# 测试入口
# ============================================================

if __name__ == "__main__":
    # 设置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    )
    
    # 测试用例
    test_cases = [
        ("HK.09992", "18820895", "泡泡玛特正股"),
        ("HK.00700", "18820895", "腾讯控股正股"),
        ("HK.02800", "18820895", "盈富基金 ETF"),
        ("HK.02500", "18820895", "顺诚窝轮"),
        ("HK.TCH260327C470000", "8246818", "腾讯期权 Call"),
        ("HK.POP260429C190000", "8246818", "泡泡玛特期权 Call"),
        ("HK.AAPL260429C190000", "8246818", "苹果期权 Call"),
    ]
    
    print("=" * 60)
    print("账户选择路由器测试")
    print("=" * 60)
    
    passed = 0
    failed = 0
    
    for code, expected_account, description in test_cases:
        try:
            result = select_account_for_code(code)
            if result == expected_account:
                print(f"✅ PASS: {code} ({description}) -> {result}")
                passed += 1
            else:
                print(f"❌ FAIL: {code} ({description}) -> {result} (期望：{expected_account})")
                failed += 1
        except Exception as e:
            print(f"❌ ERROR: {code} ({description}) -> {e}")
            failed += 1
    
    print("=" * 60)
    print(f"测试结果：{passed} 通过，{failed} 失败")
    print("=" * 60)
