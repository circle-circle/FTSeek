"""
OpenD 配置读取模块
从 FutuOpenD.xml 安全读取配置，不存储明文密码
"""
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, Tuple

OPEND_CONFIG_PATH = Path("/opt/ftseek/Futu_OpenD_10.1.6108_Centos7/Futu_OpenD_10.1.6108_Centos7/FutuOpenD.xml")

def get_opend_config() -> Tuple[str, int, Optional[str], Optional[str]]:
    """
    从 FutuOpenD.xml 读取配置
    
    Returns:
        (host, port, login_account, login_pwd_md5, trade_pwd_md5)
    """
    if not OPEND_CONFIG_PATH.exists():
        raise FileNotFoundError(f"OpenD 配置文件不存在：{OPEND_CONFIG_PATH}")
    
    tree = ET.parse(OPEND_CONFIG_PATH)
    root = tree.getroot()
    
    # 读取基础配置
    ip = root.findtext('ip', '127.0.0.1')
    port = int(root.findtext('api_port', '11111'))
    login_account = root.findtext('login_account', '')
    
    # 读取密码 (优先使用 MD5 加密)
    login_pwd_md5 = root.findtext('login_pwd_md5')
    trade_pwd_md5 = root.findtext('trade_pwd')
    
    return ip, port, login_account, login_pwd_md5, trade_pwd_md5

if __name__ == "__main__":
    """测试配置读取"""
    ip, port, account, pwd_md5, trade_pwd_md5 = get_opend_config()
    print(f"OpenD 配置:")
    print(f"  地址：{ip}:{port}")
    print(f"  账号：{account}")
    print(f"  登录密码 MD5: {pwd_md5[:8]}..." if pwd_md5 else "  登录密码 MD5: 无")
    print(f"  交易密码 MD5: {trade_pwd_md5[:8]}..." if trade_pwd_md5 else "  交易密码 MD5: 无")
