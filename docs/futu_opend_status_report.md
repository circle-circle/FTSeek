# 富途 OpenD 状态检查报告

**检查时间:** 2026-03-07 14:16  
**检查人:** 阿呆 (AI 助手)

---

## 📊 检查摘要

| 项目 | 状态 | 说明 |
|------|------|------|
| **FutuOpenD 进程** | ❌ 已停止 | 僵尸进程 (defunct) |
| **行情 API 连接** | ❌ 不可用 | 端口 11111 拒绝连接 |
| **交易 API 连接** | ❌ 不可用 | 依赖 OpenD 启动 |
| **历史数据获取** | ⏸️ 待启动后测试 | 需要 OpenD 运行 |

---

## 🔍 详细分析

### 1. OpenD 进程状态

```bash
# 当前进程状态
root 134934  0.0  0.0  0  0  ?  Z  Feb27  0:00 [FutuOpenD] <defunct>
```

**问题:** OpenD 进程处于僵尸状态，需要重启

**配置文件位置:**
```
/opt/ftseek/Futu_OpenD_9.6.5618_Centos7/Futu_OpenD_9.6.5618_Centos7/FutuOpenD.xml
```

**已配置账号:**
- 登录账号：`7078740`
- 登录密码：MD5 加密 (已配置)
- 交易密码：已配置免密码解锁

### 2. 启动方法

#### 方法 A: 使用启动脚本 (推荐)

```bash
cd /opt/ftseek
./start_opend.exp <验证码>
```

**验证码获取:** 需要查看富途牛牛 APP 或短信

#### 方法 B: 直接启动

```bash
cd /opt/ftseek/Futu_OpenD_9.6.5618_Centos7/Futu_OpenD_9.6.5618_Centos7
./FutuOpenD
```

然后根据提示输入验证码

### 3. 历史数据获取能力

**富途 API 支持的历史数据:**

| 数据类型 | API 方法 | 时间范围 | 说明 |
|----------|---------|----------|------|
| **历史订单** | `get_history_order_list()` | 最近 90 天 | 包含订单状态、成交价、时间 |
| **历史成交** | `get_history_deal_list()` | 最近 90 天 | 实际成交记录 |
| **账户资金** | `get_acc_funds()` | 当前 | 实时资金情况 |
| **持仓信息** | `get_position_list()` | 当前 | 实时持仓 |
| **历史 K 线** | `get_history_kl()` | 取决于权限 | 1 分钟/5 分钟/日 K 等 |

**限制:**
- 富途 API **不支持**获取 2026-01-01 至今的完整历史 (超过 90 天)
- 建议查询范围：**最近 30-90 天**
- 更长期数据需要：
  - 通过富途牛牛客户端导出 CSV
  - 使用对账单功能

### 4. 回测分析能力

**V4 模块规划 (Task 5.2):**

```python
class SimpleBacktest:
    """简化版回测引擎"""
    
    def run(self, signal_history, price_history):
        """
        基于历史信号和价格进行回测
        
        返回:
        - 胜率 (win_rate)
        - 盈亏比 (profit_loss_ratio)
        - 夏普比率 (sharpe_ratio)
        - 最大回撤 (max_drawdown)
        - 总收益 (total_return)
        """
```

**数据需求:**
1. 历史交易记录 (从 OpenD 获取)
2. 历史 K 线数据 (从富途行情获取)
3. 买卖信号记录 (从 FTSeek 数据库获取)

---

## ✅ 建议操作

### 立即执行

1. **重启 FutuOpenD**
   ```bash
   # 1. 清理僵尸进程
   kill -9 134934
   
   # 2. 启动 OpenD (需要验证码)
   cd /opt/ftseek
   ./start_opend.exp <验证码>
   ```

2. **验证连接**
   ```bash
   python3 /opt/ftseek/FTSeek/scripts/test_futu_connection.py
   ```

### 获取历史数据

3. **获取最近 90 天订单**
   ```python
   from futu import OpenSecTradeContext, TrdMarket
   
   trd_ctx = OpenSecTradeContext(host='127.0.0.1', port=11111)
   ret, acc_data = trd_ctx.get_acc_list()
   acc_id = acc_data.iloc[0]['acc_id']
   
   # 获取 2026-01-01 至今 (如果 API 支持)
   ret, data = trd_ctx.get_history_order_list(
       acc_id=acc_id,
       start='2026-01-01',
       end='2026-03-07'
   )
   ```

4. **导出为 CSV (如需长期数据)**
   - 打开富途牛牛客户端
   - 进入「交易」→「对账单」
   - 选择时间范围 (2026-01-01 至今)
   - 导出 CSV

### 投资风格分析

5. **分析维度**
   - **交易频率**: 日均/周均交易次数
   - **持仓周期**: 平均持仓时间
   - **胜率**: 盈利交易占比
   - **盈亏比**: 平均盈利/平均亏损
   - **偏好市场**: 港股 vs 美股
   - **偏好板块**: 科技/金融/消费等
   - **风险偏好**: 止损执行情况

---

## 📈 投资风格总结模板

**待数据获取后生成:**

```
【投资风格分析报告】
周期：2026-01-01 至 2026-03-07

1. 交易特征
   - 总交易次数：XX 次
   - 日均交易：XX 次
   - 平均持仓：XX 小时/天

2. 收益表现
   - 总收益率：XX%
   - 胜率：XX%
   - 盈亏比：1:XX

3. 投资偏好
   - 主要市场：港股/美股
   - 主要板块：XXX
   - 主要策略：突破/反弹/趋势

4. 风险评估
   - 最大回撤：XX%
   - 单笔最大亏损：XX%
   - 止损执行率：XX%

5. 改进建议
   - [基于数据的具体建议]
```

---

## 🚀 下一步

1. **请提供验证码** 以重启 FutuOpenD
2. 我将获取历史交易数据
3. 生成投资风格分析报告
4. 给出针对性改进建议

---

**联系人:** 阿呆 (FTSeek V4 开发中)  
**联系方式:** 此聊天窗口
