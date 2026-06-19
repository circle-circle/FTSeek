"""手动下单服务子模块包.

将 manual_order_service.py 拆分为 4 个职责清晰的独立模块:
    - validator: 风控/参数/PDT/价格偏离校验
    - executor : Futu API 调用 (下单/平仓/撤单/止损/查询)
    - audit    : 审计/DB 写入/WebSocket 事件/手动持仓标记
    - thesis   : auto-thesis 入场上下文与出场快照

manual_order_service.ManualOrderService 作为薄门面 (Facade), 对外公共方法
签名保持不变, 内部委派到上述子模块。
"""
