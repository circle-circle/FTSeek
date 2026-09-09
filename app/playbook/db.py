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
import re
import sqlite3
from typing import Dict, List, Optional, Any
from datetime import datetime

from app.storage.db_pool import get_db
from app.utils.symbol_utils import normalize_symbol
from app.utils.timezone import now_utc

logger = logging.getLogger(__name__)


# ── [DEB-20260902-stale-bridge P1-② 2026-09-02] 陈旧 decision_id 覆盖守卫 ────────
#   RFC-066 覆盖仲裁按源类分级 (DA-/universe_expansion/intraday_reentry=3), 对
#   decision_id 内嵌的决策日期无感 → 同 prio "同源类替换" 允许 T-1 决策覆盖当日决策。
#   9/1 实锤: DA skill 19:27 直写 LLY/META did=DA-202609011* 被服务侧 21:04 陈旧批
#   did=DA-202608311* 原地覆盖 (model_used 同步退化为 deterministic), 当日 DA 门对
#   BABA/BWXT 的 BLOCK 因此被绕过。历史面: 8/1 起 274 条 DA- 指令仅这 5 行 (全 9/1)
#   did 日期 ≠ date 列 → 守卫零误报史。
_DA_DID_RE = re.compile(r"^DA-(\d{4})(\d{2})(\d{2})")


