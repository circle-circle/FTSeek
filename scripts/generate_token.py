#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成安全的 API Token

使用方法:
    python scripts/generate_token.py
"""
import secrets
import sys

def generate_token(length: int = 64) -> str:
    """生成安全的随机 token"""
    return secrets.token_hex(length // 2)

def main():
    print("=" * 60)
    print("FTSeek 安全 Token 生成器")
    print("=" * 60)
    print()

    # 生成 API Token
    api_token = generate_token(64)
    print(f"API_TOKEN={api_token}")
    print()

    # 生成 TradingView Webhook Token
    tv_token = generate_token(64)
    print(f"TRADINGVIEW_WEBHOOK_TOKEN={tv_token}")
    print()

    # 生成临时密码（如果需要）
    temp_pwd = generate_token(32)
    print(f"临时密码 (MD5 输入)={temp_pwd}")
    print()

    print("=" * 60)
    print("使用说明:")
    print("=" * 60)
    print("1. 将上面的值复制到 .env 文件")
    print("2. 或者执行以下命令直接设置:")
    print()
    print(f"   export API_TOKEN={api_token}")
    print(f"   export TRADINGVIEW_WEBHOOK_TOKEN={tv_token}")
    print()
    print("安全提示:")
    print("- 不要将 token 提交到版本控制")
    print("- 定期更换 token")
    print("- 生产环境使用不同的 token")
    print("=" * 60)

if __name__ == '__main__':
    main()
