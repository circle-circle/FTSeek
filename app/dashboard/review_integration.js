/**
 * FTSeek 复盘数据集成模块 - 仅显示今日数据
 */

// ==================== 复盘 API 调用封装 ====================
const ReviewAPI = {
  baseUrl: '/api/v1/review',
  
  async fetch(endpoint, params = {}) {
    const url = new URL(`${this.baseUrl}${endpoint}`, window.location.origin);
    Object.entries(params).forEach(([k, v]) => {
      if (v !== null && v !== undefined) {
        url.searchParams.set(k, v);
      }
    });
    
    const response = await fetch(url);
    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }
    const result = await response.json();
    if (result.status !== 'ok') {
      throw new Error(result.message || 'API 返回错误');
    }
    return result.data;
  },
  
  async getTodayTrades() {
    // 只获取今日数据
    const today = new Date().toISOString().split('T')[0];
    return this.fetch('/trades', { 
      limit: 100,
      date: today
    });
  },
  
  async getWinrate(period = 'today') {
    return this.fetch('/winrate', { period });
  },
  
  async getPnLSummary(groupBy = 'stock') {
    return this.fetch('/pnl/summary', { group_by: groupBy, date: new Date().toISOString().split('T')[0] });
  },
  
  async getDrawdown(period = 'today') {
    return this.fetch('/drawdown', { period });
  }
};

// ==================== KPI 卡片渲染 ====================
function renderKpiCards(winrate, pnlSummary) {
  const kpiContainer = document.getElementById('review-kpi-cards');
  if (!kpiContainer) return;
  
  const totalTrades = winrate.total_trades || 0;
  const winRate = winrate.win_rate || 0;
  const totalPnl = winrate.total_pnl || 0;
  const profitFactor = winrate.profit_factor || 0;
  
  kpiContainer.innerHTML = `
    <div class="review-kpi-card">
      <div class="kpi-label">今日交易数</div>
      <div class="kpi-value">${totalTrades}</div>
      <div class="kpi-sub">已平仓 ${winrate.winning_trades || 0} / 亏损 ${winrate.losing_trades || 0}</div>
    </div>
    <div class="review-kpi-card">
      <div class="kpi-label">胜率</div>
      <div class="kpi-value ${winRate >= 50 ? 'positive' : 'negative'}">${winRate.toFixed(1)}%</div>
      <div class="kpi-sub">今日统计</div>
    </div>
    <div class="review-kpi-card">
      <div class="kpi-label">今日盈亏</div>
      <div class="kpi-value ${totalPnl >= 0 ? 'positive' : 'negative'}">${totalPnl >= 0 ? '+' : ''}${totalPnl.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})} HKD</div>
      <div class="kpi-sub">待平仓后计算</div>
    </div>
    <div class="review-kpi-card">
      <div class="kpi-label">盈亏比</div>
      <div class="kpi-value ${profitFactor >= 2 ? 'positive' : 'negative'}">${profitFactor.toFixed(2)}</div>
      <div class="kpi-sub">待平仓后显示</div>
    </div>
  `;
}

// ==================== 收益曲线图表渲染 ====================
function renderEquityChart(drawdown) {
  const chartContainer = document.getElementById('equity-chart');
  if (!chartContainer) return;
  
  const equityCurve = drawdown.equity_curve || [];
  
  if (equityCurve.length === 0) {
    chartContainer.innerHTML = '<div class="no-data">暂无今日收益曲线数据，等待交易平仓</div>';
    return;
  }
  
  const ctx = document.createElement('canvas');
  ctx.id = 'equityChartCanvas';
  chartContainer.innerHTML = '';
  chartContainer.appendChild(ctx);
  
  new Chart(ctx, {
    type: 'line',
    data: {
      labels: equityCurve.map(d => d.date),
      datasets: [{
        label: '今日累计收益 (HKD)',
        data: equityCurve.map(d => d.equity),
        borderColor: '#10b981',
        backgroundColor: 'rgba(16, 185, 129, 0.1)',
        tension: 0.4,
        fill: true
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: true },
        title: {
          display: true,
          text: '今日收益曲线',
          color: '#f7fafc',
          font: { size: 14 }
        }
      },
      scales: {
        y: {
          grid: { color: '#2d3748' },
          ticks: { color: '#a0aec0' }
        },
        x: {
          grid: { color: '#2d3748' },
          ticks: { color: '#a0aec0', maxRotation: 45 }
        }
      }
    }
  });
}

