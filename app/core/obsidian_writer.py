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
        写入经验教训笔记。支持两种输入格式:

        【格式 A — 归因引擎输出 (hk_attribution_runner)】
        data: {
            'symbol': str, 'cause': str, 'lesson': str,
            'pnl_percent': float, 'trade_date': str,
            'direction': str, 'confidence': float,
            'llm_attribution': {  # optional
                'direction': {'root_cause': '...', 'lesson': '...', 'improvement': '...'},
                'execution': {'root_cause': '...', 'lesson': '...', 'improvement': '...'},
            },
        }

        【格式 B — 详细格式】
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
        # ── 格式检测: 归因引擎输出 → 映射为详细格式 ──
        if 'cause' in data and 'symbol' in data and 'topic' not in data:
            data = self._adapt_attribution_lesson(data)

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

    def _adapt_attribution_lesson(self, data: Dict) -> Dict:
        """将归因引擎的 lesson 格式映射为 Obsidian write_lesson 格式"""
        symbol = data.get('symbol', '').replace('HK.', '').replace('US.', '')
        pnl = data.get('pnl_percent', 0) or 0
        cause = data.get('cause', 'unknown')
        lesson_text = data.get('lesson', '')
        trade_date = data.get('trade_date', '')

        # 严重程度映射
        if pnl < -3:
            severity = "P0"
        elif pnl < -1:
            severity = "P1"
        elif pnl < 0:
            severity = "P2"
        else:
            severity = "P3"

        # 类别映射
        category_map = {
            "direction_wrong": "strategy",
            "timing_poor": "execution",
            "market_misread": "strategy",
            "signal_noise": "data",
            "reasonable_loss": "risk",
            "stop_loss_triggered": "risk",
            "execution_poor": "execution",
            "execution_deviation": "execution",
            "risk_control_failed": "risk",
            "intraday_chase_high": "execution",
            "intraday_panic_sell": "execution",
            "ashare_divergence": "strategy",
            "plan_missing": "strategy",
            "lot_size_trim_impact": "config",
            "successful_trade": "strategy",
        }
        category = category_map.get(cause, "unknown")

        # Topic: use cause as topic
        topic = cause

        # 构建标题
        title = f"{symbol} {cause} (P&L {pnl:+.1f}%)"

        # 提取 LLM 归因详情
        llm = data.get('llm_attribution', {})
        dir_attr = llm.get('direction', {}) if llm else {}
        exec_attr = llm.get('execution', {}) if llm else {}

        root_cause_parts = []
        description_parts = [f"标的: {symbol}", f"交易日: {trade_date}", f"盈亏: {pnl:+.1f}%"]

        if lesson_text:
            description_parts.append(f"规则归因: {lesson_text}")

        if dir_attr.get('root_cause'):
            root_cause_parts.append(f"[方向] {dir_attr['root_cause']}")
            if dir_attr.get('lesson'):
                description_parts.append(f"LLM方向分析: {dir_attr['lesson']}")

        if exec_attr.get('root_cause') and exec_attr.get('cause_category') != 'execution_aligned':
            root_cause_parts.append(f"[执行] {exec_attr['root_cause']}")
            if exec_attr.get('lesson'):
                description_parts.append(f"LLM执行分析: {exec_attr['lesson']}")

        # 改进动作
        fix_parts = []
        if dir_attr.get('improvement'):
            fix_parts.append(f"方向: {dir_attr['improvement']}")
        if exec_attr.get('improvement'):
            fix_parts.append(f"执行: {exec_attr['improvement']}")

        # 预防措施
        prevention_parts = []
        if cause == 'intraday_chase_high':
            prevention_parts.append("设置追高过滤器，入场前确认回调信号")
        elif cause == 'intraday_panic_sell':
            prevention_parts.append("提前设置止损位，避免盘中情绪化操作")
        elif cause == 'direction_wrong':
            prevention_parts.append("加强盘前多因子确认，不依赖单一信号")
        elif cause == 'stop_loss_triggered':
            prevention_parts.append("复盘止损位设置是否合理，是否入场过晚")
        elif cause == 'execution_deviation':
            prevention_parts.append("强化作战计划约束，计划外交易需二次确认")

        if dir_attr.get('key_finding'):
            prevention_parts.append(f"关键发现: {dir_attr['key_finding']}")

        return {
            "topic": topic,
            "title": title,
            "market": "HK" if symbol.startswith("0") or symbol.isdigit() else "HK",
            "date": trade_date,
            "severity": severity,
            "category": category,
            "description": "\n".join(description_parts),
            "root_cause": "; ".join(root_cause_parts) if root_cause_parts else cause,
            "fix_applied": "\n".join(fix_parts) if fix_parts else "待实施",
            "prevention": "\n".join(prevention_parts) if prevention_parts else "基于归因分析自动生成",
            "related_trades": data.get('related_trades', []),
            "applied_to": data.get('applied_to', []),
        }

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
    # 6. Daily 笔记 — 日报 (盘前 + 盘后)
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

        fm = self._fm(
            type="daily",
            market=market,
            date=date_str,
            session="postmarket",
            tags=["daily", "postmarket", market.lower()],
            total_trades=data.get('total_trades', 0),
            win_rate=round(data.get('win_rate', 0), 2),
            total_pnl=round(data.get('total_pnl', 0), 2),
        )

        content = f"""{fm}

# 盘后复盘: {market} ({date_str})

## 概览
- **总交易**: {data.get('total_trades', 0)}
- **胜率**: {data.get('win_rate', 0):.1%}
- **总盈亏**: {data.get('total_pnl', 0):+.2f}
- **最大回撤**: {data.get('max_drawdown', 0):.2%}

## 交易明细
{trade_list}

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