def _did_trade_date(did: Any) -> str:
    """从 DA-YYYYMMDDHHMMSS-<symbol>-<hash> 取决策日 (YYYY-MM-DD); 非 DA 源/不可解析 → ''。"""
    m = _DA_DID_RE.match(str(did or ""))
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def _stale_did_guard_enabled() -> bool:
    """守卫开关 (默认开)。配置读失败 → 保持开启 (完整性保护优先, R5 warn 留痕)。"""
    try:
        from app.core.config_container import get_config as _gc_sd
        return bool(_gc_sd().get_dotted("playbook.stale_did_overwrite_guard_enabled", True))
    except Exception as _sd_e:
        logger.warning(f"[save_instruction] 陈旧did守卫开关读取失败 (保持开启): {_sd_e}")
        return True


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
            # RFC-066 (2026-08-25): 存量表移除 UNIQUE(date, symbol) — 多轮指令共存前提.
            self._migrate_drop_date_symbol_unique(conn)
            self._tables_created = True
            logger.debug("Playbook tables ensured")  # 2026-07-31 降噪: INFO→DEBUG (每 ensure 都打, 23条/11h)
        except Exception as e:
            logger.error(f"Failed to create playbook tables: {e}")
            raise

    def _migrate_drop_date_symbol_unique(self, conn):
        """RFC-066: 移除存量表的 UNIQUE(date, symbol) 约束 (SQLite 无 DROP CONSTRAINT → 重建).

        幂等: 表 sql 已无该约束则跳过。行 id 保持 (id 是 trade_records 的
        外键关联键, 绝不能变)。

        事务性 (Python 3.8 陷阱): 池连接是 legacy 隔离级别, DDL 会被隐式
        COMMIT → "INSERT 拷贝→DROP→RENAME" 无法原子化, 崩溃窗口可能留下
        孤儿表。故迁移用**专用连接** (isolation_level=None + 显式
        BEGIN IMMEDIATE/COMMIT), 全程单事务, 失败即整体回滚。
        另做崩溃恢复: 上次迁移半途崩溃留下的 __rfc066 孤儿表, 主表在则
        丢弃孤儿, 主表不在则孤儿转正 (数据不丢)。
        """
        def _exists(name):
            return conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone() is not None

        TMP = "playbook_instructions__rfc066"
        # ── 崩溃恢复 (上次迁移半途崩溃的残留处理) ──
        if _exists(TMP):
            if _exists("playbook_instructions"):
                conn.execute(f"DROP TABLE {TMP}")
                conn.commit()
                logger.warning("[playbook] RFC-066 迁移残留: 孤儿表已清理, 重新迁移")
            else:
                conn.execute(f"ALTER TABLE {TMP} RENAME TO playbook_instructions")
                conn.commit()
                logger.warning("[playbook] RFC-066 迁移崩溃恢复: 孤儿表转正为主表")

        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='playbook_instructions'"
        ).fetchone()
        if row is None:
            return  # 表不存在 (刚建的新库无约束)
        if "UNIQUE(date, symbol)" not in (row[0] or ""):
            return  # 已迁移或新建表, 无需处理

        # 专用连接 + 显式事务 (见函数 docstring 的 Python 3.8 说明)
        _main_path = None
        for _r in conn.execute("PRAGMA database_list").fetchall():
            if _r[1] == "main":
                _main_path = _r[2]
                break
        _in_memory = not _main_path  # :memory: 测试库 — 回退到同连接直执 (无崩溃原子性需求)
        if _in_memory:
            mig = conn
        else:
            mig = sqlite3.connect(_main_path, isolation_level=None, timeout=30.0)
        _own_conn = not _in_memory
        cols = [r[1] for r in conn.execute("PRAGMA table_info(playbook_instructions)").fetchall()]
        col_list = ", ".join(cols)

        try:
            mig.execute("BEGIN IMMEDIATE")
            mig.execute("""
                CREATE TABLE playbook_instructions__rfc066 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    skip_reason TEXT,
                    entry_price REAL, stop_loss REAL, take_profit REAL,
                    position_size INTEGER, risk_amount REAL, risk_reward REAL, confidence INTEGER,
                    conditions_json TEXT, thesis TEXT, sector TEXT, risks_json TEXT,
                    market_regime TEXT, vix_at_generation REAL, spy_trend_at_generation TEXT,
                    data_quality_json TEXT, ok_dimensions INTEGER,
                    matched_patterns_json TEXT, pattern_verdict TEXT,
                    triggered INTEGER DEFAULT 0, executed INTEGER DEFAULT 0,
                    execution_price REAL, execution_time TEXT,
                    exit_price REAL, exit_time TEXT, exit_reason TEXT, holding_period_hours REAL,
                    pnl REAL DEFAULT 0, pnl_pct REAL DEFAULT 0,
                    max_favorable_excursion REAL, max_adverse_excursion REAL, mfe_analysis TEXT,
                    horizon TEXT DEFAULT 'short', horizon_confidence REAL, horizon_reasoning TEXT,
                    vol_bucket TEXT, atr_pct REAL,
                    outcome TEXT, shadow_pnl REAL,
                    fwd_return_intraday REAL, fwd_return_t1 REAL, fwd_return_t3 REAL, fwd_return_t5 REAL,
                    lesson TEXT,
                    raw_instruction_json TEXT,
                    decision_id TEXT DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT,
                    config_version TEXT DEFAULT 'v1', invalidation TEXT,
                    fwd_return_intraday_open REAL, fwd_return_t1_open REAL, fwd_return_t3_open REAL,
                    entry_low REAL, entry_high REAL, fwd_proxy INTEGER, anchor_dev_pct REAL,
                    model_used TEXT, is_ab_test INTEGER, metadata_json TEXT, decision_conf REAL,
                    fwd_return_t1_adj REAL, fwd_return_t3_adj REAL, fwd_return_t5_adj REAL
                )
            """)
            # 列集合校验: 现有表列必须全部在新表中 (缺列 → 中止, 防数据丢失)
            _new_cols = {r[1] for r in mig.execute(
                "PRAGMA table_info(playbook_instructions__rfc066)").fetchall()}
            missing = [c for c in cols if c not in _new_cols]
            if missing:
                raise RuntimeError(f"RFC-066 迁移中止: 新表缺列 {missing} (防数据丢失)")
            mig.execute(
                f"INSERT INTO playbook_instructions__rfc066 ({col_list}) "
                f"SELECT {col_list} FROM playbook_instructions"
            )
            n_old = mig.execute("SELECT COUNT(*) FROM playbook_instructions").fetchone()[0]
            n_new = mig.execute(
                "SELECT COUNT(*) FROM playbook_instructions__rfc066").fetchone()[0]
            if n_old != n_new:
                raise RuntimeError(f"RFC-066 迁移中止: 行数不一致 old={n_old} new={n_new}")
            mig.execute("DROP TABLE playbook_instructions")
            mig.execute(
                "ALTER TABLE playbook_instructions__rfc066 RENAME TO playbook_instructions")
            mig.execute(
                "CREATE INDEX IF NOT EXISTS idx_playbook_instructions_date "
                "ON playbook_instructions(date)"
            )
            mig.execute(
                "CREATE INDEX IF NOT EXISTS idx_playbook_instructions_outcome "
                "ON playbook_instructions(outcome)"
            )
            mig.execute(
                "CREATE INDEX IF NOT EXISTS idx_playbook_instructions_date_symbol "
                "ON playbook_instructions(date, symbol)"
            )
            mig.execute("COMMIT")
            logger.info(
                f"[playbook] RFC-066 迁移完成: playbook_instructions 移除 UNIQUE(date,symbol), "
                f"{n_new} 行无损迁移, 多轮指令共存已启用"
            )
        except Exception:
            try:
                mig.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            if _own_conn:
                mig.close()
        # 池连接清缓存: 迁移后主表已被替换 (rootpage 变化), 避免陈旧句柄问题
        try:
            conn.commit()  # 结束池连接上任何遗留事务
        except Exception:
            pass

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
            # 2026-08-14 (DEB-20260814 P6 口径对齐): gate 校准后 decision_conf (0-1) 独立落库.
            #   根因 (Qoder §9.1-修正2 实测): 顶层 confidence 列写的是 LLM 语义 conf 或被
            #   WARN 降权后的值, 同 tier 落库 conf 不一致 → 下游按 confidence 分桶有测量误差.
            #   新增独立列承接 gate decision_conf (辩论一致度校准值), confidence 列语义不动
            #   (下游 conf<50=模型分歧代理等逻辑依赖), 消除测量误差且不破坏既有消费方.
            "decision_conf": "REAL",
            # 2026-08-22 (用户裁"落 T+N 影子字段"): 机制调整前瞻收益影子口径 —
            #   现有 fwd_return_t{N} 用纯 T+N 收盘价, 是"傻等到收盘"的悲观下界;
            #   本组影子列模拟"若按当前锁利(peak回撤0.8%)/净保本(+0.4%)/SL(-5%)机制
            #   逐日盯市, T+N 收盘前实际能拿到的收益". 日K OHLC 回放, 参数读 config 实值.
            #   用途: 收益类报告/锁利参数优化裁决用 _adj 口径; 方向 IC 仍用 fwd_return_t{N}.
            #   影子(record-only): 零消费方, 不动任何现有统计. 单位与 fwd_return_t{N} 一致(小数).
            "fwd_return_t1_adj": "REAL",
            "fwd_return_t3_adj": "REAL",
            "fwd_return_t5_adj": "REAL",
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
            row id (覆盖被仲裁拒绝时返回现存行 id)

        覆盖仲裁 (2026-08-17 DA覆盖事故根源修复, DEB-long-expectancy §十六):
            ON CONFLICT DO UPDATE WHERE 原子优先级阶梯 — 覆盖现在与未来全部写入方
            (取代散在 generate/topup 调用方的先读后写守卫, 消除 TOCTOU 窗口):
              优先级: DA-/universe_expansion*/intraday_reentry- = 3 (深度供给/重武装),
                      其他非空 decision_id (TFB-/SNAP-/DEC-) = 1, 空 (SKIP票) = 0。
              规则: 新行优先级 >= 现存行才允许覆盖; 另加深度语义 — UE 浅指令永远
              不得覆盖 DA 深度内容 (与 llm_universe_scan._is_da_priority_skip 同源,
              该调用方守卫保留作快路径跳过+精确计数, 本 SQL 为权威兜底);
              intraday_reentry- 重武装无条件放行 (合法重武装, 覆盖前自归档原行)。
            8/17 事故回放: 64 条空 decision_id SKIP 票 (prio 0) 覆盖 17 条 DA 行
            (prio 3) → 0>=3 拒绝, 事故在 SQL 层不可能再发生。
            回滚: 删除 WHERE 块即恢复旧无条件 UPSERT。
        """
        # 写入侧规范化: 折叠 US.US. 双前缀 + 归并别名(GOOGL->GOOG), 防数据割裂
        inst["symbol"] = normalize_symbol(inst.get("symbol"))

        # 准入门 (2026-08-06): 全写入方唯一收口点 — DA 直写/UE/topup/generate 四路都过这里。
        #   立项根因: confidence 门只写在 _build_instructions_from_votes 内 (pipeline.py:1264),
        #   DA/UE 直写 DB 绕过全部质量门 (实测 35 日 DA 219 条 BUY 平均 conf=36 全入库)。
        #   代码 fallback enforce=false; 生产 YAML 当前 enabled=true/enforce=true, 命中门槛会转 SKIP。
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

        # ── RFC-066 (2026-08-25): Python 层覆盖仲裁 (取代旧 ON CONFLICT(date,symbol) SQL) ──
        # 根因: 旧表 UNIQUE(date, symbol) 强制每标的每日单行, 盘中再入场重武装的
        #   UPSERT 整行覆盖并复位 executed/outcome/pnl → 第一轮已结算台账被抹除
        #   (8/7-8/24 25 行实锤, 下游 executed=1/outcome IN(win,loss) 统计全部漏计)。
        # 新语义: 约束已移除 (迁移见 _migrate_drop_date_symbol_unique), 同一 (date,
        #   symbol) 允许多轮共存, 仲裁规则 (保留旧优先级阶梯的可观察语义):
        #   ① 同 decision_id (非空) → 原地更新该行 (幂等重跑); 但已结算行
        #      (executed=1 且 outcome 终态) 永不被重写 — 结算是神圣的。
        #   ② 空 decision_id (SKIP 票) → 已有空 did 行则原地更新; 只有带源行 → 拒绝
        #      (8/17 SKIP 覆盖 DA 事故防护, 且避免 SKIP 与源行并存)。
        #   ③ intraday_reentry- → INSERT 新轮次行 (多轮共存, RFC-066 根因修复)。
        #   ④ 其他源: UE 遇组内 DA 行 → 拒绝; 新优先级 < 组内最高 → 拒绝; 否则原地
        #      更新最高优先级行 (同源类替换/高优先级覆盖, 与旧语义一致); 目标行已
        #      结算 → 拒绝 (不插入、不覆盖)。
        _did_new = str(inst.get("decision_id", "") or "")
        _date = inst.get("date")
        _symbol = inst.get("symbol")

        def _prio(did):
            d = str(did or "")
            if (d.startswith("DA-") or d.startswith("universe_expansion")
                    or d.startswith("intraday_reentry-")):
                return 3
            return 1 if d else 0

        _TERMINAL = ("win", "loss", "void")
        _existing = conn.execute(
            "SELECT id, decision_id, executed, outcome FROM playbook_instructions "
            "WHERE date = ? AND symbol = ? ORDER BY id",
            (_date, _symbol),
        ).fetchall()

        _target_id = None   # 原地更新目标行
        _do_insert = False
        if not _existing:
            _do_insert = True
        elif _did_new:
            _same = [r for r in _existing if (r[1] or "") == _did_new]
            if _same:
                _t = _same[-1]
                if _t[2] == 1 and (_t[3] or "") in _TERMINAL:
                    logger.info(
                        f"[save_instruction] RFC-066 拒绝重写: {_symbol} ({_date}) did={_did_new} "
                        f"行#{_t[0]} 已结算 (outcome={_t[3]}), 生成字段不重写"
                    )
                    return _t[0]
                _target_id = _t[0]
            elif _did_new.startswith("intraday_reentry-"):
                _do_insert = True  # 新轮次行与第一轮共存 (RFC-066)
            elif (_did_new.startswith("universe_expansion")
                    and any((r[1] or "").startswith("DA-") for r in _existing)):
                logger.info(
                    f"[save_instruction] 覆盖仲裁拒绝: {_symbol} ({_date}) "
                    f"UE 浅指令不得覆盖/并存 DA 深度内容 (did={_did_new})"
                )
                return _existing[-1][0]
            else:
                _max_prio = max(_prio(r[1]) for r in _existing)
                if _prio(_did_new) < _max_prio:
                    logger.info(
                        f"[save_instruction] 覆盖仲裁拒绝: {_symbol} ({_date}) "
                        f"新 did={_did_new} 优先级 {_prio(_did_new)} < 现存 {_max_prio}, 不写入"
                    )
                    return _existing[-1][0]
                _best = [r for r in _existing if _prio(r[1]) == _max_prio][-1]
                # [DEB-20260902-stale-bridge P1-② 2026-09-02] 同源类"新鲜度" tiebreaker:
                #   两侧 did 均可解析 DA 日期且 新 < 旧 → 拒绝覆盖 (返回现存行 id, 保留
                #   当日决策); 任一侧非 DA 源/不可解析 → 不介入 (fail-soft 保持既有阶梯语义)。
                #   回滚: playbook.stale_did_overwrite_guard_enabled=false。
                _new_did_date = _did_trade_date(_did_new)
                _old_did_date = _did_trade_date(_best[1])
                if (_new_did_date and _old_did_date
                        and _new_did_date < _old_did_date
                        and _stale_did_guard_enabled()):
                    logger.warning(
                        f"[save_instruction] 陈旧did覆盖拒绝: {_symbol} ({_date}) "
                        f"新 did={_did_new} (决策日 {_new_did_date}) < 现存行#{_best[0]} "
                        f"did={_best[1]} (决策日 {_old_did_date}) → 保留当日决策, 不写入"
                    )
                    return _best[0]
                if _best[2] == 1 and (_best[3] or "") in _TERMINAL:
                    logger.info(
                        f"[save_instruction] RFC-066 拒绝覆盖: {_symbol} ({_date}) 目标行#{_best[0]} "
                        f"已结算 (outcome={_best[3]}), 新 did={_did_new} 不写入"
                    )
                    return _best[0]
                _target_id = _best[0]
        else:
            _empties = [r for r in _existing if not (r[1] or "")]
            if _empties:
                _target_id = _empties[-1][0]
            else:
                logger.info(
                    f"[save_instruction] 覆盖仲裁拒绝: {_symbol} "
                    f"({_date}) 现存行有源, 空 decision_id 写入不覆盖"
                )
                return _existing[-1][0]

        _params = (
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
            inst.get("decision_conf"),
        )

        if _do_insert:
            cur = conn.execute("""
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
                    model_used, is_ab_test, metadata_json, decision_conf
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'v2', ?, ?, ?, ?, ?, ?)
            """, _params)
            conn.commit()
            return cur.lastrowid

        # 原地更新生成字段 (结算列不在列表内 — 与旧 ON CONFLICT SET 列表一致)
        conn.execute("""
            UPDATE playbook_instructions SET
                direction=?, skip_reason=?, entry_price=?, stop_loss=?, take_profit=?,
                entry_low=?, entry_high=?, position_size=?, risk_amount=?, risk_reward=?,
                confidence=?, conditions_json=?, thesis=?, sector=?, risks_json=?,
                market_regime=?, vix_at_generation=?, data_quality_json=?, ok_dimensions=?,
                raw_instruction_json=?, decision_id=?,
                horizon=?, horizon_confidence=?, horizon_reasoning=?,
                invalidation=?,
                anchor_dev_pct=COALESCE(?, anchor_dev_pct),
                model_used=COALESCE(?, model_used),
                is_ab_test=COALESCE(?, is_ab_test),
                metadata_json=COALESCE(?, metadata_json),
                decision_conf=COALESCE(?, decision_conf),
                updated_at=?
            WHERE id=?
        """, (
            _params[2], _params[3], _params[4], _params[5], _params[6],
            _params[7], _params[8], _params[9], _params[10], _params[11],
            _params[12], _params[13], _params[14], _params[15], _params[16],
            _params[17], _params[18], _params[20], _params[21],
            _params[24], _params[25],
            _params[26], _params[27], _params[28],
            _params[31],
            _params[32], _params[33], _params[34], _params[35], _params[36],
            now, _target_id,
        ))
        conn.commit()
        return _target_id

    def get_instructions_by_date(self, date: str) -> List[Dict]:
        """获取某日所有指令"""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM playbook_instructions WHERE date = ? ORDER BY id",
            (date,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def count_executed_by_direction(self, date: str, direction: str) -> int:
        """统计某日某方向已执行指令数 (议题二C 2026-08-01: BUY 日执行配额计数).

        [DEB-fullchain #20 2026-08-16] 排除 already_held 幻影行: broker 已有持仓
        (外部/手动/ORB) 的指令被记 executed=1 + execution_price=0 + outcome='already_held'
        (pipeline L3308), 未产生任何新订单却占用配额 → BUY 供给被无声扣减。"""
        conn = self._conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM playbook_instructions "
            "WHERE date = ? AND direction = ? AND executed = 1 "
            "AND COALESCE(outcome, '') != 'already_held'",
            (date, direction),
        ).fetchone()
        return int(row[0] or 0) if row else 0

    def mark_generate_completed(self, date: str) -> None:
        """[#22] 标记当日生成已完成 (幂等门依据; 异常中断不写, 允许重跑)."""
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO playbook_generate_state (date, completed_at) "
            "VALUES (?, ?)",
            (date, now_utc().isoformat()),
        )
        conn.commit()

    def is_generate_completed(self, date: str) -> bool:
        """[#22] 当日生成是否已完成 (完成标记, 与"是否有行"解耦)."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT 1 FROM playbook_generate_state WHERE date = ?", (date,)
            ).fetchone()
            return row is not None
        except Exception:
            return False

    def get_risk_state(self, key: str) -> Optional[str]:
        """[TZ-1] 读熔断锁存态 (无行/表未建 → None, 调用方按未熔断处理)."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT value FROM playbook_risk_state WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None
        except Exception:
            return None

    def set_risk_state(self, key: str, value: Optional[str]) -> None:
        """[TZ-1] 写/清熔断锁存态 (value=None → 删行, 即 reset 语义)."""
        conn = self._conn()
        if value is None:
            conn.execute("DELETE FROM playbook_risk_state WHERE key = ?", (key,))
        else:
            conn.execute(
                "INSERT INTO playbook_risk_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, value, now_utc().isoformat()),
            )
        conn.commit()

    def get_instruction(self, date: str, symbol: str) -> Optional[Dict]:
        """获取某日某标的指令.

        RFC-066: 多行台账下同一 (date, symbol) 可有多轮 — 本方法返回
        **第一轮** (最小 id, 即盘前计划行), 保持旧单行时代的可观察语义。
        需要全部轮次用 get_instructions_by_date 后按 symbol 过滤。
        """
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM playbook_instructions "
            "WHERE date = ? AND symbol = ? ORDER BY id ASC LIMIT 1",
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

    def get_recent_instructions(self, limit: int = 50, include_skip: bool = False) -> List[Dict]:
        """获取最近的指令

        2026-08-21: 口径过滤 — 表中自 6/14 起混入反事实审计基线占位行
        (direction='SKIP', skip_reason='非当前通道候选', model_used='deterministic',
        无 entry_price, 永不结算), 8/17 后约 50 条/日, 会吃掉 LIMIT 窗口并污染
        指令数/IC 统计. 默认过滤; 需要统计 SKIP 数的消费方传 include_skip=True.
        反事实脚本走 raw SQL 不受影响.
        """
        conn = self._conn()
        where = "" if include_skip else " WHERE direction != 'SKIP'"
        rows = conn.execute(
            f"SELECT * FROM playbook_instructions{where} ORDER BY date DESC, id LIMIT ?",
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

    def has_weekly_report_marker(self, week_end: str) -> bool:
        """DEB-20260904 #S7: 周报幂等标记精确查询 (regime='weekly_report' AND date=week_end).

        原实现借 get_regime_log(days=7, include_markers=True) — days 实为行数 LIMIT
        而非日期窗: 标记行掉出最新 7 行 → 误判未出周报 → 重复推送; 反之上周旧标记
        在窗内 → 误判已出 → 漏报。精确匹配消除两类假阳性/假阴性。
        """
        conn = self._conn()
        row = conn.execute(
            "SELECT 1 FROM playbook_regime_log "
            "WHERE regime = 'weekly_report' AND date = ? LIMIT 1",
            (week_end,),
        ).fetchone()
        return row is not None

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
    "fwd_return_t1_adj", "fwd_return_t3_adj", "fwd_return_t5_adj",
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
    updated_at TEXT

    -- RFC-066 (2026-08-25): 移除 UNIQUE(date, symbol)。
    --   旧约束强制每标的每日单行: 盘中再入场重武装的 UPSERT 整行覆盖并复位
    --   executed/outcome/pnl → 第一轮已结算台账被抹除 (8/7-8/24 25 行实锤,
    --   下游 executed=1/outcome IN(win,loss) 统计全部漏计)。多轮共存后同一
    --   (date, symbol) 每轮一行 (各自独立 decision_id), 覆盖仲裁下沉到
    --   save_instruction Python 层, "已执行/已终态行永不覆盖"为硬规则。
    --   存量表迁移见 ensure_tables → _migrate_drop_date_symbol_unique。
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

-- [DEB-fullchain #22 2026-08-16] 生成完成标记: 幂等门从"任意行存在"改为"生成完成",
-- UE 盘前写入不再锁死 pipeline 生成 (中断的半成品也可重跑)。
CREATE TABLE IF NOT EXISTS playbook_generate_state (
    date TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL
);

-- [TZ-1 2026-08-30 DEB-timezone-duplication-audit] 熔断器锁存持久化:
-- RiskCircuitBreaker._stop_until 原为纯内存实例字段, 唯一调用方 (pipeline 周报) 每轮
-- 新建实例 → "连亏5停开仓1日"的窗口从未跨调用生效 (实测 2026-08-30)。
-- 本表存熔断锁存态; value=None 语义用 DELETE 行表达 (reset)。
CREATE TABLE IF NOT EXISTS playbook_risk_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
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
