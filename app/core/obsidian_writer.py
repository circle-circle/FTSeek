#!/usr/bin/env python3
"""
FTSeek Obsidian 知识库写入器 (2026-05-04 Phase 1)

职责: 将交易/决策/归因数据同步写入 Obsidian vault (双写模式)
目标: 实现 debate → trade → attribute → learn → preview → evolve → repeat 闭环

架构:
  trades/        - 单笔交易笔记 (按 日期_代码_ID 分类)
  decisions/     - 盘前/盘中决策笔记
  attribution/   - 归因分析笔记
  lessons/       - 经验教训笔记 (按主题/市场分类)
  strategies/    - 策略笔记 (含演化历史)
  daily/         - 日报笔记 (premarket + postmarket)
"""

import os
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger("ftseek.obsidian_writer")


class ObsidianWriter:
    """Obsidian 知识库写入器 — 双写模式，与现有 DB/JSON 共存"""

    # Vault 根目录
    DEFAULT_VAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "obsidian_vault")

    # 笔记类型目录
    _DIRS = {
        "trade": "trades",
        "decision": "decisions",
        "attribution": "attribution",
        "lesson": "lessons",
        "strategy": "strategies",
        "daily": "daily",
    }

    def __init__(self, vault_path: str = None):
        self.vault = vault_path or self.DEFAULT_VAULT_PATH
        self._init_dirs()
        logger.info(f"📓 ObsidianWriter 已初始化: {self.vault}")

    def _init_dirs(self):
        """初始化所有分类目录"""
        for sub in self._DIRS.values():
            os.makedirs(os.path.join(self.vault, sub), exist_ok=True)
        # 日报子目录
        os.makedirs(os.path.join(self.vault, "daily", "premarket"), exist_ok=True)
        os.makedirs(os.path.join(self.vault, "daily", "postmarket"), exist_ok=True)
        # 决策子目录
        os.makedirs(os.path.join(self.vault, "decisions", "premarket"), exist_ok=True)
        os.makedirs(os.path.join(self.vault, "decisions", "intraday"), exist_ok=True)

    def _write_md(self, directory: str, filename: str, content: str, overwrite: bool = False) -> str:
        """安全写入 Markdown 文件"""
        dir_path = os.path.join(self.vault, directory)
        os.makedirs(dir_path, exist_ok=True)
        filepath = os.path.join(dir_path, filename)

        if os.path.exists(filepath) and not overwrite:
            logger.debug(f"[Obsidian] 文件已存在，跳过: {filepath}")
            return filepath

        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(content)
            logger.info(f"[Obsidian] 已写入: {filepath}")
            return filepath
        except Exception as e:
            logger.error(f"[Obsidian] 写入失败: {filepath} - {e}")
            return None

    def _fm(self, **kwargs) -> str:
        """生成 YAML frontmatter"""
        lines = ["---"]
        for k, v in kwargs.items():
            if v is None:
                lines.append(f"{k}:")
            elif isinstance(v, bool):
                lines.append(f"{k}: {'true' if v else 'false'}")
            elif isinstance(v, (int, float)):
                lines.append(f"{k}: {v}")
            elif isinstance(v, list):
                items = ", ".join(str(x) for x in v)
                lines.append(f"{k}: [{items}]")
            else:
                # 转义特殊字符
                val = str(v).replace('"', '\\"')
                lines.append(f'{k}: "{val}"')
        lines.append("---")
        return "\n".join(lines)

    # ============================================================
    # 1. Trade 笔记 — 单笔交易记录
    # ============================================================

    def write_trade(self, data: Dict) -> Optional[str]:
        """
        写入交易笔记

        Args:
            data: {
                'id': str, 'code': str, 'market': 'HK'|'US',
                'entry_time': str, 'exit_time': str|None,
                'entry_price': float, 'exit_price': float|None,
                'quantity': int, 'pnl': float,
                'strategy': str, 'action': 'BUY'|'SELL',
                'exit_reason': str|None, 'tags': list|None,
                'trace_id': str|None, 'signal_id': str|None,
                'stop_loss': float|None, 'take_profit': float|None,
                'highest_price': float|None, 'lowest_price': float|None,
            }
        """
        code = data.get('code', 'UNKNOWN')
        market = data.get('market', 'HK')
        entry_time = data.get('entry_time', datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        trade_id = data.get('id', 'unknown')
        date_str = entry_time[:10]

        filename = f"{date_str}_{code.replace('.', '_')}_{trade_id}.md"
        tags = data.get('tags', [])
        if not tags:
            tags = [market.lower(), data.get('action', 'unknown').lower(), data.get('strategy', 'unknown')]

        fm = self._fm(
            type="trade",
            symbol=code,
            market=market,
            date=date_str,
            entry_price=data.get('entry_price'),
            exit_price=data.get('exit_price'),
            pnl=round(data.get('pnl', 0), 2),
            pnl_pct=round(data.get('pnl_pct', 0), 2),
            strategy=data.get('strategy', 'unknown'),
            action=data.get('action', 'unknown'),
            exit_reason=data.get('exit_reason', ''),
            tags=tags,
            trace_id=data.get('trace_id', ''),
            status="closed" if data.get('exit_time') else "open",
        )

        content = f"""{fm}

# 交易: {code}

## 基本信息
| 字段 | 值 |
|------|---|
| 市场 | {market} |
| 方向 | {data.get('action', 'unknown')} |
| 数量 | {data.get('quantity', 0)} 股 |
| 策略 | {data.get('strategy', 'unknown')} |

## 入场
- **时间**: {entry_time}
- **价格**: {data.get('entry_price', 'N/A')}
- **止损**: {data.get('stop_loss', 'N/A')}
- **止盈**: {data.get('take_profit', 'N/A')}

## 出场
- **时间**: {data.get('exit_time', '持仓中')}
- **价格**: {data.get('exit_price', 'N/A')}
- **原因**: {data.get('exit_reason', 'N/A')}
- **盈亏**: {data.get('pnl', 0):+.2f} ({data.get('pnl_pct', 0):+.2%})
- **最高**: {data.get('highest_price', 'N/A')}
- **最低**: {data.get('lowest_price', 'N/A')}

## 归因
<!-- Agent 自动生成 -->

## 关联
- 决策: 
- 教训: 
"""
        return self._write_md("trades", filename, content)

    # ============================================================
    # 2. Decision 笔记 — 盘前/盘中决策
    # ============================================================

    def write_decision(self, data: Dict) -> Optional[str]:
        """
        写入决策笔记

        Args:
            data: {
                'code': str, 'market': str, 'date': str,
                'decision': 'BUY'|'SELL'|'HOLD'|'SKIP',
                'confidence': float, 'score': float,
                'models': dict, 'reason': str,
                'regime': str, 'strategy': str,
                'trace_id': str|None,
            }
        """
        code = data.get('code', 'UNKNOWN')
        date_str = data.get('date', datetime.now().strftime("%Y-%m-%d"))
        filename = f"{date_str}_{code.replace('.', '_')}_decision.md"

        models = data.get('models', {})
        model_votes = "\n".join(f"- {k}: {v}" for k, v in models.items()) if models else "- 无"

        fm = self._fm(
            type="decision",
            symbol=code,
            market=data.get('market', 'HK'),
            date=date_str,
            decision=data.get('decision', 'unknown'),
            confidence=data.get('confidence', 0),
            score=data.get('score', 0),
            regime=data.get('regime', 'unknown'),
            strategy=data.get('strategy', 'unknown'),
            tags=["decision", data.get('market', 'HK').lower()],
        )

        content = f"""{fm}

# 决策: {code} ({date_str})

## 决策结果
- **决定**: {data.get('decision', 'unknown')}
- **置信度**: {data.get('confidence', 0):.2f}
- **综合得分**: {data.get('score', 0):.2f}
- **市场状态**: {data.get('regime', 'unknown')}

## 模型投票
{model_votes}

## 决策依据
{data.get('reason', '无')}

## 关联
- 交易: 
- 归因: 
"""
        return self._write_md("decisions", filename, content)

    # ============================================================
    # 3. Attribution 笔记 — 归因分析
    # ============================================================

    def write_attribution(self, data: Dict) -> Optional[str]:
        """
        写入归因分析笔记

        Args:
            data: {
                'code': str, 'market': str, 'date': str,
                'pnl': float, 'pnl_category': str,
                'direction_correct': bool, 'strategy_fit': int|None,
                'success_factors': list|str,
                'failure_factors': list|str,
                'overall_score': float|None,
                'exit_reason': str|None,
                'trace_id': str|None,
            }
        """
        code = data.get('code', 'UNKNOWN')
        date_str = data.get('date', datetime.now().strftime("%Y-%m-%d"))
        filename = f"{date_str}_{code.replace('.', '_')}_attribution.md"

        success = data.get('success_factors', '')
        failure = data.get('failure_factors', '')

        fm = self._fm(
            type="attribution",
            symbol=code,
            market=data.get('market', 'HK'),
            date=date_str,
            pnl=round(data.get('pnl', 0), 2),
            pnl_category=data.get('pnl_category', ''),
            direction_correct=data.get('direction_correct', False),
            strategy_fit=data.get('strategy_fit'),
            overall_score=data.get('overall_score'),
            exit_reason=data.get('exit_reason', ''),
            tags=["attribution", data.get('pnl_category', 'unknown')],
        )

        content = f"""{fm}

# 归因: {code} ({date_str})

## 交易结果
- **盈亏**: {data.get('pnl', 0):+.2f}
- **类别**: {data.get('pnl_category', 'N/A')}
- **方向正确**: {'✅' if data.get('direction_correct') else '❌'}
- **策略契合度**: {data.get('strategy_fit', 'N/A')}
- **综合评分**: {data.get('overall_score', 'N/A')}
- **退出原因**: {data.get('exit_reason', 'N/A')}

## 成功因素
{success if success else '- 无'}

## 失败因素
{failure if failure else '- 无'}

## 关联
- 交易: [[{date_str}_{code.replace('.', '_')}]]
- 教训: 
"""
        return self._write_md("attribution", filename, content)

    # ============================================================
    # 4. Lesson 笔记 — 经验教训
    # ============================================================

    def write_lesson(self, data: Dict, overwrite: bool = False) -> Optional[str]:
        """
        写入经验教训笔记

        Args:
            data: {
                'topic': str, 'title': str,
                'market': 'HK'|'US'|'BOTH',
                'date': str,
                'severity': 'P0'|'P1'|'P2'|'P3',
                'category': 'risk'|'execution'|'strategy'|'config'|'data',
                'description': str,
                'root_cause': str,
                'fix_applied': str,
                'prevention': str,
                'related_trades': list|None,
                'applied_to': list|None,
            }
        """
        topic = data.get('topic', 'unknown')
        date_str = data.get('date', datetime.now().strftime("%Y-%m-%d"))
        severity = data.get('severity', 'P3')
        filename = f"{date_str}_{topic.lower().replace(' ', '_')}_{severity}.md"

        related = data.get('related_trades', [])
        related_links = "\n".join(f"- [[{t}]]" for t in related) if related else "- 无"

        applied = data.get('applied_to', [])
        applied_links = "\n".join(f"- [[{a}]]" for a in applied) if applied else "- 无"

        fm = self._fm(
            type="lesson",
            topic=topic,
            market=data.get('market', 'HK'),
            date=date_str,
            severity=severity,
            category=data.get('category', 'unknown'),
            tags=["lesson", data.get('category', 'unknown'), data.get('market', 'HK').lower()],
            status="active",
        )

        content = f"""{fm}

# 教训: {data.get('title', topic)}

## 概述
- **严重程度**: {severity}
- **分类**: {data.get('category', 'unknown')}
- **市场**: {data.get('market', 'HK')}
- **日期**: {date_str}

## 问题描述
{data.get('description', '')}

## 根因分析
{data.get('root_cause', '')}

## 修复措施
{data.get('fix_applied', '')}

## 预防机制
{data.get('prevention', '')}

## 关联
### 相关交易
{related_links}

### 已应用于
{applied_links}
"""
        return self._write_md("lessons", filename, content, overwrite=overwrite)

    # ============================================================
    # 5. Strategy 笔记 — 策略文档
    # ============================================================

    def write_strategy(self, data: Dict, overwrite: bool = True) -> Optional[str]:
        """
        写入策略笔记

        Args:
            data: {
                'name': str, 'market': str,
                'enabled': bool,
                'win_rate': float|None,
                'total_signals': int|None,
                'total_trades': int|None,
                'avg_pnl': float|None,
                'description': str,
                'parameters': dict|None,
                'last_updated': str,
                'evolve_history': list|None,
            }
        """
        name = data.get('name', 'unknown').lower().replace(' ', '_')
        filename = f"{name}.md"

        params = data.get('parameters', {})
        params_str = "\n".join(f"- `{k}`: {v}" for k, v in params.items()) if params else "- 无"

        evolve = data.get('evolve_history', [])
        evolve_str = "\n".join(f"- {e}" for e in evolve) if evolve else "- 无历史演化记录"

        fm = self._fm(
            type="strategy",
            name=data.get('name', 'unknown'),
            market=data.get('market', 'HK'),
            enabled=data.get('enabled', True),
            win_rate=data.get('win_rate'),
            total_signals=data.get('total_signals'),
            total_trades=data.get('total_trades'),
            avg_pnl=data.get('avg_pnl'),
            tags=["strategy", data.get('market', 'HK').lower(), name],
            last_updated=data.get('last_updated', datetime.now().strftime("%Y-%m-%d")),
        )

        content = f"""{fm}

# 策略: {data.get('name', 'unknown')}

## 基本信息
- **市场**: {data.get('market', 'HK')}
- **状态**: {'✅ 启用' if data.get('enabled') else '❌ 禁用'}
- **胜率**: {data.get('win_rate', 'N/A')}
- **总信号**: {data.get('total_signals', 'N/A')}
- **总交易**: {data.get('total_trades', 'N/A')}
- **平均盈亏**: {data.get('avg_pnl', 'N/A')}

## 描述
{data.get('description', '')}

## 参数
{params_str}

## 演化历史
{evolve_str}

## 关联
- 决策: 
- 教训: 
"""
        return self._write_md("strategies", filename, content, overwrite=overwrite)

    # ============================================================
    # 6. Symbol 笔记 — per-symbol 深度分析归档 (持续积累)
    # ============================================================

    def write_symbol_analysis(self, analysis: Dict) -> Optional[str]:
        """
        写入 per-symbol 深度分析笔记 (追加模式, 持续积累)

        每次 deep_analysis 产出后追加一个日期 section 到 symbols/US.XXX.md,
        记录 verdict/bull_case/bear_case/execution_plan/causal_chain。
        frontmatter 同步刷新 latest 方向/评分/战绩。

        幂等: 同日同 decision_id 不重复追加。
        best-effort: 失败只 log warning 不抛异常。

        Args:
            analysis: deep_analysis 的完整 result dict, 包含:
                symbol, name, rating, confidence, score, action,
                conclusion, bull_reasons, bear_reasons, constraints,
                entry_plan, stop_loss, target, key_levels, risks,
                _llm_output (含 bull_case/bear_case/execution_plan/causal_chain),
                _ic_decision_id, snapshot, plates, data_quality
        """
        try:
            symbol = analysis.get("symbol", "UNKNOWN")
            safe_name = symbol.replace(".", "_")
            filename = f"{safe_name}.md"

            # -- 从 _llm_output 提取结构化论据 --
            llm_out = analysis.get("_llm_output", {})
            bull_cases = llm_out.get("bull_case", [])
            bear_cases = llm_out.get("bear_case", [])
            execution_plan = llm_out.get("execution_plan", {})
            causal_chain = llm_out.get("causal_chain", "")
            risks = llm_out.get("risks", [])

            # -- 日期 + decision_id (幂等键) --
            date_str = datetime.now().strftime("%Y-%m-%d")
            decision_id = analysis.get("_ic_decision_id", "")
            id_suffix = decision_id.split("-")[-1] if decision_id else datetime.now().strftime("%H%M%S")
            section_anchor = f"{date_str}-{id_suffix}"

            # -- 从 strategy_memory.db 读战绩 (best-effort) --
            stats = self._read_symbol_stats(symbol)

            # -- 构建 section 内容 --
            score = analysis.get("score", 0)
            rating = analysis.get("rating", "")
            confidence = analysis.get("confidence", "")
            action = analysis.get("action", "")
            conclusion = analysis.get("conclusion", "")
            name = analysis.get("name", "")

            snap = analysis.get("snapshot", {})
            price = snap.get("price", 0)

            # -- claim IC 缓存 (论据级预测力) --
            claim_ic = self._load_claim_ic()

            # bull/bear 论据 (带IC标注)
            bull_lines = []
            for i, r in enumerate(bull_cases[:5], 1):
                thesis = r.get("thesis", "")[:120]
                st = r.get("strength", "MEDIUM")
                src = r.get("source_type", "")
                ct = r.get("claim_type", "")
                ic_tag = self._claim_ic_tag(ct, claim_ic)
                bull_lines.append(f"  {i}. [{st}]{ic_tag} {thesis}" + (f" ({src})" if src else ""))
            if not bull_lines:
                for r in (analysis.get("bull_reasons") or [])[:5]:
                    bull_lines.append(f"  - [{r.get('strength','')}] {r.get('text','')[:120]}")

            bear_lines = []
            for i, r in enumerate(bear_cases[:5], 1):
                thesis = r.get("thesis", "")[:120]
                st = r.get("strength", "MEDIUM")
                src = r.get("source_type", "")
                ct = r.get("claim_type", "")
                ic_tag = self._claim_ic_tag(ct, claim_ic)
                bear_lines.append(f"  {i}. [{st}]{ic_tag} {thesis}" + (f" ({src})" if src else ""))
            if not bear_lines:
                for r in (analysis.get("bear_reasons") or [])[:5]:
                    bear_lines.append(f"  - [{r.get('strength','')}] {r.get('text','')[:120]}")

            # IC 汇总段
            ic_summary = self._build_ic_summary(bull_cases + bear_cases, claim_ic)

            # 执行计划
            entry_str = execution_plan.get("entry_strategy", analysis.get("entry_plan", ""))
            sl_str = execution_plan.get("stop_loss", analysis.get("stop_loss", ""))
            tp_str = execution_plan.get("take_profit", analysis.get("target", ""))

            # 关键价位
            kl = llm_out.get("key_levels", {})
            support_lines = ", ".join(str(s) for s in kl.get("support", [])) if kl else ""
            resistance_lines = ", ".join(str(r) for r in kl.get("resistance", [])) if kl else ""

            # 风险
            risk_lines = []
            for r in risks[:5]:
                if isinstance(r, dict):
                    risk_lines.append(f"  - [{r.get('severity','')}] {r.get('description','')[:100]}")
                else:
                    risk_lines.append(f"  - {str(r)[:100]}")
            if not risk_lines:
                risk_lines = [f"  - {r[:100]}" for r in (analysis.get("risks") or [])[:5]]

            # wikilink peers (同行业, 扫描已有symbol pages)
            peer_links = ""
            plates = analysis.get("plates", [])
            industry = ""
            if plates:
                industry = plates[0].get("name", "") if plates else ""
            if industry:
                peers = self._find_industry_peers(industry, safe_name)
                if peers:
                    peer_links = f"\n## 同行业 ({industry})\n" + "\n".join(f"- [[{p}]]" for p in peers)

            # 数据质量
            dq = analysis.get("data_quality", {})
            dq_str = f"数据源 {dq.get('ok',0)}/{dq.get('total',0)} ({dq.get('coverage_pct',0):.0f}%)"

            section = f"""
## {date_str} | {rating} | 信心{confidence} | {action}
<!-- id:{section_anchor} -->

> {conclusion}

- **标的**: {name} ({symbol}) | 现价: {price} | 评分: {score}/10
- **IC ID**: `{decision_id}`
- **数据质量**: {dq_str}

### 看多论据
{chr(10).join(bull_lines) if bull_lines else '- 无'}

### 看空论据
{chr(10).join(bear_lines) if bear_lines else '- 无'}

### 交易计划
- 入场: {entry_str or 'N/A'}
- 止损: {sl_str or 'N/A'}
- 止盈: {tp_str or 'N/A'}
- 支撑: {support_lines or 'N/A'} | 阻力: {resistance_lines or 'N/A'}

### 风险
{chr(10).join(risk_lines) if risk_lines else '- 无'}

### 因果链
{causal_chain or 'N/A'}

### 论据IC验证
{ic_summary}
{peer_links}
---
"""
            # -- 读现有文件 or 创建新文件 --
            filepath = os.path.join(self.vault, "symbols", filename)
            os.makedirs(os.path.join(self.vault, "symbols"), exist_ok=True)

            if os.path.exists(filepath):
                # 幂等: 检查同 section_anchor 是否已存在
                with open(filepath, "r", encoding="utf-8") as f:
                    existing = f.read()
                if section_anchor in existing:
                    logger.debug(f"[Obsidian] 跳过重复写入: {symbol} {section_anchor}")
                    return filepath

                # 追加 section + 更新 frontmatter
                # 追加 section 到文件末尾
                with open(filepath, "a", encoding="utf-8") as f:
                    f.write(section)
                # 更新 frontmatter (重写文件头部)
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                # 用更新后的 frontmatter 替换旧的
                content = self._replace_frontmatter(content, {
                    "latest_date": date_str,
                    "latest_direction": action,
                    "latest_score": score,
                    "latest_rating": rating,
                    "total_trades": stats.get("total_trades", 0),
                    "win_rate": stats.get("win_rate", 0),
                    "industry": industry or "",
                })
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
                logger.info(f"[Obsidian] 追加分析: {symbol} {section_anchor}")
            else:
                # 创建新文件
                fm = self._fm(
                    type="symbol",
                    symbol=symbol,
                    name=name,
                    industry=industry or "",
                    latest_date=date_str,
                    latest_direction=action,
                    latest_score=score,
                    latest_rating=rating,
                    total_trades=stats.get("total_trades", 0),
                    win_rate=stats.get("win_rate", 0),
                    tags=["symbol", symbol.split(".")[0] if "." in symbol else "US"],
                )
                content = f"""{fm}

# {name} ({symbol})

> 本页持续积累每次 deep_analysis 的深度分析结果。
> 每次 LLM 分析产出后自动追加, 含论据/交易计划/风险/因果链。
> 战绩数据从 strategy_memory.db 同步。

{section}"""
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
                logger.info(f"[Obsidian] 创建标的页: {symbol}")

            return filepath

        except Exception as e:
            logger.warning(f"[Obsidian] write_symbol_analysis 失败 (不影响pipeline): {e}")
            return None

    def _load_claim_ic(self) -> Dict:
        """读取 claim_ic_cache.json (论据级IC, best-effort)"""
        try:
            cache_path = os.path.join(
                os.path.expanduser("~"), ".hermes", "skills",
                "deep-stock-analysis", "scripts", "claim_ic_cache.json"
            )
            if not os.path.exists(cache_path):
                return {}
            import json
            with open(cache_path) as f:
                return json.load(f)
        except Exception:
            return {}

    def _claim_ic_tag(self, claim_type: str, claim_ic: Dict) -> str:
        """给单条论据生成IC标注, 如 [IC=+0.56✅] 或 [IC=-0.07⚠️] 或空"""
        if not claim_type or not claim_ic:
            return ""
        claims = claim_ic.get("claims", {})
        entry = claims.get(claim_type)
        if not entry:
            # 尝试模糊匹配 (claim_type可能带后缀如 theme_momentum:+0.05)
            for k, v in claims.items():
                if k.startswith(claim_type + ":") or claim_type.startswith(k.split(":")[0]):
                    entry = v
                    break
        if not entry:
            return ""
        ic = entry.get("ic_t1")
        n = entry.get("count", 0)
        if ic is None:
            return ""
        if ic > 0.05:
            return f"[IC={ic:+.2f}✅n={n}]"
        elif ic < -0.05:
            return f"[IC={ic:+.2f}⚠️n={n}]"
        else:
            return f"[IC={ic:+.2f}n={n}]"

    def _build_ic_summary(self, all_cases: list, claim_ic: Dict) -> str:
        """构建论据IC汇总段"""
        if not claim_ic or not all_cases:
            return "- IC缓存不可用"
        total = claim_ic.get("total_samples", 0)
        if total < 10:
            return f"- IC样本不足({total}条), 暂无统计意义"
        effective = claim_ic.get("effective_claims", [])
        ineffective = claim_ic.get("ineffective_claims", [])
        lines = [f"- 基于{total}条历史信号alpha IC验证 (overall_t1={claim_ic.get('overall_ic_t1',0):+.4f})"]
        if effective:
            lines.append(f"- ✅ 有效论据(IC>0.05): {', '.join(effective)}")
        if ineffective:
            lines.append(f"- ⚠️ 无效论据(IC<-0.05): {', '.join(ineffective)}")
        return "\n".join(lines)

    def _find_industry_peers(self, industry: str, exclude_symbol: str) -> list:
        """扫描 symbols/ 目录, 找同行业的其他symbol page (返回wikilink名列表)"""
        try:
            symbols_dir = os.path.join(self.vault, "symbols")
            if not os.path.isdir(symbols_dir):
                return []
            peers = []
            for fname in os.listdir(symbols_dir):
                if not fname.endswith(".md"):
                    continue
                if fname.replace(".md", "") == exclude_symbol:
                    continue
                fpath = os.path.join(symbols_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        head = f.read(2000)  # 只读frontmatter
                    # 检查 industry 字段
                    if f'industry: "{industry}"' in head or f'industry: {industry}' in head:
                        peers.append(fname.replace(".md", ""))
                except Exception:
                    continue
            return sorted(peers)[:10]  # 最多10个
        except Exception:
            return []

    def _read_symbol_stats(self, symbol: str) -> Dict:
        """从 strategy_memory.db 读取 per-symbol 战绩 (best-effort)"""
        try:
            import sqlite3
            db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "strategy_memory.db")
            if not os.path.exists(db_path):
                return {}
            conn = sqlite3.connect(db_path, timeout=3)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT total_trades, win_rate, avg_pnl FROM symbol_memory WHERE symbol = ?",
                (symbol,)
            ).fetchall()
            conn.close()
            if rows:
                r = rows[0]
                return {
                    "total_trades": r["total_trades"],
                    "win_rate": round(r["win_rate"], 2) if r["win_rate"] else 0,
                }
            return {}
        except Exception:
            return {}

    def _replace_frontmatter(self, content: str, updates: Dict) -> str:
        """替换文件 frontmatter 中的字段值"""
        try:
            if not content.startswith("---"):
                return content
            end_idx = content.index("---", 3)
            fm_block = content[3:end_idx]
            lines = fm_block.strip().split("\n")
            updated_lines = []
            keys_updated = set()
            for line in lines:
                # 检查是否是需要更新的字段
                for k, v in updates.items():
                    if line.strip().startswith(f"{k}:"):
                        updated_lines.append(self._format_fm_line(k, v))
                        keys_updated.add(k)
                        break
                else:
                    updated_lines.append(line)
            # 添加未找到的新字段
            for k, v in updates.items():
                if k not in keys_updated:
                    updated_lines.append(self._format_fm_line(k, v))
            new_fm = "\n".join(updated_lines)
            return f"---\n{new_fm}\n---{content[end_idx+3:]}"
        except Exception:
            return content

    def _format_fm_line(self, key: str, value) -> str:
        """格式化单行 frontmatter"""
        if isinstance(value, bool):
            return f"{key}: {'true' if value else 'false'}"
        elif isinstance(value, (int, float)):
            return f"{key}: {value}"
        elif isinstance(value, str) and value:
            escaped = value.replace('"', '\\"')
            return f'{key}: "{escaped}"'
        else:
            return f"{key}:"

    # ============================================================
    # 7. Daily 笔记 — 日报 (盘前 + 盘后)
    # ============================================================

    def write_daily_premarket(self, data: Dict) -> Optional[str]:
        """写入盘前计划笔记"""
        date_str = data.get('date', datetime.now().strftime("%Y-%m-%d"))
        market = data.get('market', 'HK')
        filename = f"{date_str}_{market.lower()}_premarket.md"

        targets = data.get('targets', [])
        target_list = "\n".join(f"- [[{t}]]" for t in targets) if targets else "- 无"

        fm = self._fm(
            type="daily",
            market=market,
            date=date_str,
            session="premarket",
            tags=["daily", "premarket", market.lower()],
            regime=data.get('regime', ''),
        )

        content = f"""{fm}

# 盘前计划: {market} ({date_str})

## 市场状态
- **市场阶段**: {data.get('regime', 'unknown')}
- **生成时间**: {data.get('generated_at', '')}

## 目标标的
{target_list}

## 限制标的
{chr(10).join(f"- [[{r}]]" for r in data.get('restricted', [])) if data.get('restricted') else '- 无'}

## 整体倾向
{data.get('overall_bias', '中性')}

## 关联决策
<!-- Agent 自动生成 -->
"""
        return self._write_md("daily/premarket", filename, content)

    def write_daily_postmarket(self, data: Dict) -> Optional[str]:
        """写入盘后复盘笔记"""
        date_str = data.get('date', datetime.now().strftime("%Y-%m-%d"))
        market = data.get('market', 'HK')
        filename = f"{date_str}_{market.lower()}_postmarket.md"

        trades = data.get('trades', [])
        trade_list = "\n".join(f"- [[{date_str}_{t['code'].replace('.', '_')}_{t.get('id', '')}]] | {t.get('pnl', 0):+.2f}" for t in trades) if trades else "- 无交易"

        # 2026-08-27 收盘审计④: total_trades 历史上按"当日进场笔数"计, 明细却只列已平仓 —
        #   13 进场/10 已平时 3 笔水下过夜仓对复盘者隐形, "胜率 100%" 成幸存者口径。
        #   补过夜持仓段 + 概览三数口径标注。open_trades 缺省空 → 行为向后兼容。
        opens = data.get('open_trades') or []
        open_list = "\n".join(
            f"- [[{date_str}_{o['code'].replace('.', '_')}_{o.get('id', '')}]] | 未平 "
            f"进场 {str(o.get('entry', ''))[:19]} ET"
            + (f" | 台账水深 {float(o['underwater']):+.2f}%" if o.get('underwater') is not None else "")
            for o in opens
        ) if opens else ""

        fm = self._fm(
            type="daily",
            market=market,
            date=date_str,
            session="postmarket",
            tags=["daily", "postmarket", market.lower()],
            total_trades=data.get('total_trades', 0),
            closed_trades=len(trades),
            overnight_count=len(opens),
            win_rate=round(data.get('win_rate', 0), 2),
            total_pnl=round(data.get('total_pnl', 0), 2),
        )

        _ov_line = f"- **总交易**: {data.get('total_trades', 0)} (已平 {len(trades)} | 过夜 {len(opens)})"
        # py3.11: f-string 表达式部分不许反斜杠, 过夜段先拼好再插值
        _overnight_block = (
            "\n\n## 过夜持仓 (未平, 盈亏未落袋)\n" + open_list if open_list else ""
        )
        # 2026-09-03 复盘批: 原行 `- **最大回撤**: {data.get('max_drawdown', 0):.2%}` 双重说谎 —
        #   ①无任何调用方写入 max_drawdown 键 (daily_review._generate_summary 从不产它) → 恒 0.00%,
        #     却与"当日净亏 -49 + 水下过夜仓"并列显示 = 常量退化 (军规"基数自检"同类);
        #   ②源字段 trade_records.max_drawdown 是**百分点**口径 (峰→谷, 由 mfe/mae 反算), 套 .2%
        #     会把 3.65pp 印成 364.64% = 量纲说谎 (军规"单位/量纲声明"同类)。
        #   现改为日级"最大单笔持仓回撤": 单位写进标签 + 带非零样本数自证 (全零日如实显示 0 笔)。
        _dd_pp = data.get('max_drawdown_pp')
        _dd_line = (
            f"{float(_dd_pp):.2f} pp (峰→谷)"
            + (f" · {data['max_drawdown_symbol']}" if data.get('max_drawdown_symbol') else "")
            + f" | 当日 {data.get('max_drawdown_n') or 0}/{len(trades)} 笔有非零回撤"
        ) if _dd_pp is not None else f"n/a | 当日 0/{len(trades)} 笔有非零回撤"
        content = f"""{fm}

# 盘后复盘: {market} ({date_str})

## 概览
{_ov_line}
- **胜率 (已平口径)**: {data.get('win_rate', 0):.1%}
- **总盈亏 (已平口径)**: {data.get('total_pnl', 0):+.2f}
- **最大单笔持仓回撤**: {_dd_line}

## 交易明细 (仅已平仓)
{trade_list}{_overnight_block}

## 归因总结
<!-- Agent 自动生成 -->

## 经验教训
<!-- Agent 自动生成 -->

## 次日建议
<!-- Agent 自动生成 -->
"""
        return self._write_md("daily/postmarket", filename, content)


# ============================================================
# 单例模式 — 支持多 vault (HK/US 独立)
# ============================================================
_writer_instances: Dict[str, 'ObsidianWriter'] = {}

def get_obsidian_writer(vault_path: str = None) -> 'ObsidianWriter':
    """获取 ObsidianWriter 单例 (按 vault_path 区分 HK/US)"""
    global _writer_instances
    key = vault_path or ObsidianWriter.DEFAULT_VAULT_PATH
    if key not in _writer_instances:
        _writer_instances[key] = ObsidianWriter(vault_path=key)
    return _writer_instances[key]
