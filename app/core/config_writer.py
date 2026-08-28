"""
FTSeek-US 配置持久化写入器 — 唯一允许修改 unified_config.yaml 的模块

安全特性:
- 白名单参数控制 (只允许修改预定义的参数)
- 安全边界校验 (min/max/max_delta)
- 无人值守写盘默认拒绝 (需人工确认, 见 _auto_apply_confirmed)
- 注释保真的最小改动写入 (只改值变的行 + 写前校验 + os.replace)
- 完整审计轨迹 (config_audit_log.json)
- 版本哈希追踪 (支持回滚)
- 热加载触发 (ConfigContainer reload)
"""
import json
import logging
import os
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
#  异常
# ============================================================================

class ConfigWriteRefused(RuntimeError):
    """无法安全落点时拒绝写盘 (fail-closed) — 宁可不改, 也不毁配置文件的注释/结构."""


class AutoWriteNotConfirmed(RuntimeError):
    """自调器 (review_loop / LLM 提案) 未经人工确认的写盘请求被拒 — 2026-08-28 用户裁决."""


# ============================================================================
#  注释保真的行级编辑 (2026-08-28 全链路复查)
# ============================================================================
#  旧 _atomic_write 用 yaml.dump(整本 dict) 覆盖 unified_config.yaml:
#    · 326 行决策注释全灭 —— 那正是本仓记"为什么这么调/何时回滚"的地方 (git 历史里
#      82ab6d58"注释清理"实为 dump 事后找补, 且文件至今只剩 2 个空行 = dump 指纹);
#    · 整文件重排 → 真实变更 (一行) 埋在 2800 行 diff 里, 无法审阅。
#  现改为"叶子差异 + 原行标量替换": 只有值变的那一行被改写, 其余字节 (含注释/空行/
#  引号风格/键序) 不动; 定位不到或写后校验不符 → ConfigWriteRefused, 原文件保持不变。

def _format_scalar(value: Any) -> str:
    """把 python 标量渲染成 yaml 行内值 (与 dump 同源, 避免 True/true 漂移)."""
    import yaml
    text = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True, width=10**6).strip()
    # safe_dump 对标量输出形如 "0.8\n...\n" 或 "- 0.8\n"; 单标量取首行去噪
    if text.startswith("- "):
        text = text[2:]
    return text.split("\n", 1)[0].strip()


def _index_mapping_lines(text: str) -> Dict[str, int]:
    """dotted path → 行号 (仅映射键; 跳过注释/块标量正文/序列项).

    只服务 PARAM_MAP 这类纯映射路径; 数组元素路径不索引 → 上层判缺失并拒绝写.
    """
    index: Dict[str, int] = {}
    stack: List[tuple] = []  # [(indent, key)]
    lines = text.split("\n")
    i, n = 0, len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if stripped.startswith("- "):
            i += 1
            continue
        key, sep, value = stripped.partition(":")
        if not sep:
            i += 1
            continue
        key = key.strip().strip("'\"")
        value = value.strip()
        path = ".".join([k for _, k in stack] + [key])
        index.setdefault(path, i)
        stack.append((indent, key))
        if value[:1] in ("|", ">"):
            # 块标量正文: 吞掉更深缩进的后续行, 防其内容被误当键索引
            stack.pop()
            i += 1
            while i < n:
                nxt = lines[i]
                if not nxt.strip():
                    i += 1
                    continue
                if (len(nxt) - len(nxt.lstrip(" "))) > indent:
                    i += 1
                    continue
                break
            continue
        i += 1
    return index


def _split_inline_comment(tail: str) -> tuple:
    """' 0.7   # 说明' → ('0.7', '   ', '# 说明'); 引号内的 # 不算注释起点.

    返回 (值, 值与注释间的空白, 注释) —— 空白原样保留才使 diff 只有值本身变化。
    """
    quote = ""
    for pos, ch in enumerate(tail):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (pos == 0 or tail[pos - 1] in " \t"):
            head = tail[:pos]
            gap = head[len(head.rstrip()):] or " "
            return head.strip(), gap, tail[pos:].strip()
    return tail.strip(), "", ""


