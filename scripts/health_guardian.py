#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 系统健康守护进程 (System Health Guardian)

功能:
1. 每分钟检查所有关键服务状态
2. 发现异常立即告警 (首次异常+持续异常)
3. 自动尝试修复 (重启服务/清理缓存)
4. 记录健康度趋势

监控项:
- RSSHub 数据新鲜度
- Futu OpenD 连接
- 回调服务可用性
- 磁盘/内存
- 关键数据文件新鲜度
- Cron 任务执行状态
- 错误日志密度

告警策略:
- 首次发现异常: 立即推送到钉钉
- 持续异常: 每30分钟告警一次 (防止轰炸)
- 自动恢复: 推送恢复通知

作者: FTSeek Team
日期: 2026-04-15
"""

import json
import logging
import os
import subprocess
import sys
import time
import glob
from datetime import datetime, timedelta
from pathlib import Path

# ============================================================
# 配置
# ============================================================
BASE_DIR = Path("/opt/ftseek/FTSeek")
LOG_FILE = BASE_DIR / "logs" / "health_guardian.log"
STATE_FILE = BASE_DIR / "data" / "health_guardian_state.json"
ALERT_COOLDOWN = 1800  # 同一告警30分钟内不重复

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
    ]
)
logger = logging.getLogger("health_guardian")

# 加载 .env
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

# ============================================================
# 健康状态管理
# ============================================================

class HealthState:
    """持久化健康状态, 用于去重和持续异常跟踪"""
    
    def __init__(self):
        self.state = self._load()
    
    def _load(self):
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE) as f:
                    return json.load(f)
            except:
                pass
        return {
            "alerts": {},  # alert_key -> last_alert_time
            "checks": {},  # check_name -> last_status
            "last_full_check": 0,
        }
    
    def save(self):
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.error(f"保存健康状态失败: {e}")
    
    def should_alert(self, alert_key: str) -> bool:
        """判断是否应该告警 (防轰炸)"""
        now = time.time()
        last = self.state["alerts"].get(alert_key, 0)
        if now - last > ALERT_COOLDOWN:
            self.state["alerts"][alert_key] = now
            return True
        return False
    
    def update_status(self, check_name: str, is_healthy: bool):
        prev = self.state["checks"].get(check_name, {})
        prev_healthy = prev.get("healthy", None)
        
        # 连续计数: 用于稳定恢复判定
        if is_healthy:
            consecutive = prev.get("consecutive_healthy", 0) + 1 if prev_healthy is True else 1
            self.state["checks"][check_name] = {
                "healthy": True,
                "timestamp": time.time(),
                "consecutive_healthy": consecutive,
                "consecutive_unhealthy": 0,
            }
        else:
            consecutive = prev.get("consecutive_unhealthy", 0) + 1 if prev_healthy is False else 1
            self.state["checks"][check_name] = {
                "healthy": False,
                "timestamp": time.time(),
                "consecutive_healthy": 0,
                "consecutive_unhealthy": consecutive,
            }


# ============================================================
# 检查项
# ============================================================

def check_rsshub_health() -> dict:
    """检查 RSSHub 数据新鲜度和错误密度"""
    result = {"healthy": True, "details": "", "severity": "info"}
    
    cache_dir = BASE_DIR / "data" / "rsshub_multi_source"
    if not cache_dir.exists():
        result.update({"healthy": False, "details": "RSSHub 缓存目录不存在", "severity": "critical"})
        return result
    
    # 检查缓存文件
    json_files = sorted(cache_dir.glob("multi_source_*.json"))
    if not json_files:
        result.update({"healthy": False, "details": "无 RSSHub 缓存文件", "severity": "critical"})
        return result
    
    latest = max(json_files, key=os.path.getmtime)
    age_h = (time.time() - os.path.getmtime(latest)) / 3600
    
    if age_h > 12:
        result.update({
            "healthy": False,
            "details": f"RSSHub 缓存 {age_h:.0f}h 未更新 (阈值12h)",
            "severity": "critical"
        })
        return result
    
    if age_h > 6:
        result.update({
            "healthy": False,
            "details": f"RSSHub 缓存 {age_h:.0f}h 未更新 (阈值6h)",
            "severity": "warning"
        })
        return result
    
    # 检查日志错误密度
    log_file = BASE_DIR / "logs" / "rsshub_multi_source.log"
    if log_file.exists():
        try:
            with open(log_file) as f:
                lines = f.readlines()
            last_100 = lines[-100:] if len(lines) >= 100 else lines
            error_count = sum(1 for l in last_100 if 'error' in l.lower() or 'traceback' in l.lower())
            
            if error_count > 20:
                result.update({
                    "healthy": False,
                    "details": f"RSSHub 最近100行有 {error_count} 个错误",
                    "severity": "critical"
                })
                return result
            elif error_count > 5:
                result.update({
                    "healthy": False,
                    "details": f"RSSHub 最近100行有 {error_count} 个错误",
                    "severity": "warning"
                })
                return result
        except:
            pass
    
    result["details"] = f"RSSHub 正常 (缓存{age_h:.1f}h前)"
    return result


def check_futu_flash_health() -> dict:
    """检查 futu_flash 数据新鲜度和锁状态"""
    result = {"healthy": True, "details": "", "severity": "info"}

    data_dir = BASE_DIR / "data" / "futu_flash"
    if not data_dir.exists():
        result.update({"healthy": False, "details": "futu_flash 数据目录不存在", "severity": "critical"})
        return result

    # 检查今日数据文件
    today_file = data_dir / f"futu_flash_{datetime.now().strftime('%Y%m%d')}.json"
    if today_file.exists():
        age_m = (time.time() - os.path.getmtime(today_file)) / 60
        if age_m > 15:
            result.update({
                "healthy": False,
                "details": f"futu_flash 数据 {age_m:.0f}min 未更新 (阈值15min)",
                "severity": "critical"
            })
            return result
    else:
        # 检查最近的文件是否超过 30 分钟
        json_files = sorted(data_dir.glob("futu_flash_*.json"))
        if not json_files:
            result.update({"healthy": False, "details": "无 futu_flash 数据文件", "severity": "critical"})
            return result
        latest = max(json_files, key=os.path.getmtime)
        age_m = (time.time() - os.path.getmtime(latest)) / 60
        if age_m > 30:
            result.update({
                "healthy": False,
                "details": f"futu_flash 最新数据 {age_m:.0f}min 未更新 (阈值30min)",
                "severity": "critical"
            })
            return result

    # 检查锁文件是否被卡死进程持有 (2026-04-16 修复: 区分正常 cron 运行和僵尸进程)
    lock_file = "/tmp/futu_flash_cron.lock"
    if os.path.exists(lock_file):
        try:
            import fcntl
            fd = os.open(lock_file, os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                # 锁未被持有，正常
            except (IOError, OSError):
                # 锁被持有，检查持有者是否为僵尸进程
                # 查找持有锁的 futu_flash_cron 进程
                import subprocess
                proc_result = subprocess.run(
                    ["pgrep", "-f", "futu_flash_cron.py"],
                    capture_output=True, text=True
                )
                if proc_result.returncode == 0:
                    pids = proc_result.stdout.strip().split('\n')
                    # 检查是否有运行超过 3 分钟的进程（僵尸阈值）
                    zombie_found = False
                    for pid in pids:
                        pid = pid.strip()
                        if not pid:
                            continue
                        try:
                            etimes_result = subprocess.run(
                                ["ps", "-o", "etimes=", "-p", pid],
                                capture_output=True, text=True
                            )
                            if etimes_result.returncode == 0:
                                etimes = int(etimes_result.stdout.strip())
                                if etimes > 180:  # 运行超过 3 分钟 → 僵尸
                                    zombie_found = True
                                    logger.warning(f"🔍 futu_flash 僵尸进程 PID={pid}, 已运行 {etimes}s")
                                    break
                        except (ValueError, IndexError):
                            continue
                    
                    if not zombie_found:
                        # 锁被持有但进程都是新的 (< 3min)，属于正常 cron 运行窗口，跳过告警
                        logger.debug("futu_flash 锁被正常 cron 进程持有，跳过告警")
                    else:
                        result.update({
                            "healthy": False,
                            "details": "futu_flash 锁文件被僵尸进程占用 (运行>3min)",
                            "severity": "warning"
                        })
                        return result
                else:
                    # 找不到进程但锁被持有 → 可能是孤儿锁
                    result.update({
                        "healthy": False,
                        "details": "futu_flash 锁文件被占用但找不到对应进程 (孤儿锁)",
                        "severity": "warning"
                    })
                    return result
            finally:
                os.close(fd)
        except Exception as e:
            logger.debug(f"futu_flash 锁检查异常: {e}")

    # 检查日志错误密度
    log_file = BASE_DIR / "logs" / "futu_flash_cron.log"
    if log_file.exists():
        try:
            with open(log_file) as f:
                lines = f.readlines()
            last_100 = lines[-100:] if len(lines) >= 100 else lines
            error_count = sum(1 for l in last_100 if 'error' in l.lower() or 'traceback' in l.lower())

            if error_count > 20:
                result.update({
                    "healthy": False,
                    "details": f"futu_flash 最近100行有 {error_count} 个错误",
                    "severity": "critical"
                })
                return result
            elif error_count > 5:
                result.update({
                    "healthy": False,
                    "details": f"futu_flash 最近100行有 {error_count} 个错误",
                    "severity": "warning"
                })
                return result
        except:
            pass

    result["details"] = "futu_flash 正常"
    return result


def check_futu_opend() -> dict:
    """检查 Futu OpenD 连接状态（轻量版：TCP 端口检测，不创建/关闭连接）"""
    result = {"healthy": True, "details": "", "severity": "info"}
    
    try:
        r = subprocess.run(["pgrep", "-f", "OpenD"], capture_output=True, text=True)
        if not r.stdout.strip():
            result.update({
                "healthy": False,
                "details": "Futu OpenD 进程不存在",
                "severity": "critical"
            })
            return result
        
        # 轻量 TCP 端口检测 — 避免频繁创建/关闭 Futu 连接
        import socket
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            conn_result = sock.connect_ex(("127.0.0.1", 11111))
            sock.close()
            if conn_result == 0:
                result["details"] = "Futu OpenD 端口可达 (11111)"
            else:
                result.update({
                    "healthy": False,
                    "details": f"Futu OpenD 端口 11111 不可达",
                    "severity": "critical"
                })
        except Exception as e:
            result.update({
                "healthy": False,
                "details": f"OpenD 端口检测异常: {str(e)[:80]}",
                "severity": "warning"
            })
    except Exception as e:
        result.update({
            "healthy": False,
            "details": f"OpenD 检查异常: {str(e)[:80]}",
            "severity": "warning"
        })
    
    return result


def check_callback_service() -> dict:
    """检查回调服务可用性"""
    result = {"healthy": True, "details": "", "severity": "info"}
    
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--connect-timeout", "3",
             "http://localhost:8899/health"],
            capture_output=True, text=True, timeout=10
        )
        code = r.stdout.strip()
        if code == "200":
            result["details"] = "回调服务正常 (HTTP 200)"
        else:
            result.update({
                "healthy": False,
                "details": f"回调服务异常 (HTTP {code})",
                "severity": "critical"
            })
    except Exception as e:
        result.update({
            "healthy": False,
            "details": f"回调服务不可达: {str(e)[:80]}",
            "severity": "critical"
        })
    
    return result


def check_data_files() -> dict:
    """检查关键数据文件新鲜度 (智能版: 区分市场时段/检查DB内容)"""
    result = {"healthy": True, "details": "", "severity": "info"}
    
    now = datetime.now()
    hour = now.hour
    is_trading_hours = 9 <= hour < 17  # 港股 9:30-16:00
    is_pre_market = 7 <= hour < 9
    is_recent_close = 16 <= hour < 18  # 收盘后2小时内
    
    # 非交易时段: 只做轻度检查, 不告警文件过期
    if not (is_trading_hours or is_recent_close):
        # 只检查文件是否存在, 不检查新鲜度
        main_db = BASE_DIR / "data" / "ftseek.db"
        if not main_db.exists():
            result.update({
                "healthy": False,
                "details": "主数据库 ftseek.db 不存在",
                "severity": "critical"
            })
        else:
            result["details"] = f"非交易时段 (当前 {hour}:00), 跳过新鲜度检查"
        return result
    
    stale = []
    
    # 1. 检查信号管线健康（多指标综合，避免单一 DB 误报）
    import sqlite3
    main_db = BASE_DIR / "data" / "ftseek.db"

    signal_healthy = False
    signal_detail = ""

    # 1a. 优先检查 HTTP API (实时内存信号)
    try:
        import urllib.request
        resp = urllib.request.urlopen("http://localhost:8899/api/signals", timeout=5)
        if resp.status == 200:
            import json
            data = json.loads(resp.read().decode())
            pending = data.get("pending", []) if isinstance(data, dict) else []
            if len(pending) > 0:
                signal_healthy = True
                signal_detail = f"内存信号: {len(pending)} 条待处理"
    except Exception:
        pass

    # 1b. 如果 API 无信号, 检查日志中最近 30 分钟是否有信号活动
    if not signal_healthy:
        log_file = BASE_DIR / "logs" / "ftseek.log"
        if log_file.exists():
            try:
                import re
                cutoff = (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M")
                with open(log_file) as f:
                    lines = f.readlines()
                recent = [l for l in lines[-2000:] if l[:16] >= cutoff]
                signal_activity = sum(1 for l in recent if any(k in l for k in [
                    "信号", "signal", "decision_buffer", "NEWS-ANA",
                    "circuit_breaker", "价格检查", "冷却"
                ]))
                if signal_activity > 0:
                    signal_healthy = True
                    signal_detail = f"管线活跃: 近30min {signal_activity} 条信号活动"
                else:
                    # 真的没有信号活动 → 检查 DB
                    if main_db.exists():
                        try:
                            conn = sqlite3.connect(str(main_db))
                            cursor = conn.cursor()
                            cursor.execute("SELECT MAX(created_at) FROM signals")
                            row = cursor.fetchone()
                            conn.close()
                            if row and row[0]:
                                last_signal = datetime.fromisoformat(row[0])
                                age_h = (now - last_signal).total_seconds() / 3600
                                threshold = 3 if is_trading_hours else 24
                                if age_h > threshold:
                                    stale.append(f"信号库: {age_h:.1f}h无新信号")
                                else:
                                    signal_healthy = True
                                    signal_detail = f"DB信号: {age_h:.1f}h前"
                            else:
                                stale.append("信号库: 从未写入信号")
                        except:
                            pass
                    else:
                        stale.append("信号库: ftseek.db 不存在")
            except:
                pass
        else:
            stale.append("信号库: ftseek.log 不存在")
    
    # 2. 检查决策日志 (轻量: 只查文件mtime, 决策不一定每个交易日都有)
    decision_db = BASE_DIR / "data" / "decision_log.db"
    if decision_db.exists():
        age_h = (time.time() - os.path.getmtime(decision_db)) / 3600
        if age_h > 48:  # 决策日志放宽到48h
            stale.append(f"决策日志: {age_h:.0f}h未更新")
    else:
        stale.append("决策日志: 不存在")
    
    if stale:
        result.update({
            "healthy": False,
            "details": "数据文件异常: " + "; ".join(stale),
            "severity": "warning"
        })
    else:
        result["details"] = "关键数据文件正常"
    
    return result


def check_disk_memory() -> dict:
    """检查磁盘和内存"""
    result = {"healthy": True, "details": "", "severity": "info"}
    issues = []
    
    # 磁盘
    try:
        r = subprocess.run(["df", "-h", "/opt"], capture_output=True, text=True)
        for line in r.stdout.split('\n')[1:]:
            parts = line.split()
            if len(parts) >= 5:
                pct = int(parts[4].rstrip('%'))
                if pct > 90:
                    issues.append(f"磁盘 {pct}% (紧急)")
                elif pct > 80:
                    issues.append(f"磁盘 {pct}% (警告)")
    except:
        pass
    
    # 内存
    try:
        r = subprocess.run(["free", "-m"], capture_output=True, text=True)
        for line in r.stdout.split('\n'):
            if line.startswith("Mem:"):
                parts = line.split()
                pct = int(parts[2]) / int(parts[1]) * 100
                if pct > 90:
                    issues.append(f"内存 {pct:.0f}% (紧急)")
                elif pct > 80:
                    issues.append(f"内存 {pct:.0f}% (警告)")
    except:
        pass
    
    if issues:
        result.update({
            "healthy": False,
            "details": "; ".join(issues),
            "severity": "critical" if "紧急" in " ".join(issues) else "warning"
        })
    else:
        result["details"] = "系统资源正常"
    
    return result


def check_systemd_service() -> dict:
    """检查 systemd 服务状态"""
    result = {"healthy": True, "details": "", "severity": "info"}
    
    try:
        r = subprocess.run(["systemctl", "is-active", "ftseek"], capture_output=True, text=True)
        status = r.stdout.strip()
        if status != "active":
            result.update({
                "healthy": False,
                "details": f"ftseek.service: {status}",
                "severity": "critical"
            })
            return result
        
        # 检查服务重启次数
        r = subprocess.run(["systemctl", "show", "ftseek", "--property", "NRestarts"], capture_output=True, text=True)
        restarts = r.stdout.strip().split('=')[1] if '=' in r.stdout else '0'
        
        result["details"] = f"ftseek.service: active, 重启{restarts}次"
    except Exception as e:
        result.update({
            "healthy": False,
            "details": f"systemd 检查失败: {str(e)[:80]}",
            "severity": "warning"
        })
    
    return result


# ============================================================
# 告警推送
# ============================================================

def send_alert(title: str, message: str, severity: str = "warning"):
    """发送告警到钉钉"""
    webhook = os.getenv("DINGTALK_WEBHOOK_URL", "")
    secret = os.getenv("DINGTALK_SECRET", "")
    
    if not webhook:
        logger.warning("钉钉 Webhook 未配置, 告警仅记录日志")
        return False
    
    import requests
    import hmac
    import hashlib
    import base64
    import urllib.parse
    
    try:
        if secret:
            timestamp = str(round(time.time() * 1000))
            string_to_sign = f"{timestamp}\n{secret}"
            hmac_code = hmac.new(
                secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                digestmod=hashlib.sha256,
            ).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            url = f"{webhook}&timestamp={timestamp}&sign={sign}"
        else:
            url = webhook
        
        severity_icon = {"critical": "🔴", "warning": "🟡", "recovery": "✅"}.get(severity, "📊")
        
        text = f"""### {severity_icon} FTSeek 健康告警 | {title}

