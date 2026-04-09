#!/usr/bin/env python3
"""
钉钉推送测试脚本

测试钉钉推送是否可用
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.notifier.dingtalk import DingTalkNotifier
from app import config

def test_dingtalk_push():
    """测试钉钉推送"""
    print("="*70)
    print("钉钉推送测试")
    print("="*70)
    
    # 检查配置
    print(f"\n📋 配置检查:")
    print(f"   DINGTALK_WEBHOOK_URL: {'✅ 已配置' if config.DINGTALK_WEBHOOK_URL else '❌ 未配置'}")
    print(f"   DINGTALK_SECRET: {'✅ 已配置' if config.DINGTALK_SECRET else '❌ 未配置'}")
    
    # 创建推送器
    notifier = DingTalkNotifier()
    print(f"\n✅ 推送器已创建")
    print(f"   Webhook: {notifier.webhook_url[:50]}...")
    print(f"   Secret: {'已配置' if notifier.secret else '未配置'}")
    print(f"   限流：{notifier.MAX_PER_MINUTE} 条/分钟")
    
    # 测试推送 - 不使用@全体成员
    print(f"\n🚀 发送测试消息...")
    try:
        result = notifier.send_text(
            content="🧪 FTSeek 推送测试\n\n这是一条测试消息，用于验证钉钉推送功能是否正常。\n\n时间：" + __import__('datetime').datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            at_all=False  # 不@全体成员
        )
        
        if result:
            print(f"✅ 推送成功！")
            print(f"   请检查钉钉群消息")
            return True
        else:
            print(f"❌ 推送失败！")
            print(f"   可能原因：限流或网络问题")
            return False
    except Exception as e:
        print(f"❌ 推送异常！")
        print(f"   错误：{e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = test_dingtalk_push()
    sys.exit(0 if success else 1)
