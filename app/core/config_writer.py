"""
FTSeek-US 配置持久化写入器 — 唯一允许修改 unified_config.yaml 的模块

安全特性:
- 白名单参数控制 (只允许修改预定义的参数)
- 安全边界校验 (min/max/max_delta)
- 原子写入 (write-then-rename)
- 完整审计轨迹 (config_audit_log.json)
- 版本哈希追踪 (支持回滚)
- 热加载触发 (ConfigContainer reload)
"""
import json
import logging
import os
import shutil
import tempfile
import threading
import hashlib
from datetime import datetime
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, asdict
from app.core.paths import get_config_path, get_data_dir, get_exit_policy_path, get_project_root

logger = logging.getLogger("ftseek_us.config_writer")

# ============================================================================
#  数据类
# ============================================================================

@dataclass
class ConfigChangeRecord:
    """配置变更记录"""
    param_path: str          # YAML dotted path
    old_value: Any
    new_value: Any
    reason: str              # e.g. "auto_fix: signal_strength_all_low_20260502"
    severity: str            # P0/P1/P2
    approval_level: str      # L0/L1/L2/L3
    timestamp: str = ""
    version_hash: str = ""
    
    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()
        raw = f"{self.param_path}:{self.new_value}:{self.timestamp}"
        self.version_hash = hashlib.md5(raw.encode()).hexdigest()[:12]


# ============================================================================
#  ConfigWriter
# ============================================================================

