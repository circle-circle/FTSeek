"""
富途 OpenAPI 集成模块

功能:
- 同步富途实际持仓到本地
- 读取历史交易记录
- 执行自动下单 (止损止盈)
- 与 .futu_trade_audit.jsonl 集成
"""
import os
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from pathlib import Path

logger = logging.getLogger("ftseek.futu_integration")

# 导入富途 SDK
try:
    from futu import OpenQuoteContext, OpenSecTradeContext, TrdEnv, TrdMarket, RET_OK
    FUTU_AVAILABLE = True
except ImportError:
    FUTU_AVAILABLE = False
    logger.warning("富途 SDK 未安装，将使用模拟模式")


class FutuIntegration:
    """富途 OpenAPI 集成类"""
    
    def __init__(self, host: str = "127.0.0.1", port: int = 11111, 
                 trd_env: str = "SIMULATE", acc_id: Optional[int] = None):
        self.host = host
        self.port = port
        self.trd_env = TrdEnv.SIMULATE if trd_env == "SIMULATE" else TrdEnv.REAL
        self.acc_id = acc_id
        self.quote_ctx = None
        self.trd_ctx = None
        # P0-E1 (2026-05-14): 走 paths.py, 不再硬编码 /root
        try:
            from app.core.paths import get_logs_dir
            _logs = get_logs_dir()
            _logs.mkdir(parents=True, exist_ok=True)
            self.audit_file = str(_logs / "futu_trade_audit.jsonl")
        except Exception:
            from pathlib import Path as _P
            self.audit_file = str(_P(__file__).resolve().parent.parent / "logs" / "futu_trade_audit.jsonl")
        
    def connect(self) -> bool:
        """连接富途 API"""
        try:
            # 连接行情
            self.quote_ctx = OpenQuoteContext(host=self.host, port=self.port)
            logger.info(f"富途行情连接成功 ({self.host}:{self.port})")
            
            # 连接交易
            self.trd_ctx = OpenSecTradeContext(host=self.host, port=self.port)
            logger.info(f"富途交易连接成功")
            
            return True
        except Exception as e:
            logger.error(f"富途连接失败：{e}")
            return False
    
    def disconnect(self):
        """断开连接"""
        if self.quote_ctx:
            self.quote_ctx.close()
        if self.trd_ctx:
            self.trd_ctx.close()
        logger.info("富途连接已断开")
    
    def get_acc_list(self) -> List[Dict]:
        """获取账户列表"""
        if not self.trd_ctx:
            return []
        
        try:
            ret, data = self.trd_ctx.get_acc_list()
            if ret == RET_OK and len(data) > 0:
                accounts = []
                for _, row in data.iterrows():
                    accounts.append({
                        'acc_id': int(row['acc_id']) if 'acc_id' in row else 0,
                        'trd_env': row.get('trd_env', 'UNKNOWN'),
                        'currency': row.get('currency', 'N/A'),
                        'trdmarket_auth': row.get('trdmarket_auth', []),
                        'acc_role': row.get('acc_role', 'NORMAL'),
                        'security_firm': row.get('security_firm', 'FUTUSECURITIES')
                    })
                logger.info(f"获取账户列表成功：{len(accounts)} 个账户")
                return accounts
            else:
                logger.warning(f"获取账户列表返回空：{data}")
                return []
        except Exception as e:
            logger.error(f"获取账户列表异常：{e}")
            return []
    
    def get_positions(self, acc_id: Optional[int] = None) -> List[Dict]:
        """获取当前持仓"""
        if not self.trd_ctx:
            return []
        
        target_acc_id = acc_id or self.acc_id
        if not target_acc_id:
            logger.error("未指定账户 ID")
            return []
        
        try:
            ret, data = self.trd_ctx.position_list_query(
                acc_id=target_acc_id,
                trd_env=self.trd_env
            )
            
            if ret == RET_OK:
                positions = []
                for _, row in data.iterrows():
                    positions.append({
                        'code': row['code'],
                        'name': row.get('name', ''),
                        'qty': float(row['qty']),
                        'can_sell_qty': float(row.get('can_sell_qty', 0)),
                        'avg_cost': float(row.get('average_cost', 0)),
                        'current_price': float(row.get('nominal_price', 0)),
                        'market_val': float(row.get('market_val', 0)),
                        'unrealized_pl': float(row.get('unrealized_pl', 0)),
                        'pl_ratio': float(row.get('pl_ratio_avg_cost', 0)),
                        'side': str(row.get('position_side', 'LONG')).upper()
                    })
                logger.info(f"获取持仓成功：{len(positions)} 只股票")
                return positions
            else:
                logger.error(f"获取持仓失败：{data}")
                return []
        except Exception as e:
            logger.error(f"获取持仓异常：{e}")
            return []
    
    def get_funds(self, acc_id: Optional[int] = None) -> Optional[Dict]:
        """获取账户资金"""
        if not self.trd_ctx:
            return None
        
        target_acc_id = acc_id or self.acc_id
        if not target_acc_id:
            return None
        
        try:
            ret, data = self.trd_ctx.accinfo_query(
                acc_id=target_acc_id,
                trd_env=self.trd_env
            )
            
            if ret == RET_OK:
                row = data.iloc[0]
                funds = {
                    'total_assets': float(row.get('total_assets', 0)),
                    'cash': float(row.get('cash', 0)),
                    'market_val': float(row.get('market_val', 0)),
                    'available_funds': float(row.get('available_funds', 0)) if row.get('available_funds') != 'N/A' else 0,
                    'power': float(row.get('power', 0)),
                    'currency': row.get('currency', 'USD')
                }
                logger.info(f"获取资金成功：总资产=${funds['total_assets']:,.2f}")
                return funds
            else:
                logger.error(f"获取资金失败：{data}")
                return None
        except Exception as e:
            logger.error(f"获取资金异常：{e}")
            return None
    
    def get_stock_price(self, code: str) -> Optional[float]:
        """获取股票实时价格"""
        if not self.quote_ctx:
            return None
        
        try:
            ret, data = self.quote_ctx.get_market_snapshot([code])
            if ret == RET_OK and len(data) > 0:
                price = float(data.iloc[0]['last_price'])
                return price
            else:
                logger.warning(f"获取股价失败 {code}: {data}")
                return None
        except Exception as e:
            logger.error(f"获取股价异常 {code}: {e}")
            return None
    
    def load_audit_records(self, limit: int = 200) -> List[Dict]:
        """加载富途交易审计日志"""
        if not os.path.exists(self.audit_file):
            logger.warning(f"审计文件不存在：{self.audit_file}")
            return []
        
        records = []
        try:
            with open(self.audit_file, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip():
                        record = json.loads(line)
                        records.append(record)
                        if len(records) >= limit:
                            break
            
            logger.info(f"加载审计记录成功：{len(records)} 条")
            return records
        except Exception as e:
            logger.error(f"加载审计记录失败：{e}")
            return []
    
    def sync_positions_to_local(self, local_file: str, 
                                 acc_id: Optional[int] = None) -> bool:
        """
        同步富途持仓到本地 trade_records.json
        
        将富途的实际持仓转换为本地 TradeRecord 格式
        """
        positions = self.get_positions(acc_id)
        if not positions:
            logger.warning("无持仓可同步")
            return False
        
        # 读取现有记录
        existing_records = []
        if os.path.exists(local_file):
            try:
                with open(local_file, 'r', encoding='utf-8') as f:
                    existing_records = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"加载现有持仓记录失败：{e}")
                existing_records = []
        
        # 更新持仓记录
        for pos in positions:
            # 查找是否已有记录
            found = False
            for record in existing_records:
                if record.get('code') == pos['code'] and record.get('exit_price') is None:
                    # 更新现有记录的当前价格和盈亏
                    record['current_price'] = pos['current_price']
                    record['unrealized_pl'] = pos['unrealized_pl']
                    record['pl_ratio'] = pos['pl_ratio']
                    record['updated_at'] = datetime.now().isoformat()
                    found = True
                    break
            
            if not found and abs(pos['qty']) > 0:
                is_short = (pos['qty'] < 0) or (str(pos.get('side', '')).upper() == 'SHORT')
                # 创建新记录
                new_record = {
                    'trade_id': f"SYNC_{pos['code']}_{datetime.now().strftime('%Y%m%d%H%M%S')}",
                    'code': pos['code'],
                    'direction': 'SHORT' if is_short else 'LONG',
                    'action': 'SELL' if is_short else 'BUY',
                    'entry_price': pos['avg_cost'],
                    'quantity': abs(pos['qty']),
                    'entry_time': datetime.now().isoformat(),
                    'exit_price': None,
                    'exit_time': None,
                    'exit_reason': None,
                    'stop_loss': None,  # 从配置文件读取
                    'take_profit': None,  # 从配置文件读取
                    'current_price': pos['current_price'],
                    'unrealized_pl': pos['unrealized_pl'],
                    'pl_ratio': pos['pl_ratio'],
                    'updated_at': datetime.now().isoformat()
                }
                existing_records.append(new_record)
                logger.info(f"同步新持仓：{pos['code']} {pos['qty']}股 @ ${pos['current_price']}")
        
        # 保存
        os.makedirs(os.path.dirname(local_file), exist_ok=True)
        with open(local_file, 'w', encoding='utf-8') as f:
            json.dump(existing_records, f, indent=2, ensure_ascii=False)
        
        logger.info(f"同步持仓完成：{len([r for r in existing_records if r.get('exit_price') is None])} 个持仓")
        return True
    
    def _ensure_connected(self) -> bool:
        """P0-E2 (2026-05-14): 检查并尝试重建 Futu 连接"""
        try:
            # 简单 ping: trd_ctx 非空且属性可访问
            if self.trd_ctx is None:
                return self.connect()
            # 检测连接活性 (Futu 没有 ping API, 用属性访问代替)
            try:
                _ = self.trd_ctx.connect_id
                return True
            except Exception:
                logger.warning("[FutuIntegration·E2] 连接已断, 触发 reconnect")
                try:
                    self.trd_ctx.close()
                except Exception:
                    pass
                self.trd_ctx = None
                return self.connect()
        except Exception as e:
            logger.error(f"[FutuIntegration·E2] _ensure_connected 异常: {e}")
            return False

    def place_sell_order(self, code: str, qty: float, price: Optional[float] = None,
                         reason: str = "stop_loss",
                         max_retries: int = 3,
                         backoff: float = 1.0) -> Tuple[bool, str]:
        """
        执行卖出订单 (止损/止盈)

        P0-E2 (2026-05-14): 重试 + reconnect — 网络瞬断 / trd_ctx 死亡时自动恢复
        Returns:
            (success, message)
        """
        if not self.acc_id:
            return False, "未指定账户 ID"

        # 获取当前价 (如果未指定价格)
        if price is None:
            price = self.get_stock_price(code)
            if not price:
                return False, "获取股价失败"

        last_err = ""
        for attempt in range(max_retries):
            if not self._ensure_connected():
                last_err = "trd_ctx_unavailable"
                if attempt < max_retries - 1:
                    import time as _t
                    _t.sleep(backoff * (2 ** attempt))
                continue
            try:
                ret, data = self.trd_ctx.place_order(
                    price=price,
                    qty=int(qty),
                    code=code,
                    trd_side='SELL',
                    order_type='NORMAL',
                    trd_env=self.trd_env,
                    acc_id=self.acc_id
                )
                if ret == RET_OK:
                    order_id = str(data.iloc[0]['order_id']) if len(data) > 0 else 'unknown'
                    logger.info(f"卖出订单成功：{code} {qty}股 @ ${price} (订单 ID: {order_id}, attempt={attempt+1})")
                    self._record_audit('SELL', code, qty, price, reason, order_id)
                    return True, f"订单已提交：{order_id}"
                # 判断是否网络/瞬时错误 → 重试
                err_str = str(data).lower()
                last_err = str(data)
                if any(kw in err_str for kw in ("network", "timeout", "connection", "broken")):
                    logger.warning(f"[FutuIntegration·E2] 网络错误重试 ({attempt+1}/{max_retries}): {data}")
                    if attempt < max_retries - 1:
                        import time as _t
                        _t.sleep(backoff * (2 ** attempt))
                    continue
                # 业务错误 (insufficient funds / invalid symbol / etc.) 不重试
                logger.error(f"卖出订单失败 (业务拒绝, 不重试)：{code} {data}")
                return False, last_err
            except Exception as e:
                last_err = str(e)
                logger.warning(f"[FutuIntegration·E2] 异常重试 ({attempt+1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    import time as _t
                    _t.sleep(backoff * (2 ** attempt))

        return False, f"重试 {max_retries} 次后仍失败: {last_err}"
    
    def _record_audit(self, action: str, code: str, qty: float, 
                      price: float, reason: str, order_id: str):
        """记录到审计日志"""
        audit_record = {
            'action': action,
            'result': 'submitted',
            'order_id': order_id,
            'code': code,
            'side': action,
            'quantity': qty,
            'price': price,
            'order_type': 'NORMAL',
            'trd_env': 'SIMULATE' if self.trd_env == TrdEnv.SIMULATE else 'REAL',
            'reason': reason,
            'status': 'submitted',
            'timestamp': datetime.now().isoformat()
        }
        
        try:
            with open(self.audit_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(audit_record, ensure_ascii=False) + '\n')
        except Exception as e:
            logger.error(f"写入审计日志失败：{e}")


# 单例
_integration = None

def get_futu_integration(host: str = "127.0.0.1", port: int = 11111,
                         trd_env: str = "SIMULATE", 
                         acc_id: Optional[int] = None) -> FutuIntegration:
    """获取富途集成单例"""
    global _integration
    if _integration is None:
        _integration = FutuIntegration(host, port, trd_env, acc_id)
    return _integration
