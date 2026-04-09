# 模拟交易深度复盘数据集成文档

## 概述

本文档描述如何将模拟交易深度复盘 API 数据集成到 FTSeek 模拟盘监控看板中。

**集成日期**: 2026-03-31  
**版本**: 1.0.0

---

## 已完成 API 端点

| 端点 | 功能 | 状态 |
|------|------|------|
| `/api/v1/review/trades` | 交易列表 | ✅ 完成 |
| `/api/v1/review/winrate` | 胜率统计 | ✅ 完成 |
| `/api/v1/review/dashboard` | 大盘总览 | ✅ 完成 |
| `/api/v1/review/pnl/summary` | 收益统计 | ✅ 完成 |
| `/api/v1/review/drawdown` | 回撤分析 | ✅ 完成 |
| `/api/v1/review/quality/{id}` | 质量评分 | ✅ 完成 |
| `/api/v1/review/suggestions` | 改进建议 | ✅ 完成 |

---

## 前端集成

### 文件结构

```
/opt/ftseek/FTSeek/app/dashboard/
├── simulation_dashboard.html    # 主看板页面 (已更新)
├── review_integration.js        # 复盘数据集成脚本 (新增)
└── review_section.html          # 复盘模块 HTML 片段 (参考)
```

### 新增模块

#### 1. 复盘 KPI 卡片

展示 4 个核心指标:
- **总收益**: 已平仓交易累计收益
- **胜率**: 盈利交易占比
- **最大回撤**: 历史最大亏损幅度
- **交易笔数**: 已平仓交易总数

#### 2. 可视化图表

- **累计收益曲线**: ECharts 折线图，展示 equity 曲线
- **胜率分布**: ECharts 饼图，盈利/亏损交易分布

#### 3. 收益统计表格

按策略/标的分组的收益统计:
- 策略/标的名称
- 交易笔数
- 胜率
- 总收益
- 平均收益

#### 4. 改进建议

基于交易模式分析的改进建议列表。

---

## 数据说明

### 数据来源

所有数据来自 SQLite 数据库 `/opt/ftseek/FTSeek/data/simulation.db`

### 统计范围

**重要**: 当前 API 仅统计 `status = 'closed'` (已平仓) 的交易记录。

- 未平仓交易 (`status = 'open'`) 不计入胜率、收益等统计
- 未平仓交易在交易列表中展示，但 PnL 可能为 null

### 当前数据状态 (2026-03-31)

- 交易记录：16 条 (全部未平仓)
- 胜率统计：0 条 (仅统计已平仓)
- 收益统计：空
- 回撤分析：空

**影响**: 由于当前无已平仓交易，复盘模块将显示"暂无已平仓交易"提示。

---

## 技术实现

### API 调用封装

```javascript
const ReviewAPI = {
  baseUrl: '/api/v1/review',
  
  async fetch(endpoint, params = {}) {
    // 统一错误处理和参数序列化
  },
  
  async getDashboard() { /* ... */ },
  async getTrades(params) { /* ... */ },
  async getPnLSummary(groupBy) { /* ... */ },
  async getWinrate(period) { /* ... */ },
  async getDrawdown(period) { /* ... */ },
  async getSuggestions() { /* ... */ }
};
```

### 数据刷新机制

- **自动刷新**: 每 10 分钟自动刷新一次
- **手动刷新**: 点击"刷新"按钮立即刷新
- **错误处理**: 失败时显示错误提示，不影响主看板功能

### 空状态处理

当 API 返回空数据时:
- KPI 卡片显示 "-"
- 图表显示"暂无数据"
- 表格显示"暂无已平仓交易数据"
- 提示"⚠️ 暂无已平仓交易，统计数据为空"

---

## 部署说明

### 1. 重启 FTSeek 服务

由于新增了 API 路由，需要重启服务以加载新模块:

```bash
cd /opt/ftseek/FTSeek
./stop.sh
./start.sh
```

### 2. 验证 API 可用性

```bash
# 测试大盘总览接口
curl http://localhost:5000/api/v1/review/dashboard

# 测试交易列表接口
curl http://localhost:5000/api/v1/review/trades?limit=5
```

### 3. 访问看板

打开浏览器访问: `http://localhost:5000/static/dashboard/`

或直接访问：`http://<server-ip>:5000/static/dashboard/`

---

## 样式定制

### 颜色变量

复盘模块使用以下 CSS 变量 (可在 `simulation_dashboard.html` 中调整):

```css
:root {
    --bg-primary: #0a0e1a;       /* 主背景 */
    --bg-secondary: #111827;     /* 次级背景 */
    --bg-card: #1a1f2e;          /* 卡片背景 */
    --border-color: #2d3748;     /* 边框颜色 */
    --text-primary: #f7fafc;     /* 主文字 */
    --text-secondary: #a0aec0;   /* 次级文字 */
    --accent-blue: #3b82f6;      /* 强调色 - 蓝 */
    --accent-green: #10b981;     /* 强调色 - 绿 (盈利) */
    --accent-red: #ef4444;       /* 强调色 - 红 (亏损) */
    --accent-yellow: #f59e0b;    /* 强调色 - 黄 (警告) */
}
```

### 图表配置

ECharts 图表配置在 `review_integration.js` 中，可调整:
- 图表尺寸
- 颜色方案
- 显示格式
- 交互行为

---

## 故障排查

### 问题 1: API 返回 404

**原因**: 服务未重启，新路由未加载

**解决**:
```bash
cd /opt/ftseek/FTSeek
./stop.sh
./start.sh
```

### 问题 2: 数据显示"加载中..."但不更新

**原因**: API 调用失败或返回空数据

**检查**:
1. 浏览器控制台查看错误信息
2. 验证 API 可用性：`curl http://localhost:5000/api/v1/review/dashboard`
3. 检查数据库是否有已平仓交易

### 问题 3: 图表不显示

**原因**: ECharts 库未加载或容器尺寸为 0

**检查**:
1. 确认 `echarts.min.js` 已加载
2. 检查图表容器是否有明确高度
3. 查看浏览器控制台错误

---

## 后续优化建议

### 短期 (P1)

- [ ] 添加时间筛选器 (7d/30d/90d/1y)
- [ ] 添加策略筛选器
- [ ] 优化空状态 UI
- [ ] 添加数据导出功能

### 中期 (P2)

- [ ] 添加交易质量评分详情弹窗
- [ ] 添加回撤曲线详细分析
- [ ] 添加策略对比功能
- [ ] 优化移动端显示

### 长期 (P3)

- [ ] WebSocket 实时推送
- [ ] 自定义指标配置
- [ ] 报告生成和分享
- [ ] 多账户对比

---

## 变更记录

| 日期 | 版本 | 变更内容 | 负责人 |
|------|------|---------|--------|
| 2026-03-31 | 1.0.0 | 初始集成 | Socratic Agents |

---

## 相关文件

- API 实现：`/opt/ftseek/FTSeek/app/simulation/review_dashboard.py`
- 前端集成：`/opt/ftseek/FTSeek/app/dashboard/review_integration.js`
- 主看板页面：`/opt/ftseek/FTSeek/app/dashboard/simulation_dashboard.html`
- 数据库：`/opt/ftseek/FTSeek/data/simulation.db`

---

*文档版本：1.0.0*  
*最后更新：2026-03-31*
