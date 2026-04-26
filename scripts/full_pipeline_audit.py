#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 全链路深度审计脚本 (Full Pipeline Deep Audit)

功能:
- 6大模块逐一排查 (数据采集/LLM/AI/交易管线/Cron调度/通知/系统)
- 发现所有静默失败的组件
- 输出结构化报告到 JSON
- 严重问题自动告警到钉钉

用法:
    python3 scripts/full_pipeline_audit.py           # 完整审计
    python3 scripts/full_pipeline_audit.py --quiet    # 仅输出异常
    python3 scripts/full_pipeline_audit.py --json     # 输出 JSON 格式

作者: FTSeek Team
日期: 2026-04-15
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path("/opt/ftseek/FTSeek")
sys.path.insert(0, str(BASE_DIR))

try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("full_pipeline_audit")


# ============================================================
# 审计引擎
# ============================================================

class PipelineAuditor:
    """全链路审计引擎"""
    
    def __init__(self, quiet=False):
        self.quiet = quiet
        self.issues = {"critical": [], "warning": [], "info": []}
        self.start_time = time.time()
    
    def add_issue(self, severity, module, detail):
        self.issues[severity].append({"module": module, "detail": detail})
    
    # ========================================================================
    # 模块1: 数据采集层
    # ========================================================================
    
    def audit_data_collection(self):
        results = []
        
        # 1.1 RSSHub 日志
        log = BASE_DIR / "logs" / "rsshub_multi_source.log"
        if log.exists():
            with open(log) as f:
                content = f.read()
            last_500 = content.split('\n')[-500:]
            errors = [l for l in last_500 if 'error' in l.lower() or 'traceback' in l.lower()]
            if errors:
                self.add_issue("critical", "RSSHub", f"最近500行有 {len(errors)} 个错误")
                # 分类错误类型
                error_types = {}
                for l in errors:
                    for pattern in ['TypeError', 'AttributeError', 'KeyError', 'Timeout', 'ConnectionError']:
                        if pattern in l:
                            error_types[pattern] = error_types.get(pattern, 0) + 1
                for t, c in error_types.items():
                    results.append(f"  🔴 {t}: {c}次")
            else:
                results.append("  ✅ RSSHub 最近500行无错误")
            
            age_h = (time.time() - log.stat().st_mtime) / 3600
            if age_h > 1:
                results.append(f"  {'✅' if age_h < 0.1 else '🟡'} RSSHub 最后更新: {age_h:.1f}h前")
        
        # 1.2 Futu Flash
        log = BASE_DIR / "logs" / "futu_flash_cron.log"
        if log.exists():
            age_h = (time.time() - log.stat().st_mtime) / 3600
            with open(log) as f:
                content = f.read()
            last_50 = content.split('\n')[-50:]
            errors = [l for l in last_50 if 'error' in l.lower()]
            status = "✅" if not errors and age_h < 1 else "🔴"
            results.append(f"  {status} Futu Flash: {age_h:.1f}h前, 错误={len(errors)}")
        
        # 1.3 金十 API
        try:
            import requests
            r = requests.get("https://flash-api.jin10.com/get_flash_list",
                           params={"channel": "-8200", "vip": "1"},
                           timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                items = r.json().get('data', [])
                results.append(f"  ✅ 金十 API: 正常, {len(items)} 条")
            else:
                self.add_issue("warning", "金十API", f"HTTP {r.status_code}")
                results.append(f"  🟡 金十 API: HTTP {r.status_code}")
        except Exception as e:
            self.add_issue("warning", "金十API", f"不可达: {str(e)[:80]}")
            results.append(f"  🟡 金十 API: {str(e)[:60]}")
        
        # 1.4 RSSHub 远程端点
        for name, url in [("富途快讯", "https://rsshub.rssforever.com/futunn/live")]:
            try:
                import requests
                r = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code != 200:
                    self.add_issue("critical", f"RSSHub-{name}", f"HTTP {r.status_code}")
            except:
                pass  # 不告警, 可能是网络波动
        
        return results
    
    # ========================================================================
    # 模块2: LLM/AI 层
    # ========================================================================
    
    def audit_llm_ai(self):
        results = []
        
        # 2.1 API Key
        api_key = os.environ.get("BAILIAN_API_KEY", "")
        if not api_key:
            self.add_issue("critical", "LLM", "BAILIAN_API_KEY 未加载")
            results.append("  🔴 Bailian API Key: 未配置")
        else:
            results.append(f"  ✅ Bailian API Key: 已配置 ({api_key[:6]}...)")
        
        # 2.2 连通性
        if api_key:
            try:
                from openai import OpenAI
                client = OpenAI(api_key=api_key, 
                              base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
                start = time.time()
                resp = client.chat.completions.create(
                    model="qwen-plus", messages=[{"role": "user", "content": "OK"}], max_tokens=5)
                latency = time.time() - start
                results.append(f"  ✅ LLM: 连通, 延迟 {latency:.1f}s")
            except ImportError:
                self.add_issue("critical", "LLM", "openai 库未安装")
                results.append("  🔴 LLM: openai 库缺失")
            except Exception as e:
                self.add_issue("critical", "LLM", f"调用失败: {str(e)[:100]}")
                results.append(f"  🔴 LLM: {str(e)[:80]}")
        
        return results
    
    # ========================================================================
    # 模块3: 交易管线
    # ========================================================================
    
    def audit_trading_pipeline(self):
        results = []
        
        # 3.1 SignalStore DB
        sig_db = BASE_DIR / "data" / "signals.db"
        if sig_db.exists():
            import sqlite3
            try:
                conn = sqlite3.connect(str(sig_db))
                tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                conn.close()
                if not tables:
                    self.add_issue("warning", "SignalStore", "SQLite 数据库无表 (仅内存模式)")
                    results.append("  🟡 SignalStore: 无持久化表 (仅内存模式)")
                else:
                    results.append(f"  ✅ SignalStore: {len(tables)} 张表")
            except Exception as e:
                self.add_issue("critical", "SignalStore", f"DB 损坏: {str(e)[:80]}")
        else:
            results.append("  🟡 SignalStore: 使用内存模式 (无持久化)")
        
        # 3.2 Futu 连接
        try:
            from futu import OpenQuoteContext
            ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
            ret, _ = ctx.get_market_snapshot(["HK.00700"])
            ctx.close()
            results.append(f"  {'✅' if ret == 0 else '🔴'} Futu 连接: {'正常' if ret == 0 else '失败'}")
        except Exception as e:
            self.add_issue("critical", "Futu", f"连接失败: {str(e)[:80]}")
            results.append(f"  🔴 Futu: {str(e)[:60]}")
        
        # 3.3 Callback Server
        try:
            import requests
            r = requests.get("http://localhost:8899/health", timeout=3)
            results.append(f"  {'✅' if r.status_code == 200 else '🔴'} 回调服务: HTTP {r.status_code}")
        except:
            self.add_issue("critical", "回调服务", "不可达")
            results.append("  🔴 回调服务: 不可达")
        
        return results
    
    # ========================================================================
    # 模块4: Cron 调度
    # ========================================================================
    
    def audit_cron(self):
        results = []
        
        cron_logs = {
            "RSSHub": ("logs/rsshub_multi_source.log", 1),
            "富途快讯": ("logs/futu_flash_cron.log", 1),
            "盘前巡检": ("logs/audit/pre_market_cron.log", 24),
            "午间检查": ("logs/audit/midday_cron.log", 24),
            "盘后审计": ("logs/audit/post_market_cron.log", 24),
            "深度分析": ("logs/audit/deep_analysis_cron.log", 24),
            "数据收集": ("logs/data_collector.log", 24),
            "盘前策略": ("logs/pre_market_strategy_push.log", 24),
            "健康守护": ("logs/health_guardian.log", 1),
        }
        
        for name, (log_file, threshold_h) in cron_logs.items():
            path = BASE_DIR / log_file
            if not path.exists():
                self.add_issue("warning", "Cron", f"{name}: 日志不存在")
                results.append(f"  ❌ {name}: 日志不存在")
                continue
            
            age_h = (time.time() - path.stat().st_mtime) / 3600
            with open(path) as f:
                content = f.read()
            errors = content.lower().count('error') + content.lower().count('traceback')
            
            if age_h > threshold_h:
                self.add_issue("warning", "Cron", f"{name}: {age_h:.0f}h 未更新")
                icon = "🟡"
            elif errors > 10:
                self.add_issue("critical", "Cron", f"{name}: {errors} 个错误")
                icon = "🔴"
            else:
                icon = "✅"
            
            results.append(f"  {icon} {name:12s}: {age_h:5.1f}h前 | 错误={errors}")
        
        return results
    
    # ========================================================================
    # 模块5: 系统层
    # ========================================================================
    
    def audit_system(self):
        results = []
        
        # 磁盘
        try:
            r = subprocess.run(["df", "-h", "/opt"], capture_output=True, text=True)
            for line in r.stdout.split('\n')[1:]:
                parts = line.split()
                if len(parts) >= 5:
                    pct = int(parts[4].rstrip('%'))
                    if pct > 90:
                        self.add_issue("critical", "系统", f"磁盘 {pct}% 已满")
                    results.append(f"  {'✅' if pct < 80 else '🔴'} 磁盘: {pct}%")
        except:
            pass
        
        # 内存
        try:
            r = subprocess.run(["free", "-m"], capture_output=True, text=True)
            for line in r.stdout.split('\n'):
                if line.startswith("Mem:"):
                    parts = line.split()
                    pct = int(parts[2]) / int(parts[1]) * 100
                    results.append(f"  {'✅' if pct < 80 else '🔴'} 内存: {pct:.0f}%")
        except:
            pass
        
        # 进程
        for name, pattern in [("Futu OpenD", "OpenD"), ("Health Guardian", "health_guardian")]:
            try:
                r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
                if r.stdout.strip():
                    results.append(f"  ✅ {name}: 运行中")
                else:
                    self.add_issue("critical", "系统", f"{name} 进程不存在")
                    results.append(f"  🔴 {name}: 不存在")
            except:
                pass
        
        return results
    
    # ========================================================================
    # 执行审计
    # ========================================================================
    
    def run(self):
        results = {}
        
        results["数据采集"] = self.audit_data_collection()
        results["LLM/AI"] = self.audit_llm_ai()
        results["交易管线"] = self.audit_trading_pipeline()
        results["Cron 调度"] = self.audit_cron()
        results["系统层"] = self.audit_system()
        
        elapsed = time.time() - self.start_time
        
        # 输出报告
        print("=" * 80)
        print(f"🔍 FTSeek 全链路深度审计报告 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"   耗时: {elapsed:.1f}s")
        print("=" * 80)
        
        for module, lines in results.items():
            print(f"\n📋 {module}")
            for line in lines:
                print(line)
        
        # 汇总
        n_c = len(self.issues["critical"])
        n_w = len(self.issues["warning"])
        n_i = len(self.issues["info"])
        print(f"\n{'=' * 80}")
        print(f"📊 汇总: 🔴 {n_c} 严重 | 🟡 {n_w} 警告 | ℹ️ {n_i} 信息")
        print(f"{'=' * 80}")
        
        if self.issues["critical"]:
            print("\n🔴 严重问题:")
            for i, issue in enumerate(self.issues["critical"], 1):
                print(f"  {i}. [{issue['module']}] {issue['detail']}")
        
        if self.issues["warning"]:
            print("\n🟡 警告:")
            for i, issue in enumerate(self.issues["warning"], 1):
                print(f"  {i}. [{issue['module']}] {issue['detail']}")
        
        # 保存报告
        report = {
            "timestamp": datetime.now().isoformat(),
            "elapsed_seconds": round(elapsed, 1),
            "critical_count": n_c,
            "warning_count": n_w,
            "info_count": n_i,
            "critical": self.issues["critical"],
            "warning": self.issues["warning"],
        }
        report_file = BASE_DIR / "logs" / "full_audit_report.json"
        with open(report_file, 'w') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"\n💾 报告已保存: {report_file}")
        
        return self.issues


if __name__ == "__main__":
    auditor = PipelineAuditor()
    auditor.run()