// ==================== 胜率分布图表渲染 ====================
function renderWinrateChart(pnlSummary) {
  const chartContainer = document.getElementById('winrate-chart');
  if (!chartContainer) return;
  
  if (!pnlSummary || pnlSummary.length === 0) {
    chartContainer.innerHTML = '<div class="no-data">暂无今日胜率分布数据</div>';
    return;
  }
  
  const stocks = pnlSummary.map(d => d.group_key);
  const winRates = pnlSummary.map(d => d.win_rate);
  const totalTrades = pnlSummary.map(d => d.total_trades);
  
  const ctx = document.createElement('canvas');
  ctx.id = 'winrateChartCanvas';
  chartContainer.innerHTML = '';
  chartContainer.appendChild(ctx);
  
  new Chart(ctx, {
    type: 'bar',
    data: {
      labels: stocks,
      datasets: [{
        label: '今日胜率 (%)',
        data: winRates,
        backgroundColor: winRates.map(wr => wr >= 50 ? '#10b981' : '#ef4444'),
        borderColor: winRates.map(wr => wr >= 50 ? '#059669' : '#dc2626'),
        borderWidth: 1
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        title: {
          display: true,
          text: '按标的胜率分布 (今日)',
          color: '#f7fafc',
          font: { size: 14 }
        }
      },
      scales: {
        y: {
          beginAtZero: true,
          max: 100,
          grid: { color: '#2d3748' },
          ticks: { 
            color: '#a0aec0',
            callback: value => value + '%'
          }
        },
        x: {
          grid: { color: '#2d3748' },
          ticks: { color: '#a0aec0' }
        }
      },
      plugins: {
        tooltip: {
          callbacks: {
            afterLabel: function(context) {
              const index = context.dataIndex;
              return `交易数：${totalTrades[index]} 笔`;
            }
          }
        }
      }
    }
  });
}

// ==================== 收益统计表格渲染 ====================
function renderPnlTable(pnlSummary) {
  const tableContainer = document.getElementById('pnl-table');
  if (!tableContainer) return;
  
  if (!pnlSummary || pnlSummary.length === 0) {
    tableContainer.innerHTML = '<div class="no-data">暂无今日收益统计数据，等待交易平仓</div>';
    return;
  }
  
  const rows = pnlSummary.map(item => `
    <tr>
      <td>${item.group_key}</td>
      <td>${item.total_trades}</td>
      <td class="${item.win_rate >= 50 ? 'positive' : 'negative'}">${item.win_rate.toFixed(1)}%</td>
      <td class="${item.total_pnl >= 0 ? 'positive' : 'negative'}">
        ${item.total_pnl >= 0 ? '+' : ''}${item.total_pnl.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}
      </td>
      <td class="${item.avg_pnl >= 0 ? 'positive' : 'negative'}">
        ${item.avg_pnl >= 0 ? '+' : ''}${item.avg_pnl.toFixed(2)}
      </td>
    </tr>
  `).join('');
  
  tableContainer.innerHTML = `
    <table class="pnl-table">
      <thead>
        <tr>
          <th>标的</th>
          <th>交易数</th>
          <th>胜率</th>
          <th>总盈亏 (HKD)</th>
          <th>平均盈亏 (HKD)</th>
        </tr>
      </thead>
      <tbody>
        ${rows}
      </tbody>
    </table>
  `;
}

// ==================== 改进建议渲染 ====================
function renderSuggestions() {
  const suggestionsContainer = document.getElementById('suggestions');
  if (!suggestionsContainer) return;
  
  suggestionsContainer.innerHTML = `
    <div class="suggestion-card">
      <div class="suggestion-icon">💡</div>
      <div class="suggestion-content">
        <div class="suggestion-title">数据从今日开始积累</div>
        <div class="suggestion-text">从 2026-03-31 开始记录交易数据，后续将自动积累</div>
      </div>
    </div>
    <div class="suggestion-card">
      <div class="suggestion-icon">📈</div>
      <div class="suggestion-content">
        <div class="suggestion-title">关注持仓走势</div>
        <div class="suggestion-text">当前持仓未平仓，盈亏数据待平仓后计算</div>
      </div>
    </div>
    <div class="suggestion-card">
      <div class="suggestion-icon">⚠️</div>
      <div class="suggestion-content">
        <div class="suggestion-title">风险控制</div>
        <div class="suggestion-text">初始资金 1,000,000 HKD，请严格执行止盈止损</div>
      </div>
    </div>
  `;
}

// ==================== 主加载函数 ====================
async function loadReviewData() {
  try {
    console.log('正在加载今日复盘数据...');
    
    // 加载胜率统计
    const winrate = await ReviewAPI.getWinrate('today');
    console.log('今日胜率数据:', winrate);
    
    // 加载收益统计
    const pnlSummary = await ReviewAPI.getPnLSummary('stock');
    console.log('今日收益统计:', pnlSummary);
    
    // 加载回撤分析
    const drawdown = await ReviewAPI.getDrawdown('today');
    console.log('今日回撤数据:', drawdown);
    
    // 渲染 KPI 卡片
    renderKpiCards(winrate, pnlSummary);
    
    // 渲染图表
    renderEquityChart(drawdown);
    renderWinrateChart(pnlSummary);
    
    // 渲染表格
    renderPnlTable(pnlSummary);
    
    // 渲染建议
    renderSuggestions();
    
    console.log('今日复盘数据加载完成');
  } catch (error) {
    console.error('加载复盘数据失败:', error);
    // 显示空状态
    document.getElementById('review-kpi-cards').innerHTML = 
      '<div class="no-data">暂无今日复盘数据，等待交易平仓</div>';
  }
}

// ==================== 初始化 ====================
document.addEventListener('DOMContentLoaded', function() {
  console.log('复盘模块初始化 (仅今日数据)...');
  
  // 首次加载
  loadReviewData();
  
  // 定时刷新 (10 分钟)
  setInterval(loadReviewData, 10 * 60 * 1000);
  
  console.log('复盘模块初始化完成');
});
