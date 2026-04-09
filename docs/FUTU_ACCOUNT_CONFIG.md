# 富途账户配置指南

> **关键发现**：模拟账户不支持成交记录查询，必须使用订单数据代替

---

## 账户类型与API限制

### 模拟账户 (SIMULATE)

| 账户ID | 市场 | 账户类型 | API限制 |
|--------|------|---------|---------|
| `8246815` | HK | STOCK (现金) | ❌ 不支持成交查询 |
| `8246816` | US | STOCK (现金) | ❌ 不支持成交查询 |
| `8246818` | HK | OPTION (期权) | ❌ 不支持成交查询 |
| `8246819` | US | OPTION (期权) | ❌ 不支持成交查询 |

**可用的API**：
- ✅ `order_list_query` - 今日订单查询
- ✅ `position_list_query` - 持仓查询（有时会失败）
- ✅ `accinfo_query` - 账户资金查询
- ✅ `place_order` - 下单
- ✅ `modify_order` - 改单
- ✅ `cancel_order` - 撤单

**不可用的API**：
- ❌ `deal_list_query` - 今日成交查询
- ❌ `history_deal_list_query` - 历史成交查询

### 实盘账户 (REAL)

| 账户ID | 市场 | 状态 | 用途 |
|--------|------|------|------|
| `281756480166019917` | HK, US, HKCC | ACTIVE | 主账户 |
| `281756455989412692` | HK | DISABLED | 子账户 |
| `281756460284379988` | US | DISABLED | 子账户 |
| `281756468874314580` | HKCC | DISABLED | 子账户 |

**所有API均可使用**：
- ✅ `order_list_query` - 今日订单
- ✅ `deal_list_query` - 今日成交
- ✅ `history_deal_list_query` - 历史成交
- ✅ `position_list_query` - 持仓
- ✅ `accinfo_query` - 账户资金

---

## 解决方案：用订单数据代替成交数据

### 订单数据字段映射

订单记录 (`order_list_query`) 包含以下成交信息：

| 字段 | 说明 | 用途 |
|------|------|------|
| `dealt_qty` | 已成交数量 | 替代成交记录的数量 |
| `dealt_avg_price` | 成交均价 | 替代成交记录的价格 |
| `order_status` | 订单状态 | FILLED_ALL = 全部成交 |
| `create_time` | 创建时间 | 下单时间 |
| `updated_time` | 更新时间 | 成交时间 |

### 获取成交记录的正确方式

```python
from futu import *

trd_ctx = OpenSecTradeContext(
    filter_trdmarket=TrdMarket.HK,
    host='127.0.0.1', 
    port=11111,
    security_firm=SecurityFirm.FUTUSECURITIES
)

# 模拟账户ID
HK_SIM_ACC_ID = 8246815

# 获取今日订单（替代成交记录）
ret, orders = trd_ctx.order_list_query(trd_env=TrdEnv.SIMULATE, acc_id=HK_SIM_ACC_ID)

if ret == RET_OK:
    # 过滤已成交订单
    filled_orders = orders[orders['order_status'] == 'FILLED_ALL']
    
    for _, order in filled_orders.iterrows():
        print(f'{order["code"]} | {order["trd_side"]} | {order["dealt_qty"]}股 @ {order["dealt_avg_price"]} | {order["create_time"]}')

trd_ctx.close()
```

---

## FTSeek 系统配置

### 当前配置 (模拟模式)

```yaml
# config/app_config.yaml
futu:
  trade_env: 1  # 模拟盘
  simulate_account_id: "8246815"  # 港股模拟
  force_simulate: true
```

### 如需切换实盘

```yaml
futu:
  trade_env: 0  # 实盘
  real_account_id: "281756480166019917"  # 主账户
  force_simulate: false
```

**⚠️ 注意**：实盘交易需要先在 OpenD GUI 界面解锁交易密码。

---

## 代码修改建议

### 1. 修改成交记录获取逻辑

```python
# 原代码（不适用于模拟账户）
ret, deals = trd_ctx.deal_list_query(trd_env=TrdEnv.SIMULATE, acc_id=acc_id)

# 修改后（适用于所有账户）
if trd_env == TrdEnv.SIMULATE:
    # 模拟账户：使用订单数据代替成交数据
    ret, orders = trd_ctx.order_list_query(trd_env=trd_env, acc_id=acc_id)
    if ret == RET_OK:
        filled_orders = orders[orders['order_status'] == 'FILLED_ALL']
        # 转换为成交记录格式
        deals = filled_orders[['code', 'trd_side', 'dealt_qty', 'dealt_avg_price', 'create_time']]
        deals = deals.rename(columns={
            'dealt_qty': 'qty',
            'deelt_avg_price': 'price',
            'create_time': 'time'
        })
else:
    # 实盘账户：直接查询成交数据
    ret, deals = trd_ctx.deal_list_query(trd_env=trd_env, acc_id=acc_id)
```

### 2. 修改持仓查询逻辑

```python
# 添加重试机制处理持仓查询失败
def get_positions(trd_ctx, acc_id, max_retries=3):
    for i in range(max_retries):
        ret, pos = trd_ctx.position_list_query(trd_env=TrdEnv.SIMULATE, acc_id=acc_id)
        if ret == RET_OK:
            return pos
        if '此数据暂时还未准备好' in str(pos):
            time.sleep(2)  # 等待数据准备
            continue
        break
    return None  # 查询失败
```

---

## 快速查询命令

### 查看账户列表

```bash
python3 -c "
from futu import *
trd_ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.NONE, host='127.0.0.1', port=11111)
ret, data = trd_ctx.get_acc_list()
print(data.to_string())
trd_ctx.close()
"
```

### 查看模拟账户今日订单

```bash
python3 -c "
from futu import *
trd_ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.HK, host='127.0.0.1', port=11111)
ret, orders = trd_ctx.order_list_query(trd_env=TrdEnv.SIMULATE, acc_id=8246815)
if ret == RET_OK:
    print(orders[['code', 'trd_side', 'qty', 'dealt_qty', 'dealt_avg_price', 'order_status']].to_string())
trd_ctx.close()
"
```

### 查看实盘账户持仓

```bash
python3 -c "
from futu import *
trd_ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.NONE, host='127.0.0.1', port=11111)
ret, pos = trd_ctx.position_list_query(trd_env=TrdEnv.REAL, acc_id=281756480166019917)
if ret == RET_OK:
    print(pos[['code', 'qty', 'nominal_price', 'market_val']].to_string())
trd_ctx.close()
"
```

---

*更新时间: 2026-03-30*