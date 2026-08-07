"""
Playbook Database Layer — 表创建 + CRUD 操作

Tables (spec 6.1):
  1. playbook_instructions — 交易指令 (Layer 1 产出)
  2. playbook_factor_stats — 因子统计 (双层分析)
  3. playbook_patterns — 精确组合 (高频)
  4. playbook_user_feedback — 用户反馈
  5. playbook_regime_log — 市场环境日志

Usage:
  from app.playbook.db import PlaybookDB

  db = PlaybookDB()
  db.ensure_tables()  # 首次调用创建表

  # 写入指令
  db.save_instruction(instruction_dict)

  # 读取今日指令
  instructions = db.get_instructions_by_date("2026-06-18")
"""
import json
import logging
import sqlite3
from typing import Dict, List, Optional, Any
from datetime import datetime

from app.storage.db_pool import get_db
from app.utils.symbol_utils import normalize_symbol
from app.utils.timezone import now_utc

logger = logging.getLogger(__name__)


def _estimate_admission_qty(inst: Dict[str, Any]) -> Optional[int]:
    """估算指令仓位 (2026-08-07 grilling Q2, admission record-only 预判用).

    生成时 position_size 未定 (None) → admission 判据 notional=0 全空转。
    此函数用保守默认乘数估算 qty: risk_base×risk_pct / |entry−stop|,
    与 instruction_generator.compute_position_size 同构 (regime/water/conf=1.0,
    含 max_position_pct 截断)。
    仅用于预判审计, 不参与任何真实 sizing; 估算失败 → None。
    """
    try:
        entry = float(inst.get("entry_price") or 0)
        stop = float(inst.get("stop_loss") or 0)
        if entry <= 0 or stop <= 0:
            return None
        from app.playbook.config import get_playbook_config as _gpc_est
        _est_pb = _gpc_est()
        # risk 参数在 playbook.risk_parameters 段 (扁平合并后同键可取);
        # risk_pct 为百分数 (0.5 = 0.5%), 与 instruction_generator 同口径 (/100)
        # 2026-08-07 复核修复: 兑底默认 0.02 → 1.0 (与 PlaybookConfig.DEFAULTS 对齐;
        #   原 0.02 在 yaml 缺键时会高估 qty 50 倍) + 补 max_position_pct 截断
        #   (原缺失 → 高价股/紧止损场景 notional 系统性偏大, fee_bps 偏小, 预判偏宽松;
        #   实测样例 entry=150/stop=147 估算 136 股 vs 截断后 32 股)。
        _risk_pct = float(_est_pb.get("risk_pct", 1.0) or 1.0)
        _risk_base = float(_est_pb.get("risk_base_capital", 82000.0) or 82000.0)
        _qty = int(_risk_base * (_risk_pct / 100.0) / abs(entry - stop))
        # max_position_pct 截断与 compute_position_size 完全同构:
        #   默认对齐 DEFAULTS=0.08; 显式 0/None → 不截断 (or 0.0 而非 or 默认)
        _max_pct = float(_est_pb.get("max_position_pct", 0.08) or 0.0)
        if _max_pct > 0 and _qty * entry > _risk_base * _max_pct:
            _qty = max(1, int(_risk_base * _max_pct / entry))
        return max(_qty, 0)
    except Exception:
        return None