{message}

---
⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
        
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"健康告警 | {title}",
                "text": text,
            },
        }
        
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            result = resp.json()
            if result.get("errcode") == 0:
                logger.info(f"✅ 告警推送成功: {title}")
                return True
        
        logger.error(f"❌ 告警推送失败: {resp.status_code} {resp.text[:200]}")
        return False
        
    except Exception as e:
        logger.error(f"告警推送异常: {e}")
        return False


# ============================================================
# 自动修复
# ============================================================

def auto_fix_rsshub():
    """尝试修复 RSSHub: 杀僵尸进程 + 清锁 + 手动触发一次采集"""
    fixes_applied = []
    
    # 1. 查找并杀死僵尸/卡死的 RSSHub 进程
    try:
        result = subprocess.run(
            ["pgrep", "-f", "rsshub_multi_source_cron.py"],
            capture_output=True, text=True, timeout=5
        )
        pids = [p.strip() for p in result.stdout.strip().split('\n') if p.strip()]
        # 找出运行超过 10 分钟的进程 (可能卡死)
        for pid in pids:
            try:
                stat = os.stat(f"/proc/{pid}")
                age = time.time() - stat.st_mtime
                # 用 ps 获取更准确的启动时间
                ps_result = subprocess.run(
                    ["ps", "-o", "etimes=", "-p", pid],
                    capture_output=True, text=True, timeout=5
                )
                if ps_result.returncode == 0:
                    etimes = int(ps_result.stdout.strip())
                    if etimes > 600:  # 运行超过 10 分钟
                        subprocess.run(["kill", "-9", pid], timeout=5)
                        fixes_applied.append(f"kill zombie PID={pid} (age={etimes}s)")
                        logger.info(f"✅ 已杀死卡死的 RSSHub 进程 PID={pid}, 运行了 {etimes}s")
            except (FileNotFoundError, ValueError, ProcessLookupError):
                pass
    except Exception as e:
        logger.warning(f"清理 RSSHub 僵尸进程失败: {e}")
    
    # 2. 清除 flock 锁文件
    lock_file = "/tmp/rsshub_multi_source_cron.lock"
    if os.path.exists(lock_file):
        try:
            os.remove(lock_file)
            fixes_applied.append("remove stale lock file")
            logger.info("✅ 已清除 RSSHub 锁文件")
        except:
            pass
    
    # 3. 立即手动触发一次采集 (验证修复)
    if fixes_applied:
        try:
            logger.info("🔧 手动触发 RSSHub 采集验证修复...")
            result = subprocess.run(
                ["flock", "-n", "/tmp/rsshub_multi_source_cron.lock", "-c",
                 f"cd {BASE_DIR} && python3 scripts/rsshub_multi_source_cron.py"],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                fixes_applied.append("manual run succeeded")
                logger.info("✅ RSSHub 手动采集成功")
            else:
                logger.warning(f"⚠️ RSSHub 手动采集失败: {result.stderr[-200:]}")
        except Exception as e:
            logger.warning(f"手动触发 RSSHub 采集失败: {e}")
    
    return len(fixes_applied) > 0


def auto_fix_opend():
    """尝试重启 OpenD"""
    try:
        subprocess.run(["/opt/ftseek/restart_opend.sh"], timeout=30)
        logger.info("✅ 已尝试重启 OpenD")
        return True
    except:
        return False


def auto_fix_futu_flash():
    """尝试修复 futu_flash: 杀僵尸进程 + 清锁 + 手动触发一次采集"""
    fixes_applied = []

    # 1. 查找并杀死卡死的 futu_flash_cron 进程
    try:
        result = subprocess.run(
            ["pgrep", "-f", "futu_flash_cron.py"],
            capture_output=True, text=True, timeout=5
        )
        pids = [p.strip() for p in result.stdout.strip().split('\n') if p.strip()]
        for pid in pids:
            try:
                ps_result = subprocess.run(
                    ["ps", "-o", "etimes=", "-p", pid],
                    capture_output=True, text=True, timeout=5
                )
                if ps_result.returncode == 0:
                    etimes = int(ps_result.stdout.strip())
                    if etimes > 600:  # 运行超过 10 分钟
                        subprocess.run(["kill", "-9", pid], timeout=5)
                        fixes_applied.append(f"kill zombie PID={pid} (age={etimes}s)")
                        logger.info(f"✅ 已杀死卡死的 futu_flash 进程 PID={pid}, 运行了 {etimes}s")
            except (FileNotFoundError, ValueError, ProcessLookupError):
                pass
    except Exception as e:
        logger.warning(f"清理 futu_flash 僵尸进程失败: {e}")

    # 2. 清除 flock 锁文件
    lock_file = "/tmp/futu_flash_cron.lock"
    if os.path.exists(lock_file):
        try:
            os.remove(lock_file)
            fixes_applied.append("remove stale lock file")
            logger.info("✅ 已清除 futu_flash 锁文件")
        except:
            pass

    # 3. 立即手动触发一次采集 (验证修复)
    if fixes_applied:
        try:
            logger.info("🔧 手动触发 futu_flash 采集验证修复...")
            result = subprocess.run(
                ["flock", "-n", "/tmp/futu_flash_cron.lock", "-c",
                 f"cd {BASE_DIR} && python3 scripts/futu_flash_cron.py"],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                fixes_applied.append("manual run succeeded")
                logger.info("✅ futu_flash 手动采集成功")
            else:
                logger.warning(f"⚠️ futu_flash 手动采集失败: {result.stderr[-200:]}")
        except Exception as e:
            logger.warning(f"手动触发 futu_flash 采集失败: {e}")

    return len(fixes_applied) > 0


# ============================================================
# 主循环
# ============================================================

def run_full_audit() -> dict:
    """执行全链路深度审计 (每10分钟调用一次)"""
    try:
        sys.path.insert(0, str(BASE_DIR))
        from scripts.full_pipeline_audit import PipelineAuditor
        auditor = PipelineAuditor(quiet=True)
        issues = auditor.run()
        return issues
    except Exception as e:
        logger.error(f"全链路审计失败: {e}")
        return {"critical": [], "warning": [], "info": []}


def run_health_check():
    """执行一次完整的健康检查"""
    state = HealthState()
    
    checks = [
        ("RSSHub 数据", check_rsshub_health),
        ("futu_flash", check_futu_flash_health),
        ("Futu OpenD", check_futu_opend),
        ("回调服务", check_callback_service),
        ("数据文件", check_data_files),
        ("系统资源", check_disk_memory),
        ("Systemd 服务", check_systemd_service),
    ]
    
    critical_issues = []
    warning_issues = []
    recovery_notifications = []
    
    for check_name, check_fn in checks:
        try:
            result = check_fn()
            was_healthy = state.state["checks"].get(check_name, {}).get("healthy", None)
            is_healthy = result["healthy"]
            state.update_status(check_name, is_healthy)
            
            if not is_healthy:
                severity = result.get("severity", "warning")
                issue_msg = f"{check_name}: {result['details']}"
                
                if severity == "critical":
                    critical_issues.append(issue_msg)
                else:
                    warning_issues.append(issue_msg)
                
                # 首次异常告警
                alert_key = f"error_{check_name}"
                if state.should_alert(alert_key):
                    emoji = "🔴" if severity == "critical" else "🟡"
                    send_alert(
                        f"{emoji} {check_name} 异常",
                        f"**问题**: {result['details']}\n**严重度**: {severity}",
                        severity
                    )
            else:
                # 从异常恢复: 需要连续 3 次健康检查才推送 (防止振荡)
                consec = state.state["checks"].get(check_name, {}).get("consecutive_healthy", 0)
                if was_healthy is False and consec >= 3:
                    recovery_notifications.append(check_name)
                    if state.should_alert(f"recovery_{check_name}"):
                        send_alert(
                            f"✅ {check_name} 已恢复",
                            f"**{check_name}** 已恢复正常",
                            "recovery"
                        )
                elif was_healthy is False and consec < 3:
                    # 尚未稳定, 只记日志不推送
                    pass
                        
        except Exception as e:
            error_msg = f"{check_name}: 检查异常 - {str(e)[:100]}"
            critical_issues.append(error_msg)
            logger.error(error_msg)
    
    # 汇总告警 (如果有多个问题, 合并推送)
    if critical_issues or warning_issues:
        all_issues = critical_issues + warning_issues
        summary = "\n".join([f"- {issue}" for issue in all_issues])
        
        # 尝试自动修复 (warning 级别也触发)
        all_issue_text = " ".join(critical_issues + warning_issues)
        if "RSSHub" in all_issue_text:
            fixed = auto_fix_rsshub()
            if fixed:
                logger.info("✅ RSSHub 自动修复已执行")
                # 修复后重新检查缓存新鲜度
                time.sleep(5)  # 等待采集完成
                rsshub_result = check_rsshub_health()
                if rsshub_result["healthy"]:
                    # 修复成功，清除告警
                    state.state["checks"]["RSSHub 数据"] = {
                        "healthy": True,
                        "timestamp": time.time(),
                        "consecutive_healthy": 3,
                        "consecutive_unhealthy": 0,
                    }
                    state.save()
                    logger.info("✅ RSSHub 自动修复验证成功，告警已清除")
                else:
                    logger.warning(f"⚠️ RSSHub 自动修复后仍不健康: {rsshub_result['details']}")
        if "OpenD" in " ".join(critical_issues):
            auto_fix_opend()
        if "futu_flash" in all_issue_text or "富途快讯" in all_issue_text:
            fixed = auto_fix_futu_flash()
            if fixed:
                logger.info("✅ futu_flash 自动修复已执行")
                time.sleep(5)
                futu_result = check_futu_flash_health()
                if futu_result["healthy"]:
                    state.state["checks"]["futu_flash"] = {
                        "healthy": True,
                        "timestamp": time.time(),
                        "consecutive_healthy": 3,
                        "consecutive_unhealthy": 0,
                    }
                    state.save()
                    logger.info("✅ futu_flash 自动修复验证成功，告警已清除")
                else:
                    logger.warning(f"⚠️ futu_flash 自动修复后仍不健康: {futu_result['details']}")
        
        logger.warning(f"健康检查发现 {len(critical_issues)} 个严重问题, {len(warning_issues)} 个警告")
    
    if recovery_notifications:
        logger.info(f"已恢复: {', '.join(recovery_notifications)}")
    
    state.state["last_full_check"] = time.time()
    state.save()
    
    return len(critical_issues) == 0 and len(warning_issues) == 0


if __name__ == "__main__":
    # 防重叠锁: 确保只有一个实例运行
    LOCK_FILE = BASE_DIR / "data" / "health_guardian.lock"
    import fcntl
    try:
        lock_fd = open(LOCK_FILE, 'w')
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
    except (IOError, OSError):
        logger.warning("健康守护进程已在运行中, 退出")
        sys.exit(0)
    
    logger.info("=" * 60)
    logger.info("FTSeek 健康守护进程启动")
    logger.info("=" * 60)
    
    # 首次立即检查
    run_health_check()
    
    # 首次立即执行全链路审计
    run_full_audit()
    
    check_count = 0
    
    # 之后每60秒检查一次, 每10分钟执行一次全链路审计
    while True:
        try:
            time.sleep(60)
            check_count += 1
            run_health_check()
            
            # 每10分钟执行一次全链路深度审计
            if check_count % 10 == 0:
                logger.info("执行全链路深度审计...")
                run_full_audit()
        except KeyboardInterrupt:
            logger.info("健康守护进程退出")
            break
        except Exception as e:
            logger.error(f"健康检查循环异常: {e}")
            time.sleep(60)
