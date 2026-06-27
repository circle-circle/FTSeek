#!/usr/bin/env python3
"""
OpenD 健康检查脚本

功能:
1. 检测 OpenD 进程状态
2. 检测 11111 端口连接
3. 检测富途 API 连接状态
4. 异常时发送钉钉告警 + 自动重启

使用方法:
  python3 /opt/ftseek/FTSeek-US/scripts/opend_health_check.py

定时任务 (每 5 分钟):
  */5 * * * * /usr/bin/python3 /opt/ftseek/FTSeek-US/scripts/opend_health_check.py >> /opt/ftseek/FTSeek-US/logs/opend_health_check.log 2>&1
"""
import os
import sys
import time
import socket
import subprocess
import logging
from datetime import datetime
from typing import Optional, Tuple
from pathlib import Path

# 加载环境变量
env_file = Path("/opt/ftseek/FTSeek-US/.env.us")
if env_file.exists():
    with open(env_file, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("DINGTALK_WEBHOOK_URL_US="):
                os.environ["DINGTALK_WEBHOOK_URL_US"] = line.split("=", 1)[1]
            elif line.startswith("DINGTALK_SECRET_US="):
                os.environ["DINGTALK_SECRET_US"] = line.split("=", 1)[1]

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/opt/ftseek/FTSeek-US/logs/opend_health_check.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("opend_health_check")

# ============================================================
# 配置
# ============================================================
OPEND_PATH = "/opt/ftseek/Futu_OpenD_10.7.6728_Centos7/FutuOpenD"
FUTU_OPEND_PORT = 11111
FUTU_OPEND_HOST = "127.0.0.1"
CHECK_TIMEOUT = 5  # 每次检测超时 (秒)
MAX_CHECK_TIME = 10  # 总检测时间上限 (秒)

# 钉钉告警配置
DINGTALK_WEBHOOK = os.getenv("DINGTALK_WEBHOOK_URL_US")
DINGTALK_SECRET = os.getenv("DINGTALK_SECRET_US")

# ============================================================
# 钉钉告警
# ============================================================
class DingTalkAlerter:
    """钉钉告警推送器"""
    
    def __init__(self, webhook_url: str, secret: str):
        self.webhook_url = webhook_url
        self.secret = secret
    
    def _generate_sign(self) -> str:
        """生成钉钉签名"""
        import hmac
        import hashlib
        import base64
        from urllib.parse import quote_plus
        
        timestamp = str(round(time.time() * 1000))
        secret_enc = self.secret.encode('utf-8')
        string_to_sign = f'{timestamp}\n{self.secret}'
        string_to_sign_enc = string_to_sign.encode('utf-8')
        
        hmac_code = hmac.new(
            secret_enc,
            string_to_sign_enc,
            digestmod=hashlib.sha256
        ).digest()
        
        sign = quote_plus(base64.b64encode(hmac_code))
        return f"&timestamp={timestamp}&sign={sign}"
    
    def send_alert(self, level: str, title: str, content: str) -> bool:
        """发送告警"""
        try:
            import requests
            
            # 根据级别设置颜色
            colors = {
                "INFO": "#00ff00",
                "WARN": "#ffa500",
                "CRITICAL": "#ff0000",
            }
            color = colors.get(level, "#000000")
            
            # Markdown 格式
            markdown = f"""## {title}

**级别**: <font color="{color}">{level}</font>

**内容**: {content}

**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
            
            message = {
                "msgtype": "markdown",
                "markdown": {
                    "title": title,
                    "text": markdown
                }
            }
            
            url = self.webhook_url
            if self.secret:
                url += self._generate_sign()
            
            response = requests.post(url, json=message, timeout=10)
            result = response.json()
            
            if result.get("errcode") == 0:
                logger.info(f"✅ 钉钉告警发送成功：{title}")
                return True
            else:
                logger.error(f"❌ 钉钉告警发送失败：{result}")
                return False
                
        except Exception as e:
            logger.error(f"❌ 发送钉钉告警异常：{e}")
            return False


def send_dingtalk_alert(level: str, title: str, content: str) -> bool:
    """发送钉钉告警"""
    if not DINGTALK_WEBHOOK or not DINGTALK_SECRET:
        logger.error("❌ 钉钉配置缺失 (DINGTALK_WEBHOOK_URL_US, DINGTALK_SECRET_US)")
        return False
    
    alerter = DingTalkAlerter(DINGTALK_WEBHOOK, DINGTALK_SECRET)
    return alerter.send_alert(level, title, content)


# ============================================================
# 健康检查函数
# ============================================================
def check_opend_process() -> Tuple[bool, Optional[int]]:
    """
    检测 OpenD 进程状态
    
    Returns:
        Tuple[bool, Optional[int]]: (是否运行，PID)
    """
    start_time = time.time()
    try:
        result = subprocess.run(
            ["pgrep", "-f", "FutuOpenD"],
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT
        )
        
        elapsed = time.time() - start_time
        logger.info(f"[{elapsed:.2f}s] 检测 OpenD 进程")
        
        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split('\n')
            # 返回主进程 PID (第一个)
            return True, int(pids[0])
        return False, None
        
    except subprocess.TimeoutExpired:
        logger.error("⚠️  检测 OpenD 进程超时")
        return False, None
    except Exception as e:
        logger.error(f"❌ 检测 OpenD 进程异常：{e}")
        return False, None


def check_port_connection() -> bool:
    """
    检测 11111 端口连接
    
    Returns:
        bool: 端口是否可连接
    """
    start_time = time.time()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(CHECK_TIMEOUT)
        result = sock.connect_ex((FUTU_OPEND_HOST, FUTU_OPEND_PORT))
        sock.close()
        
        elapsed = time.time() - start_time
        logger.info(f"[{elapsed:.2f}s] 检测端口 {FUTU_OPEND_HOST}:{FUTU_OPEND_PORT}")
        
        return result == 0
        
    except socket.timeout:
        logger.error("⚠️  连接端口超时")
        return False
    except Exception as e:
        logger.error(f"❌ 连接端口异常：{e}")
        return False


def check_futu_api() -> bool:
    """
    检测富途 API 连接状态
    
    Returns:
        bool: API 是否可连接
    """
    start_time = time.time()
    try:
        # 尝试导入 futu API
        from futu import OpenQuoteContext
        
        # 尝试创建连接
        quote_ctx = OpenQuoteContext(host=FUTU_OPEND_HOST, port=FUTU_OPEND_PORT)
        
        # 尝试获取简单数据 (测试连接)
        ret_code, ret_data = quote_ctx.get_market_snapshot("HK.00700")
        
        # 关闭连接
        quote_ctx.close()
        
        elapsed = time.time() - start_time
        logger.info(f"[{elapsed:.2f}s] 检测富途 API 连接")
        
        # 检查返回码 (0 表示成功)
        if ret_code == 0:
            return True
        else:
            logger.warning(f"⚠️  API 返回错误码：{ret_code}")
            return False
            
    except ImportError:
        logger.error("❌ futu API 未安装")
        return False
    except Exception as e:
        logger.error(f"❌ 富途 API 连接异常：{e}")
        return False


# ============================================================
# OpenD 重启函数
# ============================================================
def restart_opend() -> bool:
    """
    重启 OpenD
    
    Returns:
        bool: 是否成功重启
    """
    logger.info("🔄 开始重启 OpenD...")
    
    try:
        # 1. 停止现有进程
        logger.info("⏹️  停止现有 OpenD 进程...")
        subprocess.run(
            ["pkill", "-f", "FutuOpenD"],
            timeout=10
        )
        time.sleep(2)
        
        # 2. 启动新进程
        logger.info("▶️  启动 OpenD...")
        opend_dir = os.path.dirname(OPEND_PATH)
        
        # 使用 nohup 后台启动
        subprocess.Popen(
            ["./FutuOpenD"],
            cwd=opend_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        
        # 3. 等待 OpenD 就绪
        logger.info("⏳ 等待 OpenD 就绪...")
        for i in range(30):
            time.sleep(1)
            is_running, _ = check_opend_process()
            if is_running:
                # 检查端口
                if check_port_connection():
                    logger.info("✅ OpenD 重启成功")
                    return True
        
        logger.error("❌ OpenD 重启超时")
        return False
        
    except Exception as e:
        logger.error(f"❌ 重启 OpenD 异常：{e}")
        return False


# ============================================================
# 主函数
# ============================================================
def main():
    """主函数"""
    start_time = time.time()
    logger.info("=" * 60)
    logger.info("OpenD 健康检查开始")
    logger.info("=" * 60)
    
    issues = []
    
    try:
        # 1. 检测 OpenD 进程
        is_running, pid = check_opend_process()
        if not is_running:
            issues.append("OpenD 进程未运行")
            logger.error("❌ OpenD 进程未运行")
        else:
            logger.info(f"✅ OpenD 进程运行中 (PID: {pid})")
        
        # 2. 检测端口连接
        if not check_port_connection():
            issues.append(f"端口 {FUTU_OPEND_PORT} 无法连接")
            logger.error(f"❌ 端口 {FUTU_OPEND_PORT} 无法连接")
        else:
            logger.info(f"✅ 端口 {FUTU_OPEND_PORT} 连接正常")
        
        # 3. 检测富途 API
        if not check_futu_api():
            issues.append("富途 API 连接失败")
            logger.error("❌ 富途 API 连接失败")
        else:
            logger.info("✅ 富途 API 连接正常")
        
        # 4. 检查总耗时
        elapsed = time.time() - start_time
        logger.info(f"⏱️  总检测时间：{elapsed:.2f}s")
        
        if elapsed > MAX_CHECK_TIME:
            logger.warning(f"⚠️  检测时间超过上限 ({MAX_CHECK_TIME}s)")
        
        # 5. 处理异常
        if issues:
            error_msg = "\n".join([f"- {issue}" for issue in issues])
            alert_title = "🚨 OpenD 健康检查异常"
            alert_content = f"检测到以下问题:\n\n{error_msg}\n\n将自动重启 OpenD..."
            
            logger.warning(f"⚠️  发送钉钉告警...")
            send_dingtalk_alert("CRITICAL", alert_title, alert_content)
            
            # 自动重启
            logger.info("🔄 执行自动重启...")
            if restart_opend():
                logger.info("✅ OpenD 重启完成")
                # 发送恢复通知
                send_dingtalk_alert(
                    "INFO",
                    "✅ OpenD 已自动恢复",
                    "OpenD 已成功重启，服务恢复正常"
                )
            else:
                logger.error("❌ OpenD 重启失败")
                send_dingtalk_alert(
                    "CRITICAL",
                    "❌ OpenD 重启失败",
                    "OpenD 自动重启失败，请手动干预!"
                )
        else:
            logger.info("✅ OpenD 健康检查通过")
        
    except Exception as e:
        logger.error(f"❌ 健康检查异常：{e}")
        send_dingtalk_alert(
            "CRITICAL",
            "❌ OpenD 健康检查异常",
            f"健康检查脚本执行异常:\n{str(e)}"
        )
    
    finally:
        total_elapsed = time.time() - start_time
        logger.info("=" * 60)
        logger.info(f"OpenD 健康检查结束 (总耗时：{total_elapsed:.2f}s)")
        logger.info("=" * 60)


if __name__ == "__main__":
    main()