class PlaybookDB:
    """Playbook 数据库操作"""

    def __init__(self, db_name: str = "ftseek_us", conn: sqlite3.Connection = None):
        """
        Args:
            db_name: 数据库池名称 (生产用)
            conn: 直接传入连接 (测试用, 跳过连接池)
        """
        self.db_name = db_name
        self._direct_conn = conn
        self._tables_created = False

    def _conn(self):
        """获取连接"""
        if self._direct_conn is not None:
            return self._direct_conn
        return get_db(self.db_name).connection()

    def ensure_tables(self):
        """创建所有 playbook 表 (IF NOT EXISTS) + 列迁移"""
        conn = self._conn()
        try:
            conn.executescript(_SCHEMA_SQL)
            conn.executescript(_CREATE_SIGNAL_CONTEXT_SQL)
            # 2026-06-23 修复 (OKR P0): CREATE TABLE IF NOT EXISTS 不会给已存在的表加新列.
            # fwd_return_t1/t3/t5 在 schema 里定义但旧 DB 未迁移, 导致 ic_tracker 写入
            # 因列不存在而失败 (被 except 吞 → IC 永远 0). 显式 ALTER 补列.
            self._migrate_instruction_columns(conn)
            self._tables_created = True
            logger.debug("Playbook tables ensured")  # 2026-07-31 降噪: INFO→DEBUG (每 ensure 都打, 23条/11h)
        except Exception as e:
            logger.error(f"Failed to create playbook tables: {e}")
            raise

    def _migrate_instruction_columns(self, conn):
        """为已存在的 playbook_instructions 表补缺失列 (SQLite 无 ADD COLUMN IF NOT EXISTS)."""
        # name → type, 仅列 schema 定义但可能未迁移的列
        needed = {
            "fwd_return_intraday": "REAL",
            "fwd_return_t1": "REAL",
            "fwd_return_t3": "REAL",
            "fwd_return_t5": "REAL",
            "horizon": "TEXT",
            "horizon_confidence": "REAL",
            "horizon_reasoning": "TEXT",
            "config_version": "TEXT",
            "invalidation": "TEXT",
            "entry_low": "REAL",
            "entry_high": "REAL",
            "vol_bucket": "TEXT",
            "atr_pct": "REAL",
            # 2026-07-19 (DEB-round3 #11): IC 代理回填标记 — fwd_return_* 用当日 open
            #   代理 (未触发指令无 entry_price) 时置 1, 供 IC 统计区分真实/代理样本.
            "fwd_proxy": "INTEGER",
            # 2026-07-31 (DEB-feedback-loop F1): 锚偏离度 — entry 锚 vs 生成时刻市价 (%).
            #   DA=LLM entry vs 分析时刻快照价; UE=inject 现价 vs scan 浮现价 (锚时滞).
            "anchor_dev_pct": "REAL",
            # 2026-08-01 (用户裁"同意"四议题方案): open 基准前瞻收益 — 与 entry 基准成对落库.
            #   根因: 未执行桶"edge"经 open 基准检验≈100%基准幻觉 (limit 价不可成交),
            #   任何 hit-rate 必须标注价格基准 (新口径戒律). ic_tracker 回填时同柱计算.
            "fwd_return_intraday_open": "REAL",
            "fwd_return_t1_open": "REAL",
            "fwd_return_t3_open": "REAL",
            # 2026-08-03 (改进批 任务#3): 模型归因 — deepseek A/B 污染复盘暴露"指令无模型字段",
            #   scorecard 无法按模型分层. model_used=生成模型 (DA skill 写 config.LLM_MODEL);
            #   is_ab_test=1 标记实验写入; metadata_json 通用标注 blob (如污染注记).
            "model_used": "TEXT",
            "is_ab_test": "INTEGER",
            "metadata_json": "TEXT",
        }
        try:
            existing = {row[1] for row in conn.execute("PRAGMA table_info(playbook_instructions)").fetchall()}
        except Exception as e:
            logger.warning(f"[playbook] 迁移: 读取 table_info 失败: {e}")
            return
        for col, col_type in needed.items():
            if col not in existing:
                try:
                    conn.execute(f"ALTER TABLE playbook_instructions ADD COLUMN {col} {col_type}")
                    logger.info(f"[playbook] 迁移: 已补列 playbook_instructions.{col} ({col_type})")
                except Exception as e:
                    logger.warning(f"[playbook] 迁移: 补列 {col} 失败: {e}")
        conn.commit()


    # ========== playbook_instructions ==========

    def save_instruction(self, inst: Dict[str, Any]) -> int:
        """
        写入/更新一条交易指令 (UPSERT by date+symbol)

        Args:
            inst: 指令 dict, 必须包含 date, symbol, direction

        Returns:
            row id
        """
        # 写入侧规范化: 折叠 US.US. 双前缀 + 归并别名(GOOGL->GOOG), 防数据割裂
        inst["symbol"] = normalize_symbol(inst.get("symbol"))

        # 准入门 (2026-08-06): 全写入方唯一收口点 — DA 直写/UE/topup/generate 四路都过这里。
        #   立项根因: confidence 门只写在 _build_instructions_from_votes 内 (pipeline.py:1264),
        #   DA/UE 直写 DB 绕过全部质量门 (实测 35 日 DA 219 条 BUY 平均 conf=36 全入库)。
        #   默认 record-only (playbook.admission.enforce=false) → 不改指令, 只落审计。
        #   回滚: playbook.admission.enabled=false。异常一律不阻断写入 (审计降级优于丢指令)。
        # 2026-08-07 grilling Q2 (record-only 预判): 生成时 position_size=None →
        #   notional=0 → 判据全空转 (8月 0 条生成时拦截)。用估算 size 预判,
        #   结果挂 raw_instruction.admission.precheck (只审计, 不 enforce — 估算基于
        #   评估时点乘数, 硬拦会误杀执行时乘数恢复的候选)。注意: 预判与执行时
        #   二次复核结果可能不一致, 统计侧须按 estimated 标记区分口径。
        try:
            from app.playbook.admission import apply as _admission_apply
            from app.playbook.admission import evaluate as _admission_eval
            _inst_est = dict(inst)
            if not _inst_est.get("position_size"):
                _est_qty = _estimate_admission_qty(_inst_est)
                if _est_qty and _est_qty > 0:
                    _inst_est["position_size"] = _est_qty
            inst, _adm = _admission_apply(inst)
            # 预判在 apply 之后写 (apply 会整体赋值 admission 键, 先写会被覆盖)
            if _inst_est.get("position_size"):
                try:
                    _pre = _admission_eval(_inst_est)
                    _raw_pre = inst.get("raw_instruction")
                    if not isinstance(_raw_pre, dict):
                        _raw_pre = ({"_original": _raw_pre}
                                    if _raw_pre is not None else {})
                        inst["raw_instruction"] = _raw_pre
                    _raw_pre.setdefault("admission", {})["precheck"] = {
                        "verdict": _pre["verdict"], "reasons": _pre["reasons"],
                        "metrics": _pre["metrics"], "enforced": False,
                        "estimated": True,
                    }
                except Exception:
                    pass  # 预判失败不影响写入 (审计降级)
            if _adm.get("reasons"):
                logger.info(
                    f"[admission] {inst.get('symbol')} {_adm.get('verdict')} "
                    f"reasons={_adm['reasons']} enforced={_adm.get('enforce')}"
                )
        except Exception as _adm_err:
            logger.warning(f"[admission] 判定异常 (已忽略, 指令照常入库): {_adm_err}")

        conn = self._conn()
        now = now_utc().isoformat()

        conn.execute("""
            INSERT INTO playbook_instructions (
                date, symbol, direction, skip_reason,
                entry_price, stop_loss, take_profit,
                entry_low, entry_high,
                position_size, risk_amount, risk_reward, confidence,
                conditions_json, thesis, sector, risks_json,
                market_regime, vix_at_generation, spy_trend_at_generation,
                data_quality_json, ok_dimensions,
                matched_patterns_json, pattern_verdict,
                raw_instruction_json, decision_id,
                horizon, horizon_confidence, horizon_reasoning,
                created_at, updated_at, config_version, invalidation, anchor_dev_pct,
                model_used, is_ab_test, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'v2', ?, ?, ?, ?, ?)
            ON CONFLICT(date, symbol) DO UPDATE SET
                direction=excluded.direction,
                skip_reason=excluded.skip_reason,
                entry_price=excluded.entry_price,
                stop_loss=excluded.stop_loss,
                take_profit=excluded.take_profit,
                entry_low=excluded.entry_low,
                entry_high=excluded.entry_high,
                position_size=excluded.position_size,
                risk_amount=excluded.risk_amount,
                risk_reward=excluded.risk_reward,
                confidence=excluded.confidence,
                conditions_json=excluded.conditions_json,
                thesis=excluded.thesis,
                sector=excluded.sector,
                risks_json=excluded.risks_json,
                market_regime=excluded.market_regime,
                vix_at_generation=excluded.vix_at_generation,
                data_quality_json=excluded.data_quality_json,
                ok_dimensions=excluded.ok_dimensions,
                raw_instruction_json=excluded.raw_instruction_json,
                decision_id=excluded.decision_id,
                horizon=excluded.horizon,
                horizon_confidence=excluded.horizon_confidence,
                horizon_reasoning=excluded.horizon_reasoning,
                invalidation=excluded.invalidation,
                anchor_dev_pct=COALESCE(excluded.anchor_dev_pct, anchor_dev_pct),
                model_used=COALESCE(excluded.model_used, model_used),
                is_ab_test=COALESCE(excluded.is_ab_test, is_ab_test),
                metadata_json=COALESCE(excluded.metadata_json, metadata_json),
                updated_at=excluded.updated_at
        """, (
            inst.get("date"),
            inst.get("symbol"),
            inst.get("direction"),
            inst.get("skip_reason"),
            inst.get("entry_price"),
            inst.get("stop_loss"),
            inst.get("take_profit"),
            inst.get("entry_low"),
            inst.get("entry_high"),
            inst.get("position_size"),
            inst.get("risk_amount"),
            inst.get("risk_reward"),
            inst.get("confidence"),
            _to_json(inst.get("conditions")),
            inst.get("thesis"),
            inst.get("sector"),
            _to_json(inst.get("risks")),
            inst.get("market_regime"),
            inst.get("vix_at_generation"),
            inst.get("spy_trend_at_generation"),
            _to_json(inst.get("data_quality")),
            inst.get("ok_dimensions"),
            _to_json(inst.get("matched_patterns")),
            inst.get("pattern_verdict"),
            _to_json(inst.get("raw_instruction")),
            inst.get("decision_id", ""),
            inst.get("horizon", "short"),
            inst.get("horizon_confidence"),
            _to_json(inst.get("horizon_reasoning")),
            inst.get("created_at", now),
            now,
            inst.get("invalidation", "") or "",
            inst.get("anchor_dev_pct"),
            inst.get("model_used"),
            inst.get("is_ab_test"),
            _to_json(inst.get("metadata") if inst.get("metadata") is not None else inst.get("metadata_json")),
        ))
        conn.commit()
        # UPSERT: last_insert_rowid() 在 UPDATE 时返回 0, 需要显式查询
        row = conn.execute(
            "SELECT id FROM playbook_instructions WHERE date = ? AND symbol = ?",
            (inst.get("date"), inst.get("symbol"))
        ).fetchone()
        return row[0] if row else 0

    def get_instructions_by_date(self, date: str) -> List[Dict]:
        """获取某日所有指令"""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM playbook_instructions WHERE date = ? ORDER BY id",
            (date,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def count_executed_by_direction(self, date: str, direction: str) -> int:
        """统计某日某方向已执行指令数 (议题二C 2026-08-01: BUY 日执行配额计数)."""
        conn = self._conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM playbook_instructions "
            "WHERE date = ? AND direction = ? AND executed = 1",
            (date, direction),
        ).fetchone()
        return int(row[0] or 0) if row else 0

    def get_instruction(self, date: str, symbol: str) -> Optional[Dict]:
        """获取某日某标的指令"""
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM playbook_instructions WHERE date = ? AND symbol = ?",
            (date, symbol)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def update_instruction_tracking(self, instruction_id: int, updates: Dict[str, Any]):
        """更新指令的追踪字段 (Layer 3 盘后填充)"""
        conn = self._conn()
        now = now_utc().isoformat()

        set_clauses = []
        values = []
        for key, value in updates.items():
            if key in _TRACKING_FIELDS:
                set_clauses.append(f"{key} = ?")
                values.append(value)

        if not set_clauses:
            return

        set_clauses.append("updated_at = ?")
        values.append(now)
        values.append(instruction_id)

        conn.execute(
            f"UPDATE playbook_instructions SET {', '.join(set_clauses)} WHERE id = ?",
            values
        )
        conn.commit()

    def try_claim_execution(self, instruction_id: int) -> bool:
        """原子抢占执行权 — 防止同一指令被并发/重试双重下单.

        用条件 UPDATE (WHERE executed 未置位) 把 executed 置 1, 仅当本次调用
        真正翻转了行 (rowcount == 1) 才返回 True. 两个并发线程只有一个能抢到.
        必须在调用 broker submit_order 之前调用; 若后续下单失败可用
        release_execution_claim 释放以便重试.
        """
        conn = self._conn()
        cur = conn.execute(
            "UPDATE playbook_instructions SET executed = 1, updated_at = ? "
            "WHERE id = ? AND (executed IS NULL OR executed = 0)",
            (now_utc().isoformat(), instruction_id),
        )
        conn.commit()
        return cur.rowcount == 1

    def release_execution_claim(self, instruction_id: int):
        """释放执行抢占 (executed 置回 0) — 下单前失败时允许重试."""
        conn = self._conn()
        conn.execute(
            "UPDATE playbook_instructions SET executed = 0, updated_at = ? WHERE id = ?",
            (now_utc().isoformat(), instruction_id),
        )
        conn.commit()

    def get_instructions_with_outcome(self, days_back: int = 90) -> List[Dict]:
        """获取有结果的指令 (用于因子分析)"""
        conn = self._conn()
        rows = conn.execute("""
            SELECT * FROM playbook_instructions
            WHERE outcome IS NOT NULL
              AND outcome != 'holding'
              AND direction != 'SKIP'
            ORDER BY date DESC
            LIMIT ?
        """, (days_back * 10,)).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_recent_instructions(self, limit: int = 50) -> List[Dict]:
        """获取最近的指令"""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM playbook_instructions ORDER BY date DESC, id LIMIT ?",
            (limit,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ========== playbook_factor_stats ==========

    def upsert_factor_stat(self, stat: Dict[str, Any]):
        """写入/更新因子统计"""
        conn = self._conn()
        now = now_utc().isoformat()

        conn.execute("""
            INSERT INTO playbook_factor_stats (
                factor_key, analysis_level, co_factors_json,
                total_trades, wins, losses, win_rate,
                avg_pnl, avg_pnl_pct, total_pnl, profit_factor,
                max_win, max_loss,
                bayesian_win_rate, bayesian_lower, bayesian_upper,
                effective_sample_size,
                verdict, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(factor_key, analysis_level) DO UPDATE SET
                total_trades=excluded.total_trades,
                wins=excluded.wins,
                losses=excluded.losses,
                win_rate=excluded.win_rate,
                avg_pnl=excluded.avg_pnl,
                avg_pnl_pct=excluded.avg_pnl_pct,
                total_pnl=excluded.total_pnl,
                profit_factor=excluded.profit_factor,
                max_win=excluded.max_win,
                max_loss=excluded.max_loss,
                bayesian_win_rate=excluded.bayesian_win_rate,
                bayesian_lower=excluded.bayesian_lower,
                bayesian_upper=excluded.bayesian_upper,
                effective_sample_size=excluded.effective_sample_size,
                verdict=excluded.verdict,
                last_updated=excluded.last_updated
        """, (
            stat.get("factor_key"),
            stat.get("analysis_level"),
            _to_json(stat.get("co_factors")),
            stat.get("total_trades", 0),
            stat.get("wins", 0),
            stat.get("losses", 0),
            stat.get("win_rate", 0),
            stat.get("avg_pnl", 0),
            stat.get("avg_pnl_pct", 0),
            stat.get("total_pnl", 0),
            stat.get("profit_factor", 0),
            stat.get("max_win", 0),
            stat.get("max_loss", 0),
            stat.get("bayesian_win_rate"),
            stat.get("bayesian_lower"),
            stat.get("bayesian_upper"),
            stat.get("effective_sample_size"),
            stat.get("verdict", "observing"),
            now,
        ))
        conn.commit()

    def get_factor_stats(self, level: str = None, verdict: str = None) -> List[Dict]:
        """获取因子统计"""
        conn = self._conn()
        query = "SELECT * FROM playbook_factor_stats WHERE 1=1"
        params = []

        if level:
            query += " AND analysis_level = ?"
            params.append(level)
        if verdict:
            query += " AND verdict = ?"
            params.append(verdict)

        query += " ORDER BY total_trades DESC"
        rows = conn.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ========== playbook_patterns ==========

    def upsert_pattern(self, pattern: Dict[str, Any]):
        """写入/更新 pattern"""
        conn = self._conn()
        now = now_utc().isoformat()

        conn.execute("""
            INSERT INTO playbook_patterns (
                pattern_key, conditions_json,
                total_trades, wins, losses, win_rate,
                avg_pnl, avg_pnl_pct, max_win, max_loss, profit_factor,
                bayesian_win_rate, bayesian_lower, bayesian_upper,
                status, first_seen, last_seen, last_updated, weekly_notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pattern_key) DO UPDATE SET
                total_trades=excluded.total_trades,
                wins=excluded.wins,
                losses=excluded.losses,
                win_rate=excluded.win_rate,
                avg_pnl=excluded.avg_pnl,
                avg_pnl_pct=excluded.avg_pnl_pct,
                max_win=excluded.max_win,
                max_loss=excluded.max_loss,
                profit_factor=excluded.profit_factor,
                bayesian_win_rate=excluded.bayesian_win_rate,
                bayesian_lower=excluded.bayesian_lower,
                bayesian_upper=excluded.bayesian_upper,
                status=excluded.status,
                last_seen=excluded.last_seen,
                last_updated=excluded.last_updated,
                weekly_notes=excluded.weekly_notes
        """, (
            pattern.get("pattern_key"),
            _to_json(pattern.get("conditions")),
            pattern.get("total_trades", 0),
            pattern.get("wins", 0),
            pattern.get("losses", 0),
            pattern.get("win_rate", 0),
            pattern.get("avg_pnl", 0),
            pattern.get("avg_pnl_pct", 0),
            pattern.get("max_win", 0),
            pattern.get("max_loss", 0),
            pattern.get("profit_factor", 0),
            pattern.get("bayesian_win_rate"),
            pattern.get("bayesian_lower"),
            pattern.get("bayesian_upper"),
            pattern.get("status", "observing"),
            pattern.get("first_seen", now),
            pattern.get("last_seen", now),
            now,
            pattern.get("weekly_notes"),
        ))
        conn.commit()

    def get_patterns(self, status: str = None) -> List[Dict]:
        """获取 patterns"""
        conn = self._conn()
        if status:
            rows = conn.execute(
                "SELECT * FROM playbook_patterns WHERE status = ? ORDER BY total_trades DESC",
                (status,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM playbook_patterns ORDER BY total_trades DESC"
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ========== playbook_user_feedback ==========

    def save_user_feedback(self, feedback: Dict[str, Any]) -> int:
        """保存用户反馈"""
        conn = self._conn()
        now = now_utc().isoformat()

        cursor = conn.execute("""
            INSERT INTO playbook_user_feedback (
                instruction_id, action, skip_reason,
                modified_field, original_value, modified_value,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            feedback.get("instruction_id"),
            feedback.get("action"),
            feedback.get("skip_reason"),
            feedback.get("modified_field"),
            feedback.get("original_value"),
            feedback.get("modified_value"),
            now,
        ))
        conn.commit()
        return cursor.lastrowid

    def get_user_feedback(self, instruction_id: int) -> List[Dict]:
        """获取某指令的用户反馈"""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM playbook_user_feedback WHERE instruction_id = ?",
            (instruction_id,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_recent_feedback(self, days: int = 7) -> List[Dict]:
        """获取最近 N 天的所有用户反馈 (Phase 1: 反馈学习)"""
        conn = self._conn()
        from datetime import datetime, timedelta
        cutoff = (now_utc() - timedelta(days=days)).isoformat()
        rows = conn.execute(
            "SELECT * FROM playbook_user_feedback WHERE created_at >= ? ORDER BY created_at DESC",
            (cutoff,)
        ).fetchall()
        return [d for r in rows if (d := _row_to_dict(r)) is not None]

    def get_instruction_by_id(self, instruction_id: int) -> Optional[Dict]:
        """通过 ID 获取单条指令"""
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM playbook_instructions WHERE id = ?",
            (instruction_id,)
        ).fetchone()
        return _row_to_dict(row)

    def void_instruction(
        self,
        instruction_id: int,
        reason: str,
        operator: str = "system",
    ) -> bool:
        """将指令标记为 void (phantom / manual reconcile 等), 并写入审计日志.

        Returns:
            True 若成功, False 若指令不存在或已是终态.
        """
        from app.playbook.audit import log_reconciliation

        before = self.get_instruction_by_id(instruction_id)
        if before is None:
            logger.warning(f"[PlaybookDB] void_instruction({instruction_id}) 不存在")
            return False

        current_outcome = before.get("outcome")
        if current_outcome in ("win", "loss"):
            logger.warning(
                f"[PlaybookDB] void_instruction({instruction_id}) 已是终态 {current_outcome}, 跳过"
            )
            return False

        conn = self._conn()
        now = now_utc().isoformat()
        conn.execute(
            "UPDATE playbook_instructions SET outcome=?, updated_at=? WHERE id=?",
            ("void", now, instruction_id),
        )
        conn.commit()

        after = self.get_instruction_by_id(instruction_id)
        log_reconciliation(
            action="void_instruction",
            reason=reason,
            operator=operator,
            before=before,
            after=after,
            metadata={"instruction_id": instruction_id},
        )
        logger.info(f"[PlaybookDB] void_instruction({instruction_id}) 已标记: {reason}")
        return True

    # ========== playbook_signal_context ==========

    def save_signal_context(self, ctx: Dict[str, Any]) -> None:
        """UPSERT 信号上下文 (信息层数据)."""
        conn = self._conn()
        now = now_utc().isoformat()
        conn.execute("""
            INSERT INTO playbook_signal_context (
                date, symbol,
                news_headlines_json, news_sentiment,
                insider_signal, insider_cluster_score, insider_net_shares, insider_cluster_buyers_json,
                earnings_next_date, earnings_days_until, earnings_beat_rate,
                pe, market_cap, industry, revenue_growth_yoy, roe, debt_to_equity, high_52w, low_52w,
                theme_momentum, theme_wind, theme_narratives_json,
                llm_reasoning_json, raw_data_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, symbol) DO UPDATE SET
                news_headlines_json=excluded.news_headlines_json,
                news_sentiment=excluded.news_sentiment,
                insider_signal=excluded.insider_signal,
                insider_cluster_score=excluded.insider_cluster_score,
                insider_net_shares=excluded.insider_net_shares,
                insider_cluster_buyers_json=excluded.insider_cluster_buyers_json,
                earnings_next_date=excluded.earnings_next_date,
                earnings_days_until=excluded.earnings_days_until,
                earnings_beat_rate=excluded.earnings_beat_rate,
                pe=excluded.pe, market_cap=excluded.market_cap, industry=excluded.industry,
                revenue_growth_yoy=excluded.revenue_growth_yoy, roe=excluded.roe,
                debt_to_equity=excluded.debt_to_equity, high_52w=excluded.high_52w, low_52w=excluded.low_52w,
                theme_momentum=excluded.theme_momentum, theme_wind=excluded.theme_wind,
                theme_narratives_json=excluded.theme_narratives_json,
                llm_reasoning_json=excluded.llm_reasoning_json,
                raw_data_json=excluded.raw_data_json
        """, (
            ctx.get("date"), ctx.get("symbol"),
            _to_json(ctx.get("news_headlines")), ctx.get("news_sentiment"),
            ctx.get("insider_signal"), ctx.get("insider_cluster_score"),
            ctx.get("insider_net_shares"), _to_json(ctx.get("insider_cluster_buyers")),
            ctx.get("earnings_next_date"), ctx.get("earnings_days_until"),
            ctx.get("earnings_beat_rate"),
            ctx.get("pe"), ctx.get("market_cap"), ctx.get("industry"),
            ctx.get("revenue_growth_yoy"), ctx.get("roe"), ctx.get("debt_to_equity"),
            ctx.get("high_52w"), ctx.get("low_52w"),
            ctx.get("theme_momentum"), ctx.get("theme_wind"),
            _to_json(ctx.get("theme_narratives")),
            _to_json(ctx.get("llm_reasoning")),
            _to_json(ctx.get("raw_data")),
            now,
        ))
        conn.commit()

    def get_signal_context(self, date: str, symbol: str) -> Optional[Dict]:
        """获取某日某标的的信号上下文."""
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM playbook_signal_context WHERE date = ? AND symbol = ?",
            (date, symbol),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def get_signal_contexts_by_date(self, date: str) -> List[Dict]:
        """获取某日所有信号上下文."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM playbook_signal_context WHERE date = ? ORDER BY symbol",
            (date,),
        ).fetchall()
        return [d for r in rows if (d := _row_to_dict(r)) is not None]

    # ========== playbook_regime_log ==========

    def save_regime_log(self, regime_data: Dict[str, Any]):
        """保存市场环境日志 (每日一条, UPSERT by date)"""
        conn = self._conn()
        now = now_utc().isoformat()

        conn.execute("""
            INSERT INTO playbook_regime_log (
                date, regime, vix, spy_price, spy_ma20, spy_ma50,
                score, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                regime=excluded.regime,
                vix=excluded.vix,
                spy_price=excluded.spy_price,
                spy_ma20=excluded.spy_ma20,
                spy_ma50=excluded.spy_ma50,
                score=excluded.score,
                notes=excluded.notes
        """, (
            regime_data.get("date"),
            regime_data.get("regime"),
            regime_data.get("vix"),
            regime_data.get("spy_price"),
            regime_data.get("spy_ma20"),
            regime_data.get("spy_ma50"),
            regime_data.get("score"),
            regime_data.get("notes"),
            now,
        ))
        conn.commit()

    def get_regime_log(self, days: int = 30, include_markers: bool = False) -> List[Dict]:
        """获取近期 regime 日志

        include_markers=False (默认): 过滤掉非市场环境的幂等标记行 (周报任务借本表
            存幂等标记, regime='weekly_report' 且 date=未来周五, 会排到真实环境行之上),
            供 push/service 读"最新真实环境"。
        include_markers=True: 返回全部行 (供周报幂等自查等需要看到标记的场景)。
        """
        conn = self._conn()
        if include_markers:
            rows = conn.execute(
                "SELECT * FROM playbook_regime_log ORDER BY date DESC LIMIT ?",
                (days,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM playbook_regime_log WHERE regime NOT LIKE '%weekly%' "
                "ORDER BY date DESC LIMIT ?",
                (days,)
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ========== 汇总统计 ==========

    def get_summary_stats(self, days: int = 30) -> Dict[str, Any]:
        """获取汇总统计"""
        conn = self._conn()

        # 总指令数
        total = conn.execute(
            "SELECT COUNT(*) FROM playbook_instructions WHERE direction != 'SKIP'"
        ).fetchone()[0]

        # 执行了的
        executed = conn.execute(
            "SELECT COUNT(*) FROM playbook_instructions WHERE executed = 1"
        ).fetchone()[0]

        # 胜率
        wins = conn.execute(
            "SELECT COUNT(*) FROM playbook_instructions WHERE outcome = 'win'"
        ).fetchone()[0]
        losses = conn.execute(
            "SELECT COUNT(*) FROM playbook_instructions WHERE outcome = 'loss'"
        ).fetchone()[0]

        # 总盈亏 (2026-08-07 修复: 排除 outcome='void' — 19 笔 void 中 10 笔 pnl 非零
        #   (6/30-7/15 ghost 交易 BABA+63.5/COIN+77.2/AMZN-58.0), 合计 +$11.23 持续
        #   污染 total_pnl; void 本就不计入 win_rate 分母, 口径应一致)
        total_pnl = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM playbook_instructions "
            "WHERE executed = 1 AND COALESCE(outcome, '') != 'void'"
        ).fetchone()[0]

        decided = wins + losses
        win_rate = (wins / decided * 100) if decided > 0 else 0

        # 2026-08-07 grilling Q1: conf 分布统计 — DA 侧 confidence 恒 35 (8月 54 条
        #   零方差, 实测) = 无信息列, 任何 conf 判据/分层在 DA 侧失效; 此统计让
        #   恒值可观测, 下游切片/IC 分析时对零方差来源排除或单独标注。
        try:
            _conf_rows = conn.execute(
                "SELECT CASE WHEN decision_id LIKE 'DA-%' THEN 'DA' "
                "WHEN decision_id LIKE 'universe_expansion:%' THEN 'UE' "
                "WHEN decision_id LIKE 'intraday_reentry-%' THEN 'REENTRY' "
                "ELSE 'OTHER' END AS src, "
                "COUNT(*) AS n, ROUND(AVG(confidence), 1) AS avg_conf, "
                "MIN(confidence) AS mn, MAX(confidence) AS mx "
                "FROM playbook_instructions "
                "WHERE date >= date('now', '-30 day') "
                "GROUP BY src"
            ).fetchall()
            conf_stats = [
                {"src": r[0], "n": r[1], "avg_conf": r[2], "min": r[3], "max": r[4]}
                for r in _conf_rows
            ]
        except Exception:
            conf_stats = []

        return {
            "total_instructions": total,
            "executed": executed,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
            "conf_stats": conf_stats,
        }

    # ========== Horizon 统计 ==========

    def save_horizon_stat(self, stat: Dict[str, Any]):
        """写入/更新 horizon 统计"""
        conn = self._conn()
        now = now_utc().isoformat()

        conn.execute("""
            INSERT INTO playbook_horizon_stats (
                feature_key, horizon,
                total_trades, wins, losses, win_rate,
                avg_return, avg_pnl, profit_factor,
                bayesian_win_rate, bayesian_lower, bayesian_upper,
                last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(feature_key, horizon) DO UPDATE SET
                total_trades=excluded.total_trades,
                wins=excluded.wins,
                losses=excluded.losses,
                win_rate=excluded.win_rate,
                avg_return=excluded.avg_return,
                avg_pnl=excluded.avg_pnl,
                profit_factor=excluded.profit_factor,
                bayesian_win_rate=excluded.bayesian_win_rate,
                bayesian_lower=excluded.bayesian_lower,
                bayesian_upper=excluded.bayesian_upper,
                last_updated=excluded.last_updated
        """, (
            stat.get("feature_key"),
            stat.get("horizon"),
            stat.get("total_trades", 0),
            stat.get("wins", 0),
            stat.get("losses", 0),
            stat.get("win_rate", 0),
            stat.get("avg_return", 0),
            stat.get("avg_pnl", 0),
            stat.get("profit_factor", 0),
            stat.get("bayesian_win_rate"),
            stat.get("bayesian_lower"),
            stat.get("bayesian_upper"),
            now,
        ))
        conn.commit()

    def get_horizon_stats(self, feature_key: str = None, horizon: str = None) -> List[Dict]:
        """获取 horizon 统计"""
        conn = self._conn()
        query = "SELECT * FROM playbook_horizon_stats WHERE 1=1"
        params = []

        if feature_key:
            query += " AND feature_key = ?"
            params.append(feature_key)
        if horizon:
            query += " AND horizon = ?"
            params.append(horizon)

        query += " ORDER BY total_trades DESC"
        rows = conn.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]


# ========== 辅助函数 ==========

_TRACKING_FIELDS = {
    "triggered", "executed", "execution_price", "execution_time",
    "exit_price", "exit_time", "exit_reason", "holding_period_hours",
    "pnl", "pnl_pct",
    "max_favorable_excursion", "max_adverse_excursion", "mfe_analysis",
    "outcome", "shadow_pnl", "lesson",
    "decision_id",
    # Phase 1: IC 追踪
    "fwd_return_intraday", "fwd_return_t1", "fwd_return_t3", "fwd_return_t5",
    # Horizon 决策追踪
    "horizon", "horizon_confidence", "horizon_reasoning",
    # 2026-06-29: re-price 审计 — 记录触发时重新定价后的 entry/stop
    "entry_price", "stop_loss",
    # 2026-07-11: re-price 审计补全 — position_size/take_profit 此前被 whitelist 挡下,
    #   导致 bridge(DA) 路径触发时兜底算出的仓位/止盈从不落库 (审计断裂: 成交单已正确
    #   带 size 走 execute_instruction 的 custom_params, 但 playbook_instructions 审计列恒 0)。
    #   加入后 re-price 的 position_size/take_profit 与实际下单一致, 审计可追溯。
    #   (reprice_from/to/original_sl 非 DB 列, 仍由 whitelist 正确忽略。)
    "position_size", "take_profit",
    # 2026-07-13: risk_amount 派生落库 — re-price 时补算 |entry-SL|×position_size,
    #   此前 DA/bridge 路径恒 NULL (盘前推送 {risk:.0f} 曾崩). 让审计/推送/周报可见每笔风险敞口。
    "risk_amount",
    # 2026-07-17: 盘前定桶落库 — DA/UE 直写指令绕过 vol_bucket 分桶, 盘前用日线 ATR 定桶后
    #   落主表, 使桶分类可解释、可事后查询 (此前桶名仅落 shadow jsonl)。
    "vol_bucket", "atr_pct",
    # 2026-07-19 (DEB-round3 #2): thesis 失效作废 — 漂移>5% 写 skip_reason=
    #   'thesis_drift_invalidate' 永久作废指令, 需经白名单才能落库.
    "skip_reason",
    # 2026-07-19 (DEB-round3 #11): ic_tracker open 代理回填标记 (见迁移注释).
    "fwd_proxy",
    # 2026-07-24 (DEB-20260723 方向4): regime 落库补全 — DA/UE 直写指令三字段恒 NULL,
    #   盘前由 run_playbook_normalize_vol_buckets 统一补写 (只填 NULL 行, 幂等).
    "market_regime", "vix_at_generation", "spy_trend_at_generation",
    # 2026-08-01: open 基准前瞻收益 (见迁移注释 — 双价格基准口径).
    "fwd_return_intraday_open", "fwd_return_t1_open", "fwd_return_t3_open",
}


def _to_json(value) -> Optional[str]:
    """安全地将值转为 JSON 字符串"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _row_to_dict(row) -> Optional[Dict]:
    """将 sqlite3.Row 转为 dict, 自动解析 JSON 字段"""
    if row is None:
        return None
    d = dict(row)
    # 自动解析 JSON 字段
    for key in ("conditions_json", "risks_json", "data_quality_json",
                "matched_patterns_json", "raw_instruction_json",
                "co_factors_json"):
        if key in d and d[key]:
            try:
                d[key] = json.loads(d[key])
            except (json.JSONDecodeError, TypeError):
                pass
    return d


# ========== Schema SQL ==========

_CREATE_SIGNAL_CONTEXT_SQL = """
CREATE TABLE IF NOT EXISTS playbook_signal_context (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    -- 新闻
    news_headlines_json TEXT,       -- top5 标题列表
    news_sentiment REAL,            -- -1.0 ~ 1.0
    -- 内部人交易
    insider_signal TEXT,            -- strong_buy/buy/neutral/sell
    insider_cluster_score REAL,     -- 0-1
    insider_net_shares INTEGER,     -- 30日净买入
    insider_cluster_buyers_json TEXT,  -- 集群买入人列表
    -- 财报
    earnings_next_date TEXT,
    earnings_days_until INTEGER,
    earnings_beat_rate REAL,
    -- 基本面
    pe REAL,
    market_cap REAL,
    industry TEXT,
    revenue_growth_yoy REAL,
    roe REAL,
    debt_to_equity REAL,
    high_52w REAL,
    low_52w REAL,
    -- Deepulse 主题雷达 (per-symbol)
    theme_momentum REAL,            -- -1.0 ~ 1.0
    theme_wind REAL,                -- -1.0 ~ 1.0
    theme_narratives_json TEXT,     -- 关键叙事列表
    -- LLM 投票 reasoning (从 decisions.db 关联, 可能没命中)
    llm_reasoning_json TEXT,        -- [{model_id, direction, strength, confidence, reasoning}]
    -- 原始数据完整快照
    raw_data_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_signal_context_date ON playbook_signal_context(date);
"""

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS playbook_instructions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    skip_reason TEXT,

    -- 执行参数
    entry_price REAL,
    stop_loss REAL,
    take_profit REAL,
    position_size INTEGER,
    risk_amount REAL,
    risk_reward REAL,
    confidence INTEGER,

    -- 选股条件
    conditions_json TEXT,
    thesis TEXT,
    sector TEXT,
    risks_json TEXT,

    -- 市场环境
    market_regime TEXT,
    vix_at_generation REAL,
    spy_trend_at_generation TEXT,

    -- 数据质量
    data_quality_json TEXT,
    ok_dimensions INTEGER,

    -- Pattern 匹配
    matched_patterns_json TEXT,
    pattern_verdict TEXT,

    -- 追踪
    triggered INTEGER DEFAULT 0,
    executed INTEGER DEFAULT 0,
    execution_price REAL,
    execution_time TEXT,
    exit_price REAL,
    exit_time TEXT,
    exit_reason TEXT,
    holding_period_hours REAL,
    pnl REAL DEFAULT 0,
    pnl_pct REAL DEFAULT 0,

    -- MFE/MAE
    max_favorable_excursion REAL,
    max_adverse_excursion REAL,
    mfe_analysis TEXT,

    -- Horizon 决策 (2026-06-26)
    horizon TEXT DEFAULT 'short',
    horizon_confidence REAL,
    horizon_reasoning TEXT,

    -- 波动率分桶 (2026-07-17: 盘前用日线 ATR 定桶落库, 桶分类可解释可查)
    vol_bucket TEXT,
    atr_pct REAL,

    outcome TEXT,
    shadow_pnl REAL,

    -- IC 追踪 (Phase 1: 2026-06-19)
    fwd_return_intraday REAL,
    fwd_return_t1 REAL,
    fwd_return_t3 REAL,
    fwd_return_t5 REAL,

    -- 复盘
    lesson TEXT,

    -- LLM 原始输出
    raw_instruction_json TEXT,

    -- IC 归因 (关联 VotingCoordinator decision_id)
    decision_id TEXT DEFAULT '',

    created_at TEXT NOT NULL,
    updated_at TEXT,

    UNIQUE(date, symbol)
);

CREATE TABLE IF NOT EXISTS playbook_factor_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_key TEXT NOT NULL,
    analysis_level TEXT NOT NULL,
    co_factors_json TEXT,

    total_trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0,
    avg_pnl REAL DEFAULT 0,
    avg_pnl_pct REAL DEFAULT 0,
    total_pnl REAL DEFAULT 0,
    profit_factor REAL DEFAULT 0,
    max_win REAL DEFAULT 0,
    max_loss REAL DEFAULT 0,

    bayesian_win_rate REAL,
    bayesian_lower REAL,
    bayesian_upper REAL,
    effective_sample_size REAL,

    verdict TEXT DEFAULT 'observing',
    last_updated TEXT,

    UNIQUE(factor_key, analysis_level)
);

CREATE TABLE IF NOT EXISTS playbook_horizon_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_key TEXT NOT NULL,
    horizon TEXT NOT NULL,
    total_trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0,
    avg_return REAL DEFAULT 0,
    avg_pnl REAL DEFAULT 0,
    profit_factor REAL DEFAULT 0,
    bayesian_win_rate REAL,
    bayesian_lower REAL,
    bayesian_upper REAL,
    last_updated TEXT,
    UNIQUE(feature_key, horizon)
);

CREATE INDEX IF NOT EXISTS idx_playbook_horizon_stats_feature
    ON playbook_horizon_stats(feature_key);
CREATE INDEX IF NOT EXISTS idx_playbook_horizon_stats_horizon
    ON playbook_horizon_stats(horizon);

CREATE TABLE IF NOT EXISTS playbook_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_key TEXT NOT NULL UNIQUE,
    conditions_json TEXT NOT NULL,

    total_trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0,
    avg_pnl REAL DEFAULT 0,
    avg_pnl_pct REAL DEFAULT 0,
    max_win REAL DEFAULT 0,
    max_loss REAL DEFAULT 0,
    profit_factor REAL DEFAULT 0,

    bayesian_win_rate REAL,
    bayesian_lower REAL,
    bayesian_upper REAL,

    status TEXT DEFAULT 'observing',
    first_seen TEXT,
    last_seen TEXT,
    last_updated TEXT,
    weekly_notes TEXT
);

CREATE TABLE IF NOT EXISTS playbook_user_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instruction_id INTEGER NOT NULL REFERENCES playbook_instructions(id),
    action TEXT NOT NULL,
    skip_reason TEXT,
    modified_field TEXT,
    original_value TEXT,
    modified_value TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS playbook_regime_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    regime TEXT NOT NULL,
    vix REAL,
    spy_price REAL,
    spy_ma20 REAL,
    spy_ma50 REAL,
    score INTEGER,
    notes TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_playbook_instructions_date
    ON playbook_instructions(date);

CREATE INDEX IF NOT EXISTS idx_playbook_instructions_outcome
    ON playbook_instructions(outcome);

CREATE INDEX IF NOT EXISTS idx_playbook_factor_stats_level
    ON playbook_factor_stats(analysis_level);

CREATE INDEX IF NOT EXISTS idx_playbook_patterns_status
    ON playbook_patterns(status);
"""
