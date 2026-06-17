"""
美股作战计划管理器 (US Battle Plan Manager)

职责:
1. 加载 `daily_battle_plan_us.json`。
2. 提供 `check_trade` 接口供调度器调用，判断是否允许交易。
3. 支持计划状态的动态更新 (由 OpenChecker 更新)。
"""

import os
import json
import logging
from datetime import datetime
from typing import Dict, Tuple, List, Any

logger = logging.getLogger("ftseek.us_pipeline.battle_plan_manager")

class USBattlePlanManager:
    """美股作战计划管理器 (Singleton)"""
    
    _instance = None
    _plan: Dict = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(USBattlePlanManager, cls).__new__(cls)
            cls._instance._loaded = False
        return cls._instance

    def load(self, plan_path: str) -> bool:
        """加载作战计划

        P0 修复 (2026-04-22): 检测过期计划 (>24h) 并自动降级为宽松模式
        P1 修复 (2026-05-01): 加载后强制质量校验 (entry_zone/ SL/TP 合法性)
        """
        try:
            if not os.path.exists(plan_path):
                logger.warning(f"⚠️ 未找到作战计划文件: {plan_path}")
                self._plan = None
                self._generate_sim_fallback()
                return False

            with open(plan_path, 'r', encoding='utf-8') as f:
                self._plan = json.load(f)

            plan_date = self._plan.get('date', '')
            logger.info(f"✅ 已加载美股作战计划 ({plan_date})")

            # P1 修复 (2026-05-01): 加载后强制质量校验
            validation_issues = self.validate_plan()
            if validation_issues:
                logger.warning(f"[计划质检] 发现 {len(validation_issues)} 个问题:")
                for issue in validation_issues:
                    logger.warning(f"  ⚠️ {issue}")
            else:
                logger.info("[计划质检] ✅ 作战计划数据完整性校验通过")

            # P0 修复 (2026-04-22): 检测计划是否过期
            if plan_date:
                from datetime import datetime, timedelta
                try:
                    plan_dt = datetime.strptime(plan_date, "%Y-%m-%d")
                    now = datetime.now()
                    age_days = (now - plan_dt).days
                    
                    if age_days > 1:
                        logger.warning(
                            f"⚠️ 作战计划已过期 {age_days} 天 (计划日期: {plan_date})! "
                            f"自动降级为宽松模式: 允许所有 watchlist 标的"
                        )
                        # 降级: 将所有 watchlist 标的加入 targets (status=ACTIVE)
                        self._activate_all_watchlist()
                        self._loaded = True
                        # Task #19: confidence 统一 + ConvictionTracker 初始化 (过期路径)
                        self._normalize_confidence()
                        self._init_conviction_tracker()
                        return True
                except ValueError:
                    logger.debug(f"[计划] 日期格式解析失败: {plan_date}")
            
            self._loaded = True
            # Task #19: confidence 统一为 0-100 整数 + 初始化 ConvictionTracker
            self._normalize_confidence()
            self._init_conviction_tracker()
            return True
        except Exception as e:
            logger.error(f"❌ 加载作战计划失败: {e}")
            self._plan = None
            self._loaded = False
            return False

    @property
    def plan_path(self) -> str:
        """获取计划文件路径 (统一使用 project_root/data/)"""
        import os
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            'data', 'daily_battle_plan_us.json'
        )

    # plan_file 别名 (兼容旧引用)
    plan_file = plan_path
    
    def save(self, plan: Dict) -> bool:
        """保存作战计划到文件
        
        P0 修复 (2026-05-04): 强制覆盖 date 为今天，防止 LLM arbiter 返回错误日期。
        P1 修复 (2026-05-13): 保存前自动清理 targets/restricted 冲突，防止 LLM 生成矛盾计划。
        """
        try:
            from datetime import datetime
            import os
            # P0 修复: 强制使用今天日期，防止 LLM 返回 2024 等过期日期
            plan["date"] = datetime.now().strftime("%Y-%m-%d")
            # P1 修复: 保存前清理 targets/restricted 冲突 (targets 优先)
            target_symbols = {t.get("symbol") for t in plan.get("targets", [])}
            restricted = set(plan.get("restricted", []))
            conflicts = target_symbols & restricted
            if conflicts:
                plan["restricted"] = list(restricted - conflicts)
                logger.info(f"[BattlePlan·保存前自修复] 从 restricted 移除 {len(conflicts)} 个冲突: {sorted(conflicts)}")
            os.makedirs(os.path.dirname(self.plan_path), exist_ok=True)
            with open(self.plan_path, 'w', encoding='utf-8') as f:
                json.dump(plan, f, ensure_ascii=False, indent=2)
            self._plan = plan
            self._loaded = True
            logger.info(f"[BattlePlan] ✅ 计划已保存: {self.plan_path}")
            return True
        except Exception as e:
            logger.error(f"[BattlePlan] ❌ 保存计划失败: {e}")
            return False
    
    def _activate_all_watchlist(self):
        """P0 修复 (2026-05-04): 过期计划降级 — 将所有 watchlist 标的加入 targets。
        
        此方法在 load() 第71行被调用但之前从未定义，导致 AttributeError。
        复用 get_plan() 中已有的 watchlist fallback 逻辑。
        """
        try:
            from app.utils.watchlist_manager import get_stock_codes
            codes = get_stock_codes()
            if self._plan and "targets" in self._plan:
                existing = {t.get("symbol") for t in self._plan["targets"]}
            else:
                existing = set()
            
            new_targets = []
            for code in codes:
                if code not in existing:
                    new_targets.append({
                        "symbol": code,
                        "direction": "ANY",
                        "status": "ACTIVE",
                        "entry_zone": [],
                        "stop_loss": 0,
                        "take_profit": 0,
                        "urgency": "NORMAL",
                        "catalyst": "Watchlist fallback (plan expired)"
                    })
            
            if self._plan and "targets" in self._plan:
                self._plan["targets"].extend(new_targets)
            else:
                self._plan = {"date": datetime.now().strftime("%Y-%m-%d"), "targets": new_targets, "restricted": []}
            
            logger.info(f"[计划降级] 已激活 {len(new_targets)} 只 watchlist 标的 (计划过期)")
        except ImportError:
            logger.warning("[计划降级] watchlist_manager 不可用，跳过降级")
        except Exception as e:
            logger.error(f"[计划降级] 降级失败: {e}")

    # P1.4 (RFC-060): 已删除重复的死代码 get_plan (与 _activate_all_watchlist 重复, 且无 return).
    # 过期计划降级在 load() (见 _activate_all_watchlist 调用) 正常生效; get_plan() 唯一定义在后段 (只读 getter).
    def _generate_sim_fallback(self):
        """生成模拟期兜底计划"""
        try:
            from app.utils.watchlist_manager import get_stock_codes
            codes = get_stock_codes()
            targets = [{"symbol": s, "direction": "ANY", "status": "VALID"} for s in codes]
            self._plan = {"date": "Simulation", "targets": targets, "restricted": []}
            self._loaded = True
            logger.warning(f"🤖 [US Sim Fallback] 已生成万能计划 (包含 {len(targets)} 只标的)")
        except Exception as e:
            logger.error(f"生成 US 模拟兜底计划失败: {e}")

    def is_trading_halted(self) -> Tuple[bool, str]:
        """P0 修复 (2026-05-20): 检测系统级交易熔断
        
        检查 updated_position_actions 中是否有 SYSTEM HALT_TRADING 指令。
        供 battle_plan_check 在已持仓 bypass 之前调用。
        
        Returns:
            (halted: bool, reason: str)
        """
        if not self._loaded or not self._plan:
            return False, ""
        for action_item in self._plan.get("updated_position_actions", []):
            if (isinstance(action_item, dict)
                    and action_item.get("action") == "HALT_TRADING"
                    and action_item.get("symbol", "").upper() == "SYSTEM"):
                return True, (
                    f"HALT_TRADING: {action_item.get('reason', 'System trading halted by battle plan')}"
                )
        return False, ""

    # ===== Task #19: 决策引擎集成辅助 =====
    def _normalize_confidence(self) -> None:
        """统一 confidence 为 0-100 整数 (向后兼容 0-1 浮点)。"""
        if not self._plan:
            return
        try:
            for target in self._plan.get("targets", []) or []:
                if not isinstance(target, dict):
                    continue
                conf = target.get("confidence", 0)
                if isinstance(conf, bool):
                    continue
                if isinstance(conf, float):
                    if 0 < conf < 1.0:
                        target["confidence"] = int(round(conf * 100))
                    else:
                        target["confidence"] = int(round(conf))
                elif isinstance(conf, int):
                    # 如果是 0/1 但可能是诡异代码路径，保留
                    target["confidence"] = conf

            plan_conf = self._plan.get("confidence", 0)
            if isinstance(plan_conf, float):
                if 0 < plan_conf < 1.0:
                    self._plan["confidence"] = int(round(plan_conf * 100))
                else:
                    self._plan["confidence"] = int(round(plan_conf))
        except Exception as _e:
            logger.debug(f"[计划] confidence 统一化异常(忽略): {_e}")

    def _init_conviction_tracker(self) -> None:
        """从当前 plan 初始化 ConvictionTracker (失败不阻断)。"""
        if not self._plan:
            return
        try:
            from app.plan.conviction_tracker import get_conviction_tracker
            tracker = get_conviction_tracker()
            tracker.init_from_plan(self._plan)
            logger.info(
                f"[ConvictionTracker] 已从 plan 初始化 "
                f"{len(self._plan.get('targets', []) or [])} targets"
            )
        except Exception as _e:
            logger.debug(f"[ConvictionTracker] 初始化异常(忽略): {_e}")

    def check_trade(self, symbol: str, direction: str, strength: int = 0, confidence: float = 0.0) -> Tuple[bool, str]:
        """
        检查是否允许交易
        
        Args:
            symbol: e.g. "US.NVDA"
            direction: "BUY" or "SELL"
            strength: 技术面信号强度 (0-100)
            confidence: 技术面置信度 (0.0-1.0)
            
        Returns:
            (allowed: bool, reason: str)
        """
        if not self._loaded or not self._plan:
            return True, "Plan not loaded, bypass check."

        # P0 修复 (2026-05-20): 检测 HALT_TRADING 指令
        # 辩论报告中的 updated_position_actions 可能包含 {symbol: "SYSTEM", action: "HALT_TRADING"}
        # 此前 check_trade() 完全不读此字段，导致熔断指令形同虚设
        for action_item in self._plan.get("updated_position_actions", []):
            if (isinstance(action_item, dict)
                    and action_item.get("action") == "HALT_TRADING"
                    and action_item.get("symbol", "").upper() == "SYSTEM"):
                return False, (
                    f"HALT_TRADING: {action_item.get('reason', 'System trading halted by battle plan')}"
                )

        # Task #19: Day Score 门禁 (graceful) — 今日市场适合度低于 AVOID 则拒绝新仓
        try:
            from app.plan.day_score import calculate_day_score
            day = calculate_day_score(
                available_cash=getattr(self, '_available_cash', 100000),
                position_count=getattr(self, '_position_count', 0),
            )
            if day.level == "AVOID":
                _reason_brief = '; '.join(day.reasons[:2]) if day.reasons else day.level
                logger.warning(
                    f"[DayScore] {symbol} 被拒绝: 今日评分{day.score}/100 (AVOID) - {_reason_brief}"
                )
                return False, f"[DayScore] 今日评分{day.score}/100 ({day.level}): {_reason_brief}"
        except Exception as _de:
            logger.debug(f"[DayScore] 计算异常(忽略): {_de}")

        # ── 修复 (2026-06-12): Dashboard 暂停/确认按钮真正生效 ──
        # 此前 _paused 和 _confirmed 写入 JSON 后无任何模块消费，按钮形同虚设
        # 修复: check_trade() 从磁盘实时读取这两个 flag (Dashboard 直接写文件)
        # 注意: 不能只读内存 self._plan, 因为 Dashboard 写入文件后 singleton 不会自动刷新
        _dashboard_flags = {}
        try:
            if self.plan_path and os.path.exists(self.plan_path):
                with open(self.plan_path, 'r') as _f:
                    _raw = json.load(_f)
                    _dashboard_flags = {
                        "_paused": _raw.get("_paused", False),
                        "_paused_at": _raw.get("_paused_at", ""),
                        "_confirmed": _raw.get("_confirmed", False),
                        "_require_confirm": _raw.get("_require_confirm", False),
                    }
        except Exception:
            # 读磁盘失败则降级用内存缓存
            _dashboard_flags = {
                "_paused": self._plan.get("_paused", False),
                "_paused_at": self._plan.get("_paused_at", ""),
                "_confirmed": self._plan.get("_confirmed", False),
                "_require_confirm": self._plan.get("_require_confirm", False),
            }

        # 暂停: Dashboard "今日暂停" 按钮 → _paused=true → 拒绝所有新仓
        if _dashboard_flags.get("_paused", False):
            paused_at = _dashboard_flags.get("_paused_at", "")
            logger.warning(f"[Plan Gate] {symbol} 被拒绝: 今日交易已暂停 (paused_at={paused_at})")
            return False, (
                f"Trading paused: 用户在 Dashboard 点击了「今日暂停」 "
                f"({paused_at}). 所有新仓位被阻止。"
            )

        # 确认: 如果计划标记需要确认但尚未确认 → 阻止新仓
        # _require_confirm 默认为 false (自动执行模式, 与现有行为兼容)
        # 用户在 Dashboard 编辑 plan 或 LLM 输出时可设为 true (需人工审批)
        require_confirm = _dashboard_flags.get("_require_confirm", False)
        if require_confirm and not _dashboard_flags.get("_confirmed", False):
            logger.warning(f"[Plan Gate] {symbol} 被拒绝: 计划需要人工确认后才能执行")
            return False, (
                f"Awaiting confirmation: 战前计划标记了需要人工确认 "
                f"(_require_confirm=true). 请在 Dashboard 点击「确认执行」。"
            )

        # ── 新增 (2026-06-12): 禁止交易窗口检查 ──
        # 经济数据发布期间 (CPI/FOMC/NFP 等), 波动率飙升, 技术分析失效
        # no_trade_windows 由 EconomicCalendar 生成, PlanValidator 注入到 plan
        no_trade_windows = self._plan.get("no_trade_windows", [])
        if no_trade_windows:
            try:
                from datetime import datetime as _ntw_dt
                import pytz as _ntw_pz
                now_et = _ntw_dt.now(_ntw_pz.timezone('US/Eastern')).strftime("%H:%M")
                for window in no_trade_windows:
                    w_start = window.get("start", "")
                    w_end = window.get("end", "")
                    if w_start and w_end and w_start <= now_et <= w_end:
                        reason = window.get("reason", "Economic event")
                        impact = window.get("impact", "HIGH")
                        logger.warning(
                            f"[Plan Gate] {symbol} 处于禁止交易窗口: "
                            f"{w_start}-{w_end} ET ({reason})"
                        )
                        return False, (
                            f"No-trade window: {reason} "
                            f"({w_start}-{w_end} ET, {impact} impact). New positions blocked."
                        )
            except Exception as ntw_err:
                logger.debug(f"[Plan Gate] 禁止交易窗口检查异常 (不阻断): {ntw_err}")

        # P0 修复 (2026-04-22): ETF 标的 (SPY/QQQ/DIA/IWM/UVXY) 不受 restricted 限制
        # 这些 ETF 用于市场状态监测，不应被计划门禁拦截
        # P0 修复 (2026-05-27): 移到 restricted 检查之前 — 否则 ETF 被 restricted 拦截
        ETF_WHITELIST = {"US.SPY", "US.QQQ", "US.DIA", "US.IWM", "US.UVXY"}
        if symbol in ETF_WHITELIST:
            return True, f"ETF whitelist bypass ({symbol})"

        # 1. Check Restricted List (合并 restricted + updated_restricted)
        # P0 修复 (2026-05-20): 辩论报告写入 updated_restricted 但 check_trade 只读 restricted (空)
        restricted = set(self._plan.get("restricted", []))
        restricted.update(self._plan.get("updated_restricted", []))
        if symbol in restricted:
            return False, f"Symbol {symbol} is on the restricted list."

        # 2. Check Targets
        targets = self._plan.get("targets", [])
        target = None
        for t in targets:
            if t.get("symbol") == symbol:
                target = t
                break
        
        if not target:
            # 方案 A: 计划外标的 → 技术面高评分后门
            # P0-辩论修复: strength 统一 direct 语义 (LONG/SHORT 均同)
            # 越高 = 信号越强, 无需反转
            trade_dir = "LONG" if direction.upper() == "BUY" else "SHORT"
            if strength >= 5:  # 模拟盘: 50→5, 极度放宽
                return True, (
                    f"Plan bypass ({trade_dir}): {symbol} not in battle plan, "
                    f"but signal is strong (strength={strength} >= 5)"
                )
            return False, (
                f"Symbol {symbol} not in battle plan "
                f"(strength={strength}/50 — below bypass threshold)"
            )

        # 3. Check Status (Pre-market plan status)
        # Later, OpenChecker might update this to "CANCELLED" or "EXECUTABLE"
        status = target.get("status", "PRE_MARKET")
        # P0 修复 (2026-05-27): open_check_status=CANCELLED → 视同"不在计划中"
        # 根因: OpenChecker 写入 open_check_status="CANCELLED" 但 check_trade 不读此字段
        # → NVDA SHORT 79 被方向冲突拦截 (plan=LONG), 但 open_check 已取消 LONG 方向
        # 修复: CANCELLED target 降级为"不在计划中", 走 strength bypass 路径
        open_status = target.get("open_check_status", "")
        if open_status == "CANCELLED" and status != "CANCELLED":
            cancel_reason = target.get("cancellation_reason", "OpenChecker cancelled")
            logger.info(f"[Plan Gate] {symbol} open_check=CANCELLED ({cancel_reason}), 降级为计划外")
            # 直接走 strength bypass 逻辑 (与上方 "not target" 分支一致)
            trade_dir = "LONG" if direction.upper() == "BUY" else "SHORT"
            if strength >= 50:
                return True, (
                    f"Plan bypass ({trade_dir}): {symbol} open_check cancelled, "
                    f"signal is strong (strength={strength} >= 50)"
                )
            return False, (
                f"Symbol {symbol} plan cancelled by OpenChecker "
                f"(strength={strength}/50 — below bypass threshold)"
            )
        if status == "CANCELLED":
            reason = target.get("cancellation_reason", "Cancelled by OpenChecker.")
            return False, f"Plan cancelled for {symbol}: {reason}"

        # 4. Check Direction Match (Optional strictness)
        # If plan says LONG, should we allow SELL (shorting)? 
        # For now, allow both if it's in the plan, but flag divergence.
        plan_direction = target.get("direction", "").upper()
        trade_direction = "LONG" if direction.upper() == "BUY" else "SHORT"
        
        if plan_direction != "ANY" and plan_direction != trade_direction:
            # Warning level divergence
            logger.warning(f"⚠️ 方向背离: Plan={plan_direction}, Signal={trade_direction} for {symbol}")
            # We can still allow it if the system supports it, but strictly speaking,
            # we might want to block counter-trend trades against the plan.
            # Let's block for safety in this "Plan-Driven" architecture.
            return False, f"Direction mismatch: Plan suggests {plan_direction}, Signal is {trade_direction}."

        return True, f"Plan approved ({plan_direction})."

    def get_target_info(self, symbol: str) -> Dict:
        """获取目标详细信息"""
        if not self._plan:
            return {}
        for t in self._plan.get("targets", []):
            if t.get("symbol") == symbol:
                return t
        return {}
    
    def get_plan(self) -> Dict:
        """获取当前计划"""
        return self._plan.copy() if self._plan else {}

    def validate_plan(self) -> List[str]:
        """P1 修复 (2026-05-01): 作战计划质量校验

        检查项:
        1. entry_zone 不为 None, 且为包含 2 个数字的列表
        2. stop_loss / take_profit 在 entry_zone 中点的 ±30% 范围内
        3. targets 中无重复 symbol
        4. restricted 中标的与 targets 不冲突

        Returns:
            问题描述列表 (空列表 = 通过)
        """
        issues = []
        if not self._plan:
            return ["计划为空"]

        targets = self._plan.get("targets", [])
        if not targets:
            issues.append("targets 为空, 无交易目标")
            return issues

        seen_symbols = set()
        restricted = set(self._plan.get("restricted", []))

        for i, t in enumerate(targets):
            symbol = t.get("symbol", f"target[{i}]")

            # 检查重复
            if symbol in seen_symbols:
                issues.append(f"[{symbol}] 在 targets 中重复出现")
            seen_symbols.add(symbol)

            # 检查 entry_zone
            entry_zone = t.get("entry_zone")
            if entry_zone is None:
                issues.append(f"[{symbol}] entry_zone 为 None (无效占位符)")
            elif not isinstance(entry_zone, list) or len(entry_zone) != 2:
                issues.append(f"[{symbol}] entry_zone 格式错误: {entry_zone}")
            else:
                try:
                    low, high = float(entry_zone[0]), float(entry_zone[1])
                    if low <= 0 or high <= 0:
                        issues.append(f"[{symbol}] entry_zone 包含非正数: [{low}, {high}]")
                    elif low >= high:
                        issues.append(f"[{symbol}] entry_zone 区间无效: low={low} >= high={high}")
                    else:
                        mid = (low + high) / 2.0

                        # 检查 SL/TP 合理性
                        sl = t.get("stop_loss")
                        tp = t.get("take_profit")
                        if sl is not None and isinstance(sl, (int, float)) and sl > 0:
                            sl_dev = abs(sl - mid) / mid
                            if sl_dev > 0.30:
                                issues.append(
                                    f"[{symbol}] stop_loss=${sl:.2f} 偏离 entry_zone 中点 ${mid:.2f} "
                                    f"({sl_dev*100:.0f}% > 30%), 疑似残留旧值"
                                )
                        if tp is not None and isinstance(tp, (int, float)) and tp > 0:
                            tp_dev = abs(tp - mid) / mid
                            if tp_dev > 0.30:
                                issues.append(
                                    f"[{symbol}] take_profit=${tp:.2f} 偏离 entry_zone 中点 ${mid:.2f} "
                                    f"({tp_dev*100:.0f}% > 30%), 疑似残留旧值"
                                )

                        # 检查 direction 一致性
                        direction = t.get("direction", "").upper()
                        if direction == "SHORT" and sl <= mid:
                            issues.append(f"[{symbol}] SHORT 方向的 stop_loss=${sl:.2f} 应在 entry_zone 中点 ${mid:.2f} 上方")
                        if direction == "LONG" and 0 < sl < mid * 0.7:
                            issues.append(f"[{symbol}] LONG 方向的 stop_loss=${sl:.2f} 异常偏低 (entry_zone 中点 ${mid:.2f})")

                except (TypeError, ValueError) as e:
                    issues.append(f"[{symbol}] entry_zone 数值解析失败: {entry_zone} ({e})")

            # 检查 status
            status = t.get("status", "")
            if status == "CANCELLED":
                issues.append(f"[{symbol}] 状态为 CANCELLED 但仍存在于 targets (应移入 cancelled_targets)")

        # 检查 restricted 与 targets 冲突
        for s in seen_symbols & restricted:
            issues.append(f"[{s}] 同时出现在 targets 和 restricted 中 (冲突)")

        return issues

    def revalidate_on_restart(self) -> bool:
        """P1 修复 (2026-05-01): 服务重启时重新校验并修复计划

        在 main.py 重启流程中调用, 对已有计划做完整性检查。
        发现问题时尝试自动修复 (如移除 stale targets)。

        Returns:
            True if plan is valid (possibly after auto-fix)
        """
        issues = self.validate_plan()
        if not issues:
            return True

        logger.warning(f"[计划·重启校验] 发现 {len(issues)} 个问题, 尝试自动修复...")

        # 自动修复: 移除 CANCELLED 状态的 targets
        if self._plan and "targets" in self._plan:
            original_count = len(self._plan["targets"])
            self._plan["targets"] = [
                t for t in self._plan["targets"]
                if t.get("status", "") != "CANCELLED"
            ]
            removed = original_count - len(self._plan["targets"])
            if removed > 0:
                logger.info(f"[计划·自动修复] 移除 {removed} 个 CANCELLED targets")

            # 修复: 移除 entry_zone=None 或无效的 targets
            def _valid_entry_zone(t):
                ez = t.get("entry_zone")
                if ez is None:
                    return False
                if not isinstance(ez, list) or len(ez) != 2:
                    return False
                try:
                    low, high = float(ez[0]), float(ez[1])
                    return low > 0 and high > 0 and low < high
                except (TypeError, ValueError):
                    return False
            self._plan["targets"] = [
                t for t in self._plan["targets"]
                if _valid_entry_zone(t)
            ]

        # 修复: 移除 targets 与 restricted 的冲突
        # 策略: targets 优先 (Agent 明确建议交易的标的)，从 restricted 中移除冲突项
        if self._plan:
            target_symbols = {t.get("symbol") for t in self._plan.get("targets", [])}
            restricted = set(self._plan.get("restricted", []))
            conflicts = target_symbols & restricted
            if conflicts:
                self._plan["restricted"] = list(restricted - conflicts)
                logger.info(f"[计划·自动修复] 从 restricted 移除 {len(conflicts)} 个冲突标的: {sorted(conflicts)}")

        # 重新校验
        remaining = self.validate_plan()
        if remaining:
            logger.warning(f"[计划·重启校验] 自动修复后仍剩余 {len(remaining)} 个问题 (需人工介入)")
            for issue in remaining:
                logger.warning(f"  ⚠️ {issue}")
            return False

        logger.info("[计划·重启校验] ✅ 自动修复完成, 计划已恢复有效状态")
        # P0 修复 (2026-05-06): 持久化修复后的计划到磁盘
        self.save(self._plan)
        logger.info("[计划·重启校验] ✅ 修复后的计划已保存到磁盘")
        return True
