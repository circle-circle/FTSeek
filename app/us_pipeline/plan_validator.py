"""
作战计划校验器 (Plan Validator)

职责:
1. 校验 LLM 生成的作战计划是否符合技术约束
2. 自动修正不合理的入场/止损/止盈价格
3. 标记风险信号 (RSI超买/超卖、板块逆风等)
4. 生成校验报告 (供人工审核)

复用:
- PreMarketScanner: 提供技术指标和板块数据
- EconomicCalendar: 提供经济事件和禁止交易窗口

校验规则:
- RSI Guard: RSI>70 做多 / RSI<30 做空 → FLAG
- 入场区偏离: 入场价偏离现价 >5% → REJECT
- 止损过窄: 止损距离 < 1.0×ATR → 自动修正到 1.5×ATR
- 止盈不足: R:R < 1.5 → 自动修正到 2.0
- 板块逆风: 做多弱势板块 / 做空强势板块 → FLAG
- 趋势矛盾: 做多下跌趋势 / 做空上升趋势 → FLAG
- 禁止交易窗口: 在经济事件窗口内 → REJECT
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
import pytz

logger = logging.getLogger("ftseek_us.us_pipeline.plan_validator")


class PlanValidator:
    """作战计划校验器"""
    
    def __init__(
        self,
        premarket_scanner,
        economic_calendar,
        scan_cache_path: str = "data/premarket_scan_cache.json"
    ):
        """
        初始化校验器
        
        Args:
            premarket_scanner: USPreMarketScanner 实例
            economic_calendar: EconomicCalendar 实例
            scan_cache_path: 盘前扫描缓存路径
        """
        self.scanner = premarket_scanner
        self.calendar = economic_calendar
        self.scan_cache_path = Path(scan_cache_path)
    
    def validate_plan(
        self,
        plan: Dict[str, Any],
        scan_result: Optional[Dict[str, Any]] = None
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """
        校验作战计划
        
        Args:
            plan: LLM 生成的作战计划
            scan_result: 盘前扫描结果 (可选，如不提供则从缓存加载)
        
        Returns:
            (修正后的计划, 校验报告列表)
            
            校验报告格式:
            [
                {
                    "symbol": "AAPL",
                    "rule": "RSI Guard",
                    "severity": "FLAG",  # FLAG / REJECT / FIXED
                    "message": "RSI=75.2 (超买)，做多风险较高",
                    "auto_fixed": False
                },
                ...
            ]
        """
        if scan_result is None:
            scan_result = self._load_scan_cache()
        
        if not scan_result:
            logger.warning("无盘前扫描数据，跳过校验")
            return plan, []
        
        reports = []
        
        # 获取当前时间 (ET)
        et_tz = pytz.timezone("US/Eastern")
        current_time = datetime.now(et_tz)
        
        # 1. 校验禁止交易窗口
        window_reports = self._check_no_trade_windows(current_time, scan_result)
        reports.extend(window_reports)
        
        # 2. 校验每个标的
        targets = plan.get("targets", [])
        scan_targets = scan_result.get("targets", {})
        sector_rotation = scan_result.get("sector_rotation", {})
        
        # 改动7b: 如果premarket_scanner没有sector_rotation, 用deep_analysis的作fallback
        if not sector_rotation:
            try:
                import json as _json, os
                _sr_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), "data", "deep_sector_rotation.json")
                if os.path.exists(_sr_path):
                    with open(_sr_path) as _f:
                        sector_rotation = _json.load(_f)
                    logger.info(f"[PlanValidator] 使用deep_analysis sector_rotation fallback: strongest={sector_rotation.get('strongest', [])[:2]}")
            except Exception as _sr_err:
                logger.debug(f"[PlanValidator] deep_sector_rotation.json读取失败(忽略): {_sr_err}")
        
        for target in targets:
            symbol = target.get("symbol")
            if not symbol:
                continue
            
            scan_data = scan_targets.get(symbol, {})
            if not scan_data:
                logger.warning(f"扫描数据中无 {symbol}，跳过校验")
                continue
            
            indicators = scan_data.get("indicators", {})
            
            # 3. RSI Guard
            rsi = indicators.get("rsi")
            if rsi is not None:
                direction = target.get("direction", "").upper()
                if direction == "LONG" and rsi > 70:
                    reports.append({
                        "symbol": symbol,
                        "rule": "RSI Guard",
                        "severity": "FLAG",
                        "message": f"RSI={rsi:.1f} (超买)，做多风险较高",
                        "auto_fixed": False
                    })
                elif direction == "SHORT" and rsi < 30:
                    reports.append({
                        "symbol": symbol,
                        "rule": "RSI Guard",
                        "severity": "FLAG",
                        "message": f"RSI={rsi:.1f} (超卖)，做空风险较高",
                        "auto_fixed": False
                    })
            
            # 4. 入场区偏离
            entry_price = target.get("entry_price")
            current_price = indicators.get("price")
            
            if entry_price and current_price:
                deviation_pct = abs(entry_price - current_price) / current_price * 100
                
                if deviation_pct > 5:
                    # 偏离过大，拒绝
                    reports.append({
                        "symbol": symbol,
                        "rule": "入场区偏离",
                        "severity": "REJECT",
                        "message": f"入场价 ${entry_price:.2f} 偏离现价 ${current_price:.2f} 达 {deviation_pct:.1f}% (>5%)",
                        "auto_fixed": False
                    })
                    target["status"] = "REJECTED"
                    target["reject_reason"] = f"入场价偏离过大 ({deviation_pct:.1f}%)"
                elif deviation_pct > 3:
                    # 轻度偏离，自动修正
                    old_entry = entry_price
                    target["entry_price"] = current_price
                    reports.append({
                        "symbol": symbol,
                        "rule": "入场区偏离",
                        "severity": "FIXED",
                        "message": f"入场价从 ${old_entry:.2f} 修正到现价 ${current_price:.2f} (偏离 {deviation_pct:.1f}%)",
                        "auto_fixed": True
                    })
            
            # 5. 止损过窄
            stop_loss = target.get("stop_loss")
            atr = indicators.get("atr")
            
            if entry_price and stop_loss and atr:
                stop_distance = abs(entry_price - stop_loss)
                min_stop_distance = atr * 1.5
                
                if stop_distance < min_stop_distance:
                    old_stop = stop_loss
                    direction = target.get("direction", "").upper()
                    
                    if direction == "LONG":
                        target["stop_loss"] = entry_price - min_stop_distance
                    else:
                        target["stop_loss"] = entry_price + min_stop_distance
                    
                    reports.append({
                        "symbol": symbol,
                        "rule": "止损过窄",
                        "severity": "FIXED",
                        "message": f"止损从 ${old_stop:.2f} 修正到 ${target['stop_loss']:.2f} (原距离 {stop_distance:.2f} < 1.5×ATR {min_stop_distance:.2f})",
                        "auto_fixed": True
                    })
            
            # 6. 止盈不足 (R:R < 1.5)
            take_profit = target.get("take_profit")
            
            if entry_price and stop_loss and take_profit:
                direction = target.get("direction", "").upper()
                
                if direction == "LONG":
                    risk = entry_price - stop_loss
                    reward = take_profit - entry_price
                else:
                    risk = stop_loss - entry_price
                    reward = entry_price - take_profit
                
                if risk > 0:
                    rr_ratio = reward / risk
                    
                    if rr_ratio < 1.5:
                        old_tp = take_profit
                        # 自动修正到 R:R = 2.0
                        if direction == "LONG":
                            target["take_profit"] = entry_price + risk * 2.0
                        else:
                            target["take_profit"] = entry_price - risk * 2.0
                        
                        reports.append({
                            "symbol": symbol,
                            "rule": "止盈不足",
                            "severity": "FIXED",
                            "message": f"止盈从 ${old_tp:.2f} 修正到 ${target['take_profit']:.2f} (原 R:R={rr_ratio:.2f} < 1.5)",
                            "auto_fixed": True
                        })
            
            # 7. 板块逆风
            sector = target.get("sector")
            if sector:
                strongest = sector_rotation.get("strongest", [])
                weakest = sector_rotation.get("weakest", [])
                
                # 查找板块 ETF
                sector_etf = None
                for etf, name in self.scanner.SECTOR_ETFS.items():
                    if name == sector:
                        sector_etf = etf
                        break
                
                if sector_etf:
                    direction = target.get("direction", "").upper()
                    
                    if direction == "LONG" and sector_etf in weakest:
                        reports.append({
                            "symbol": symbol,
                            "rule": "板块逆风",
                            "severity": "FLAG",
                            "message": f"{sector} 是今日弱势板块，做多需谨慎",
                            "auto_fixed": False
                        })
                    elif direction == "SHORT" and sector_etf in strongest:
                        reports.append({
                            "symbol": symbol,
                            "rule": "板块逆风",
                            "severity": "FLAG",
                            "message": f"{sector} 是今日强势板块，做空需谨慎",
                            "auto_fixed": False
                        })
            
            # 8. 趋势矛盾
            trend = indicators.get("trend")
            if trend:
                direction = target.get("direction", "").upper()
                
                if direction == "LONG" and trend == "DOWN":
                    reports.append({
                        "symbol": symbol,
                        "rule": "趋势矛盾",
                        "severity": "FLAG",
                        "message": f"趋势为 DOWN (下跌)，做多需谨慎",
                        "auto_fixed": False
                    })
                elif direction == "SHORT" and trend == "UP":
                    reports.append({
                        "symbol": symbol,
                        "rule": "趋势矛盾",
                        "severity": "FLAG",
                        "message": f"趋势为 UP (上涨)，做空需谨慎",
                        "auto_fixed": False
                    })
        
        # 统计
        flag_count = sum(1 for r in reports if r["severity"] == "FLAG")
        reject_count = sum(1 for r in reports if r["severity"] == "REJECT")
        fixed_count = sum(1 for r in reports if r["severity"] == "FIXED")
        
        logger.info(
            f"[PlanValidator] 校验完成: "
            f"{flag_count} 个警告, {reject_count} 个拒绝, {fixed_count} 个自动修正"
        )
        
        # 将校验报告附加到计划中
        plan["validation_reports"] = reports
        plan["validation_summary"] = {
            "flags": flag_count,
            "rejects": reject_count,
            "fixes": fixed_count,
            "validated_at": current_time.isoformat()
        }
        
        return plan, reports
    
    def _check_no_trade_windows(
        self,
        current_time: datetime,
        scan_result: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """检查是否在禁止交易窗口内"""
        reports = []
        
        windows = scan_result.get("no_trade_windows", [])
        
        for window in windows:
            start_time = window.get("start_time")
            end_time = window.get("end_time")
            
            if not start_time or not end_time:
                continue
            
            try:
                # 解析时间
                start_dt = datetime.fromisoformat(start_time)
                end_dt = datetime.fromisoformat(end_time)
                
                # 检查当前时间是否在窗口内
                if start_dt <= current_time <= end_dt:
                    event_name = window.get("event_name", "未知事件")
                    reports.append({
                        "symbol": "ALL",
                        "rule": "禁止交易窗口",
                        "severity": "REJECT",
                        "message": f"当前处于禁止交易窗口 ({event_name}, {start_time} - {end_time})",
                        "auto_fixed": False
                    })
                    
                    logger.warning(
                        f"⚠️ 当前处于禁止交易窗口: {event_name} "
                        f"({start_time} - {end_time})"
                    )
            except Exception as e:
                logger.warning(f"解析禁止交易窗口失败: {window}, {e}")
        
        return reports
    
    def _load_scan_cache(self) -> Optional[Dict[str, Any]]:
        """加载盘前扫描缓存"""
        if not self.scan_cache_path.exists():
            logger.warning(f"扫描缓存不存在: {self.scan_cache_path}")
            return None
        
        try:
            with open(self.scan_cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"加载扫描缓存失败: {e}")
            return None
    
    def format_reports_for_display(self, reports: List[Dict[str, Any]]) -> str:
        """
        格式化校验报告供展示
        
        Returns:
            Markdown 格式的报告文本
        """
        if not reports:
            return "✅ 所有校验通过，无风险信号"
        
        lines = ["## 计划校验报告", ""]
        
        # 按严重程度分组
        rejects = [r for r in reports if r["severity"] == "REJECT"]
        flags = [r for r in reports if r["severity"] == "FLAG"]
        fixes = [r for r in reports if r["severity"] == "FIXED"]
        
        if rejects:
            lines.append("### 🚫 拒绝 (REJECT)")
            for r in rejects:
                symbol_str = f"**{r['symbol']}**" if r["symbol"] != "ALL" else "**全局**"
                lines.append(f"- {symbol_str} [{r['rule']}]: {r['message']}")
            lines.append("")
        
        if flags:
            lines.append("### ⚠️ 警告 (FLAG)")
            for r in flags:
                symbol_str = f"**{r['symbol']}**" if r["symbol"] != "ALL" else "**全局**"
                lines.append(f"- {symbol_str} [{r['rule']}]: {r['message']}")
            lines.append("")
        
        if fixes:
            lines.append("### 🔧 自动修正 (FIXED)")
            for r in fixes:
                symbol_str = f"**{r['symbol']}**" if r["symbol"] != "ALL" else "**全局**"
                lines.append(f"- {symbol_str} [{r['rule']}]: {r['message']}")
            lines.append("")
        
        # 统计
        lines.append(f"**统计**: {len(rejects)} 拒绝, {len(flags)} 警告, {len(fixes)} 修正")
        
        return "\n".join(lines)