class ConfigWriter:
    """安全地写入统一配置文件"""
    
    # US 版配置文件路径
    CONFIG_PATH = str(get_config_path())
    AUDIT_LOG_PATH = str(get_data_dir() / "config_audit_log.json")
    
    # 参数映射 (ActionExecutor 键名 → YAML dotted path → 安全边界)
    # F27 (2026-05-14): 扩白名单 — 加 risk_unit_R / day_trade TP / chandelier / portfolio drawdown
    # 每条新增项必须有 min/max/max_delta safety bound, 否则不许加
    # yaml_file 字段缺省为 unified_config.yaml; 写 exit_policy.yaml 等需显式指定
    PARAM_MAP = {
        # ── 现有 4 项 (保持不变) ──
        "MIN_CONFIDENCE": {
            "yaml_path": "risk.signal_quality.min_confidence",
            "min": 0.3,
            "max": 0.8,
            "max_delta": 0.1,
        },
        "SIGNAL_MIN_SCORE": {
            "yaml_path": "risk.signal_quality.min_score",
            "min": 30,
            "max": 80,
            "max_delta": 10,
        },
        "MAX_POSITION_PER_SYMBOL": {
            "yaml_path": "risk.position.max_per_symbol",
            "min": 0.1,
            "max": 0.4,
            "max_delta": 0.05,
        },
        "cooldown_hours": {
            "yaml_path": "decision.signal_timing.cooldown_hours",
            "min": 1,
            "max": 72,
            "max_delta": 12,
        },
        # ── F27 (2026-05-14) 新增: 风险预算 ──
        "PER_TRADE_RISK_PCT": {
            "yaml_path": "risk.per_trade_risk_pct",
            "min": 0.002,        # 0.2% — 最低风险, 避免不交易
            "max": 0.015,        # 1.5% — 上限保护
            "max_delta": 0.002,  # 单次调整 0.2%, 避免过快变化
        },
        "MAX_TRADE_RISK_PCT": {
            "yaml_path": "risk.max_trade_risk_pct",
            "min": 0.005,
            "max": 0.020,
            "max_delta": 0.003,
        },
        # ── F27 新增: 组合层面止损 (F17 配套) ──
        "PORTFOLIO_WARN_DRAWDOWN_PCT": {
            "yaml_path": "risk.portfolio.warn_drawdown_pct",
            "min": 0.01, "max": 0.04, "max_delta": 0.005,
        },
        "PORTFOLIO_REDUCE_DRAWDOWN_PCT": {
            "yaml_path": "risk.portfolio.reduce_drawdown_pct",
            "min": 0.015, "max": 0.04, "max_delta": 0.005,
        },
        "PORTFOLIO_STOP_DRAWDOWN_PCT": {
            "yaml_path": "risk.portfolio.stop_drawdown_pct",
            "min": 0.02, "max": 0.05, "max_delta": 0.005,
        },
        # ── F27 新增: Day Trade Mode TP (写 exit_policy.yaml) ──
        "DAYTRADE_TP1_PCT": {
            "yaml_file": "exit_policy",
            "yaml_path": "day_trade_mode.take_profit.tier_1.profit_pct",
            "min": 2.0, "max": 8.0, "max_delta": 1.0,
        },
        "DAYTRADE_TP2_PCT": {
            "yaml_file": "exit_policy",
            "yaml_path": "day_trade_mode.take_profit.tier_2.profit_pct",
            "min": 5.0, "max": 15.0, "max_delta": 2.0,
        },
        "DAYTRADE_TP3_PCT": {
            "yaml_file": "exit_policy",
            "yaml_path": "day_trade_mode.take_profit.tier_3.profit_pct",
            "min": 10.0, "max": 25.0, "max_delta": 3.0,
        },
        "DAYTRADE_INITIAL_STOP_ATR": {
            "yaml_file": "exit_policy",
            "yaml_path": "day_trade_mode.initial_stop.atr_multiplier",
            "min": 1.0, "max": 3.0, "max_delta": 0.3,
        },
        "DAYTRADE_MAX_LOSS_PCT": {
            "yaml_file": "exit_policy",
            "yaml_path": "day_trade_mode.initial_stop.max_loss_pct",
            "min": 1.5, "max": 6.0, "max_delta": 0.5,
        },
        # ── F27 新增: Chandelier 追踪 (F14 配套, 让 trail 更早接管 PRE_CLOSE) ──
        "CHANDELIER_ACTIVATION_ATR": {
            "yaml_file": "exit_policy",
            "yaml_path": "exit_rules.chandelier_trail.activation_profit_atr",
            "min": 0.5, "max": 2.5, "max_delta": 0.3,
        },
        "CHANDELIER_TRAIL_ATR_MULT": {
            "yaml_file": "exit_policy",
            "yaml_path": "exit_rules.chandelier_trail.trail_atr_multiplier",
            "min": 1.5, "max": 4.0, "max_delta": 0.5,
        },
    }

    # yaml_file 别名 → 实际路径
    @staticmethod
    def _resolve_yaml_file(alias: str) -> str:
        if alias == "exit_policy":
            return str(get_exit_policy_path())
        # 默认 unified_config.yaml
        return str(get_config_path())
    
    def __init__(self, config_path: str = None, audit_path: str = None):
        self.config_path = config_path or self.CONFIG_PATH
        self.audit_path = audit_path or self.AUDIT_LOG_PATH
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.audit_path), exist_ok=True)
    
    def _read_config(self, path: Optional[str] = None) -> Dict:
        """读取 YAML 配置 (可指定文件 — F27 多 yaml 支持)"""
        try:
            import yaml
            target = path or self.config_path
            with open(target, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"[ConfigWriter] 读取配置失败 ({target}): {e}")
            raise
    
    def _get_nested(self, data: Dict, dotted_path: str) -> Any:
        """从嵌套字典获取值 (a.b.c → data['a']['b']['c'])"""
        keys = dotted_path.split('.')
        current = data
        for key in keys:
            if isinstance(current, dict):
                current = current.get(key)
            else:
                return None
        return current
    
    def _set_nested(self, data: Dict, dotted_path: str, value: Any):
        """设置嵌套字典的值"""
        keys = dotted_path.split('.')
        current = data
        for key in keys[:-1]:
            if key not in current:
                current[key] = {}
            current = current[key]
        current[keys[-1]] = value
    
    def _atomic_write(self, config: Dict, path: Optional[str] = None):
        """原子写入: 先写临时文件，再 rename 覆盖 (F27 支持多 yaml)"""
        try:
            import yaml
            target = path or self.config_path
            dir_name = os.path.dirname(target)
            fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.yaml.tmp')
            os.close(fd)
            try:
                with open(tmp_path, 'w', encoding='utf-8') as f:
                    yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
                shutil.move(tmp_path, target)
                logger.info(f"[ConfigWriter] 配置已原子写入: {target}")
            except (IOError, OSError, yaml.YAMLError):
                # 写入失败时清理临时文件
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
        except Exception as e:
            logger.error(f"[ConfigWriter] 原子写入失败 ({target}): {e}")
            raise
    
    def _validate_change(self, key: str, old_value: Any, new_value: Any):
        """安全校验"""
        param = self.PARAM_MAP.get(key)
        if not param:
            raise ValueError(f"[ConfigWriter] 参数 {key} 不在白名单中")
        
        # 边界校验
        if new_value < param["min"]:
            raise ValueError(
                f"[ConfigWriter] {key}: {new_value} 低于最小值 {param['min']}"
            )
        if new_value > param["max"]:
            raise ValueError(
                f"[ConfigWriter] {key}: {new_value} 超过最大值 {param['max']}"
            )
        
        # 变化幅度校验
        if old_value is not None:
            delta = abs(new_value - old_value)
            if delta > param["max_delta"]:
                raise ValueError(
                    f"[ConfigWriter] {key}: 变化幅度 {delta} 超过最大允许 {param['max_delta']}"
                )
    
    def update_param(
        self,
        key: str,           # ActionExecutor 键名
        new_value: Any,
        reason: str,
        severity: str = "P1",
        approval_level: str = "L1",
    ) -> ConfigChangeRecord:
        """更新配置参数 (线程安全)

        F27 (2026-05-14): 支持多 yaml — PARAM_MAP entry 的 yaml_file 决定写哪个文件。
        """
        with self._lock:
            param_info = self.PARAM_MAP.get(key)
            if not param_info:
                raise ValueError(f"[ConfigWriter] 参数 {key} 不在白名单中")

            dotted_path = param_info["yaml_path"]
            yaml_file_alias = param_info.get("yaml_file")  # None → unified_config.yaml
            target_path = self._resolve_yaml_file(yaml_file_alias) if yaml_file_alias else self.config_path

            # 1. 读取当前配置 (从对应文件)
            config = self._read_config(target_path)
            old_value = self._get_nested(config, dotted_path)

            # 2. 安全校验
            self._validate_change(key, old_value, new_value)

            # 3. 更新内存
            self._set_nested(config, dotted_path, new_value)

            # 4. 原子写入 (写到对应文件)
            self._atomic_write(config, target_path)

            # 5. 记录审计 (含 yaml_file 标记)
            record = ConfigChangeRecord(
                param_path=f"[{yaml_file_alias or 'main'}] {dotted_path}",
                old_value=old_value,
                new_value=new_value,
                reason=reason,
                severity=severity,
                approval_level=approval_level,
            )
            self._write_audit(record)

            # 6. 热加载到内存 (只对 unified_config.yaml 生效, exit_policy 由 ExitPolicyGuardian 自查)
            if not yaml_file_alias:
                self._trigger_reload()

            logger.info(
                f"[ConfigWriter] 参数更新: [{yaml_file_alias or 'main'}] {dotted_path} "
                f"{old_value} → {new_value} (reason: {reason})"
            )

            return record
    
    def rollback_param(self, dotted_path: str) -> bool:
        """回滚配置到上一个版本"""
        with self._lock:
            history = self._read_audit_log()
            param_history = [e for e in history if e.get("param_path") == dotted_path]
            
            if len(param_history) < 2:
                logger.warning(f"[ConfigWriter] 无法回滚 {dotted_path}: 无历史版本")
                return False
            
            # 取上一个版本
            last = param_history[-1]
            prev = param_history[-2]
            target_value = prev.get("old_value")
            
            if target_value is None:
                logger.warning(f"[ConfigWriter] 无法回滚 {dotted_path}: 上一个版本无值")
                return False
            
            # 找到对应的 key
            key = None
            for k, v in self.PARAM_MAP.items():
                if v["yaml_path"] == dotted_path:
                    key = k
                    break
            
            if not key:
                # 尝试从路径反推 key
                key = dotted_path.split(".")[-1]
            
            # 执行回滚
            record = self.update_param(
                key=key,
                new_value=target_value,
                reason=f"rollback: {last.get('reason', 'unknown')}",
                severity="P0",
                approval_level="L0",
            )
            
            self._write_rollback_log({
                "param_path": dotted_path,
                "from": last.get("new_value"),
                "to": target_value,
                "reason": last.get("reason", "unknown"),
                "version_hash": record.version_hash,
                "timestamp": datetime.now().isoformat(),
            })
            
            logger.info(f"[ConfigWriter] 回滚成功: {dotted_path} → {target_value}")
            return True
    
    def _write_audit(self, record: ConfigChangeRecord):
        """写入审计日志"""
        entries = self._read_audit_log()
        entries.append(asdict(record))
        
        # 限制日志大小 (保留最近 500 条)
        if len(entries) > 500:
            entries = entries[-500:]
        
        with open(self.audit_path, 'w', encoding='utf-8') as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)
    
    def _read_audit_log(self) -> List[Dict]:
        """读取审计日志"""
        if os.path.exists(self.audit_path):
            try:
                with open(self.audit_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError, UnicodeDecodeError):
                pass
        return []
    
    def _write_rollback_log(self, entry: Dict):
        """写入回滚日志"""
        rollback_path = self.audit_path.replace("config_audit", "config_rollback")
        entries = []
        if os.path.exists(rollback_path):
            try:
                with open(rollback_path, 'r', encoding='utf-8') as f:
                    entries = json.load(f)
            except (json.JSONDecodeError, IOError, UnicodeDecodeError):
                pass
        entries.append(entry)
        with open(rollback_path, 'w', encoding='utf-8') as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)
    
    def _trigger_reload(self):
        """触发配置热加载"""
        try:
            from app.core.config_container import get_config

            cfg = get_config()
            if hasattr(cfg, 'reload'):
                cfg.reload()
                logger.info("[ConfigWriter] ConfigContainer 已热加载")
            else:
                logger.debug("[ConfigWriter] ConfigContainer 无 reload 方法，跳过")
        except Exception as e:
            logger.debug(f"[ConfigWriter] 热加载失败 (不影响写入): {e}")
    
    def get_param_history(self, dotted_path: str) -> List[Dict]:
        """获取参数修改历史"""
        entries = self._read_audit_log()
        return [e for e in entries if e.get("param_path") == dotted_path]


# ============================================================================
#  模块级单例
# ============================================================================

_config_writer: Optional[ConfigWriter] = None


def get_config_writer() -> ConfigWriter:
    global _config_writer
    if _config_writer is None:
        _config_writer = ConfigWriter()
    return _config_writer