def _apply_scalar_edit(text: str, dotted_path: str, new_value: Any) -> str:
    """把 dotted_path 对应的标量值就地换成 new_value, 其余字节原样返回."""
    lines = text.split("\n")
    idx = _index_mapping_lines(text).get(dotted_path)
    if idx is None:
        raise ConfigWriteRefused(
            f"无法在 {dotted_path!r} 定位到标量键 (路径不存在或藏在数组内) — 拒绝整本重写"
        )
    line = lines[idx]
    indent = line[: len(line) - len(line.lstrip(" "))]
    key_part, _, tail = line.strip().partition(":")
    old_value_text, gap, comment = _split_inline_comment(tail)
    if not old_value_text:
        raise ConfigWriteRefused(
            f"{dotted_path!r} 不是标量键 (值是子映射/多行块) — 拒绝外科式写入"
        )
    new_line = f"{indent}{key_part}: {_format_scalar(new_value)}"
    if comment:
        new_line += f"{gap}{comment}"
    lines[idx] = new_line
    return "\n".join(lines)


def _leaf_diffs(old: Any, new: Any, prefix: str = "") -> List[tuple]:
    """收集 old→new 的叶子差异 [(dotted_path, new_value)].

    结构变化 (增删键 / 标量↔映射 / 数组) 返回 (None, ...) 标记, 由调用方拒绝写。
    """
    diffs: List[tuple] = []
    if isinstance(old, dict) and isinstance(new, dict):
        extra = set(new) - set(old) or set(old) - set(new)
        if extra:
            return [(None, f"键集合变化: {sorted(extra)[:3]}")]
        for k in old:
            child = f"{prefix}.{k}" if prefix else str(k)
            diffs.extend(_leaf_diffs(old[k], new[k], child))
        return diffs
    if old == new:
        return []
    if isinstance(old, dict) or isinstance(new, dict) or isinstance(old, list) or isinstance(new, list):
        return [(None, f"{prefix}: 结构类型变化, 无法按行替换")]
    return [(prefix, new)]


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
            # 2026-08-28 修路径: 原写 decision.signal_timing.cooldown_hours — 该路径在
            # unified_config.yaml 中不存在, 消费点实为 order_executor._apply_symbol_cooldown
            # 读 symbol_cooldown.structure_exit.cooldown_hours。旧 dump 写法会凭空造一条
            # 无人读的死键 (参数"改成功"而冷却时长不变); 新行级写入会因定位不到直接拒绝。
            "yaml_path": "symbol_cooldown.structure_exit.cooldown_hours",
            "min": 1,
            "max": 72,
            "max_delta": 12,
        },
        # ── F27 (2026-05-14) 新增: 风险预算 ──
        # PER_TRADE_RISK_PCT 已删除 (2026-06-20): risk.per_trade_risk_pct 是死路径
        # (yaml 注释为 "已删除", SoT 是 risk.position_sizing.risk_per_trade_pct)。
        # auto-tuner 若提议会写入死路径, 故移除 PARAM_MAP 项。
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
        # ── F27 Day Trade Mode TP 调参已删除 (2026-06-20): day_trade 规则代码已移除,
        #    auto-tuner 不应再调 disabled 死配置。day_trade_mode yaml 块保留 (time_decay 依赖)。
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
        """原子写入: 只改「值变了的那几行」, 其余字节 (含注释) 原样保留.

        2026-08-28 (全链路复查): 旧实现 `yaml.dump(整本 dict)` 覆盖 unified_config.yaml,
        把 326 行决策注释 (为什么这么调 / 何时回滚) 与空行/引号风格一并抹掉, 且把一行
        真实变更埋进 2800 行 diff 里无法审阅; 更糟的是值没变 (auto_fix 反复提同一值,
        审计日志 8/6-8/15 四次 old==new) 也照写一遍 → 每次都是一次全文件毁注释。
        现在: 叶子差异 → 行级标量替换 → 写前校验 → os.replace。任何一步不成立都
        ConfigWriteRefused (原文件字节不动), 宁可不改。
        """
        import yaml

        target = path or self.config_path
        try:
            with open(target, "r", encoding="utf-8") as f:
                original_text = f.read()
        except OSError as e:
            raise ConfigWriteRefused(f"[ConfigWriter] 读取 {target} 失败, 拒绝写入: {e}")

        try:
            original = yaml.safe_load(original_text) or {}
        except yaml.YAMLError as e:
            raise ConfigWriteRefused(f"[ConfigWriter] {target} 解析失败, 拒绝覆盖疑似损坏文件: {e}")

        diffs = _leaf_diffs(original, config)
        structural = [msg for key, msg in diffs if key is None]
        if structural:
            raise ConfigWriteRefused(
                f"[ConfigWriter] 非叶子级变更, 无法在保注释的前提下落盘: {'; '.join(structural[:3])}"
            )
        if not diffs:
            logger.info("[ConfigWriter] 目标值与现值一致, 跳过写盘 (文件字节不变)")
            return

        new_text = original_text
        for dotted, new_value in diffs:
            new_text = _apply_scalar_edit(new_text, dotted, new_value)

        try:
            reparsed = yaml.safe_load(new_text) or {}
        except yaml.YAMLError as e:
            raise ConfigWriteRefused(f"[ConfigWriter] 写前校验失败 (新文本不可解析): {e} — 原文件未改动")
        if reparsed != config:
            changed = ", ".join(str(d[0]) for d in diffs)
            raise ConfigWriteRefused(
                f"[ConfigWriter] 写前校验失败 (落盘结果与期望不符, 变更键: {changed}) — 原文件未改动"
            )

        dir_name = os.path.dirname(target) or "."
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".yaml.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_text)
            os.replace(tmp_path, target)
            logger.info(
                "[ConfigWriter] 配置已原子写入: %s | 改动键: %s (其余行含注释字节不变)",
                target,
                ", ".join(f"{d[0]}={d[1]!r}" for d in diffs),
            )
        except (IOError, OSError) as e:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise ConfigWriteRefused(f"[ConfigWriter] 临时文件写入失败 ({target}): {e}")
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise
    
    def _auto_apply_confirmed(self) -> bool:
        """自调器 (无人值守) 写盘是否已获放行 — 默认 False (fail-closed).

        2026-08-28 用户裁决: "自调器不做, 需要实战人工确认才行"。参数自调此前是
        L1 = "自动执行+通知", 实际等于改 SSOT 不打扰任何人 (审计日志: 7/24 曾把
        min_confidence 0.7→0.8 直接落盘)。现默认关闭该通道, 提案改由日报呈现,
        人决定后自己改 yaml。确要恢复自动写盘: deployment.feature_flags.
        config_writer_auto_apply: true (人工加, 缺键=关)。
        """
        try:
            from app.core.feature_flags import get_feature_flags
            return bool(get_feature_flags().is_enabled("config_writer_auto_apply"))
        except Exception as e:
            logger.warning(f"[ConfigWriter] auto_apply 开关读取失败, 按未放行处理: {e}")
            return False

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
        2026-08-28: 非 L0 (人工/回滚) 的写盘默认拒绝, 见 _auto_apply_confirmed。
        """
        with self._lock:
            param_info = self.PARAM_MAP.get(key)
            if not param_info:
                raise ValueError(f"[ConfigWriter] 参数 {key} 不在白名单中")

            # 人工确认门 (2026-08-28 用户裁决"自调器不做, 需实战人工确认"):
            #   L0 = 显式人工路径 (rollback_param) → 放行;
            #   L1/L2/L3 = review_loop auto_fix / LLM 提案的无人值守调用 → 拒绝, 提案留在
            #   WARNING 日志与 ActionResult(success=False) 里给人看, 人自己改 yaml。
            if approval_level != "L0" and not self._auto_apply_confirmed():
                logger.warning(
                    "🚫 [ConfigWriter·需人工确认] 拒写 %s (approval=%s reason=%s) — "
                    "自调器写盘通道默认关闭, 恢复见 deployment.feature_flags.config_writer_auto_apply",
                    param_info["yaml_path"], approval_level, reason[:120],
                )
                raise AutoWriteNotConfirmed(
                    f"{param_info['yaml_path']} → {new_value!r} 未获人工确认, 拒绝写盘 ({reason[:80]})"
                )

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
