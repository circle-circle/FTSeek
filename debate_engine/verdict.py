"""
辩论结论 + 注入通道

DebateVerdict: 结构化辩论结论
Injection: 可执行的配置注入指令
InjectionApplier: Overlay 模式注入器
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class Injection:
    """可执行的配置注入指令"""
    injection_id: str
    target: str                             # "factor:SSS" | "exit:stable_15m" | "filter:global"
    action: str                             # "override_param" | "add_filter" | "remove_filter"
    params: Dict[str, Any]                  # 具体参数变更
    confidence: float                       # 辩论共识置信度
    market: str                             # "US" | "HK" | "BOTH"
    source_debate_id: str
    status: str = "pending"                 # pending / applied / reverted
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Injection:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class DebateVerdict:
    """辩论最终结论 — 结构化 + 可执行"""
    debate_id: str
    topic_id: str
    topic_type: str
    market: str
    total_rounds: int
    consensus_score: float
    stop_reason: str                        # converged / divergence / max_rounds
    timestamp: str = ""
    
    consensus_items: List[Dict] = field(default_factory=list)
    disagreement_items: List[Dict] = field(default_factory=list)
    blind_spots: List[str] = field(default_factory=list)
    injections: List[Injection] = field(default_factory=list)
    action_items: List[Dict] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["injections"] = [i.to_dict() if hasattr(i, "to_dict") else i for i in self.injections]
        return d
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DebateVerdict:
        injections = [Injection.from_dict(i) if isinstance(i, dict) else i 
                      for i in data.get("injections", [])]
        data_copy = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        data_copy["injections"] = injections
        return cls(**data_copy)


class InjectionApplier:
    """
    注入执行器 — Overlay 模式
    
    不直接修改基线配置文件，而是写入 override 文件。
    Registry 在加载配置时自动 merge baseline + overrides。
    
    config/overrides/
        debate_20260523_factor_SSS.yaml
        debate_20260523_exit_stable_15m.yaml
    """
    
    def __init__(self, overrides_dir: Optional[str] = None):
        if overrides_dir is None:
            overrides_dir = str(Path(__file__).resolve().parent.parent / "config" / "overrides")
        self.overrides_dir = Path(overrides_dir)
        self.overrides_dir.mkdir(parents=True, exist_ok=True)
    
    def apply(self, injection: Injection) -> str:
        """
        将注入写入 override YAML 文件
        
        Returns: 写入的文件路径
        """
        safe_target = injection.target.replace(":", "_")
        filename = f"{injection.source_debate_id}_{safe_target}.yaml"
        filepath = self.overrides_dir / filename
        
        data = {
            "override": {
                "target": injection.target,
                "action": injection.action,
                "params": injection.params,
                "confidence": injection.confidence,
                "market": injection.market,
            },
            "source": {
                "debate_id": injection.source_debate_id,
                "injection_id": injection.injection_id,
                "timestamp": datetime.now().isoformat(),
            },
            "rollback": {
                "method": f"delete {filename}",
            },
        }
        
        with open(filepath, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
        
        logger.info(f"Applied injection: {injection.injection_id} -> {filepath}")
        return str(filepath)
    
    def revert(self, injection_id: str) -> bool:
        """回滚指定注入"""
        for f in self.overrides_dir.glob("*.yaml"):
            try:
                with open(f) as fh:
                    data = yaml.safe_load(fh)
                if data.get("source", {}).get("injection_id") == injection_id:
                    f.unlink()
                    logger.info(f"Reverted injection: {injection_id}")
                    return True
            except Exception:
                continue
        return False
    
    def revert_all(self, debate_id: str) -> int:
        """回滚某次辩论的所有注入"""
        count = 0
        for f in self.overrides_dir.glob(f"{debate_id}_*.yaml"):
            f.unlink()
            count += 1
        if count:
            logger.info(f"Reverted {count} injections for debate: {debate_id}")
        return count
    
    def list_active(self) -> List[Dict[str, Any]]:
        """列出当前生效的所有 override"""
        active = []
        for f in sorted(self.overrides_dir.glob("*.yaml")):
            try:
                with open(f) as fh:
                    data = yaml.safe_load(fh)
                active.append({
                    "file": f.name,
                    "target": data.get("override", {}).get("target", ""),
                    "action": data.get("override", {}).get("action", ""),
                    "debate_id": data.get("source", {}).get("debate_id", ""),
                    "confidence": data.get("override", {}).get("confidence", 0),
                })
            except Exception:
                continue
        return active
