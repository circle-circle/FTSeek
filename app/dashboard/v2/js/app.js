/**
 * FTSeek 深度复盘大盘 - Vue 3 应用
 */

const { createApp, ref, reactive, onMounted, computed } = Vue;

// API 基础路径
const API_BASE = '/api/v2/review';

// 主应用
createApp({
    setup() {
        // 状态
        const loading = ref(false);
        const error = ref('');
        const lastUpdated = ref('');

        // 过滤器
        const filters = reactive({
            startDate: '',
            endDate: '',
            strategy: '',
        });

        // 数据
        const kpi = ref({
            total_trades: 0,
            closed_trades: 0,
            winning_trades: 0,
            losing_trades: 0,
            win_rate: 0,
            total_pnl: 0,
            avg_pnl: 0,
            sharpe_ratio: 0,
            max_drawdown: 0,
            avg_holding_minutes: 0,
            avg_slippage_bps: 0,
        });

        const equityCurve = ref([]);
        const drawdownData = ref([]);
        const attributionData = ref({
            by_strategy: {},
            by_market_regime: {},
            by_signal_strength: {},
            strategy_regime_matrix: {},
        });

        const correlationData = ref({
            score_pnl_correlation: null,
            confidence_vs_winrate: null,
            by_strategy: {},
        });

        const recommendations = ref([]);
        const tradeDetails = ref([]);

        // ECharts 实例
        const equityChart = ref(null);
        const drawdownChart = ref(null);
        const attributionHeatmap = ref(null);
        const correlationScatter = ref(null);
        const confidenceBar = ref(null);

        // 加载数据
        const loadDashboard = async () => {
            loading.value = true;
            error.value = '';

            try {
                const params = new URLSearchParams();
                if (filters.startDate) params.append('start_date', filters.startDate);
                if (filters.endDate) params.append('end_date', filters.endDate);

                const response = await fetch(`${API_BASE}/dashboard?${params}`);
                const data = await response.json();

                kpi.value = data.kpi;
                equityCurve.value = data.equity_curve;
                drawdownData.value = data.drawdown_analysis.curve;

                lastUpdated.value = new Date().toLocaleString('zh-CN');

                // 渲染收益曲线
                renderEquityChart();
                renderDrawdownChart();

            } catch (e) {
                error.value = `加载大盘数据失败：${e.message}`;
                console.error('Dashboard load error:', e);
            } finally {
                loading.value = false;
            }
        };

        // 加载归因数据
        const loadAttribution = async () => {
            try {
                const params = new URLSearchParams();
                if (filters.startDate) params.append('start_date', filters.startDate);
                if (filters.endDate) params.append('end_date', filters.endDate);

                const response = await fetch(`${API_BASE}/attribution?${params}`);
                const data = await response.json();

                attributionData.value = {
                    by_strategy: data.by_strategy,
                    by_market_regime: data.by_market_regime,
                    by_signal_strength: data.by_signal_strength,
                    strategy_regime_matrix: data.strategy_regime_matrix,
                };

                // 渲染热力图
                renderAttributionHeatmap();

            } catch (e) {
                console.error('Attribution load error:', e);
            }
        };

        // 加载相关性数据
        const loadCorrelation = async () => {
            try {
                const params = new URLSearchParams();
                if (filters.startDate) params.append('start_date', filters.startDate);
                if (filters.endDate) params.append('end_date', filters.endDate);

                const response = await fetch(`${API_BASE}/correlation?${params}`);
                const data = await response.json();

                correlationData.value = {
                    score_pnl_correlation: data.score_pnl_correlation,
                    confidence_vs_winrate: data.confidence_vs_winrate,
                    by_strategy: data.by_strategy,
                };

                // 渲染散点图和柱状图
                renderCorrelationScatter();
                renderConfidenceBar();

            } catch (e) {
                console.error('Correlation load error:', e);
            }
        };

        // 加载优化建议
        const loadRecommendations = async () => {
            try {
                const params = new URLSearchParams();
                if (filters.startDate) params.append('start_date', filters.startDate);
                if (filters.endDate) params.append('end_date', filters.endDate);

                const response = await fetch(`${API_BASE}/recommendations?${params}`);
                const data = await response.json();

                recommendations.value = data.recommendations || [];

            } catch (e) {
                console.error('Recommendations load error:', e);
            }
        };

        // 加载交易明细
        const loadTradeDetails = async () => {
            try {
                const params = new URLSearchParams();
                if (filters.startDate) params.append('start_date', filters.startDate);
                if (filters.endDate) params.append('end_date', filters.endDate);
                if (filters.strategy) params.append('strategy', filters.strategy);

                const response = await fetch(`${API_BASE}/trades/detail?${params}`);
                const data = await response.json();

                tradeDetails.value = data.trades || [];

            } catch (e) {
                console.error('Trade details load error:', e);
            }
        };

        // 刷新数据
        const refreshData = async () => {
            await Promise.all([
                loadDashboard(),
                loadAttribution(),
                loadCorrelation(),
                loadRecommendations(),
                loadTradeDetails(),
            ]);
        };

        // 应用过滤器
        const applyFilters = async () => {
            await refreshData();
        };

        // 导出报告
        const exportReport = () => {
            const report = {
                generated_at: new Date().isoformat(),
                kpi: kpi.value,
                equity_curve: equityCurve.value,
                attribution: attributionData.value,
                correlation: correlationData.value,
                recommendations: recommendations.value,
                trades: tradeDetails.value,
            };

            const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `ftseek_report_${new Date().toISOString().slice(0, 10)}.json`;
            a.click();
            URL.revokeObjectURL(url);
        };

        // 应用建议
        const applyRecommendation = (rec) => {
            alert(`已应用建议：${rec.suggestion}`);
            // TODO: 集成到配置热更新系统
        };

        // 拒绝建议
        const rejectRecommendation = (rec) => {
            rec.status = 'rejected';
            alert(`已拒绝建议：${rec.suggestion}`);
        };

        // 优先级标签
        const priorityLabel = (priority) => {
            const labels = {
                critical: '紧急',
                high: '高',
                medium: '中',
                low: '低',
            };
            return labels[priority] || priority;
        };

        // 类型标签
        const typeLabel = (type) => {
            const labels = {
                downweight: '降权',
                upweight: '加权',
                blacklist: '黑名单',
                recalibrate: '重新校准',
                adjust_execution: '调整执行',
                suppress: '抑制',
            };
            return labels[type] || type;
        };

        // 信号分数样式
        const getScoreClass = (score) => {
            if (!score) return 'text-gray-400';
            if (score >= 0.7) return 'text-green-600 font-semibold';
            if (score >= 0.5) return 'text-blue-600';
            if (score >= 0.3) return 'text-orange-600';
            return 'text-red-600';
        };

        // 渲染收益曲线
        const renderEquityChart = () => {
            if (!equityChart.value) return;

            const chart = echarts.init(equityChart.value);
            const data = equityCurve.value.map(item => ({
                name: item.date,
                value: [item.date, item.cumulative_pnl],
            }));

            const option = {
                tooltip: {
                    trigger: 'axis',
                    formatter: (params) => {
                        const point = params[0];
                        return `${point.name}<br/>累计盈亏：${point.value[1]} HKD`;
                    },
                },
                grid: {
                    left: '3%',
                    right: '4%',
                    bottom: '3%',
                    containLabel: true,
                },
                xAxis: {
                    type: 'category',
                    boundaryGap: false,
                    data: equityCurve.value.map(item => item.date),
                },
                yAxis: {
                    type: 'value',
                    name: '累计盈亏 (HKD)',
                },
                series: [{
                    name: '累计盈亏',
                    type: 'line',
                    smooth: true,
                    areaStyle: {
                        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                            { offset: 0, color: 'rgba(16, 185, 129, 0.5)' },
                            { offset: 1, color: 'rgba(16, 185, 129, 0.1)' },
                        ]),
                    },
                    lineStyle: {
                        color: '#10b981',
                        width: 2,
                    },
                    data: data,
                }],
            };

            chart.setOption(option);
            window.addEventListener('resize', () => chart.resize());
        };

        // 渲染回撤分析
        const renderDrawdownChart = () => {
            if (!drawdownChart.value) return;

            const chart = echarts.init(drawdownChart.value);

            const option = {
                tooltip: {
                    trigger: 'axis',
                    formatter: (params) => {
                        const point = params[0];
                        return `${point.name}<br/>累计：${point.value[1]} HKD<br/>回撤：${(point.value[2] * 100).toFixed(2)}%`;
                    },
                },
                legend: {
                    data: ['累计盈亏', '回撤'],
                },
                grid: {
                    left: '3%',
                    right: '4%',
                    bottom: '3%',
                    containLabel: true,
                },
                xAxis: {
                    type: 'category',
                    boundaryGap: false,
                    data: drawdownData.value.map(item => item.date),
                },
                yAxis: [
                    {
                        type: 'value',
                        name: '累计盈亏 (HKD)',
                        position: 'left',
                    },
                    {
                        type: 'value',
                        name: '回撤',
                        position: 'right',
                        axisLabel: {
                            formatter: '{value}%',
                        },
                    },
                ],
                series: [
                    {
                        name: '累计盈亏',
                        type: 'line',
                        data: drawdownData.value.map(item => item.cumulative_pnl),
                        lineStyle: { color: '#3b82f6' },
                    },
                    {
                        name: '回撤',
                        type: 'line',
                        yAxisIndex: 1,
                        data: drawdownData.value.map(item => item.drawdown * 100),
                        lineStyle: { color: '#ef4444' },
                        areaStyle: {
                            color: 'rgba(239, 68, 68, 0.2)',
                        },
                    },
                ],
            };

            chart.setOption(option);
            window.addEventListener('resize', () => chart.resize());
        };

        // 渲染策略归因热力图
        const renderAttributionHeatmap = () => {
            if (!attributionHeatmap.value) return;

            const chart = echarts.init(attributionHeatmap.value);
            const matrix = attributionData.value.strategy_regime_matrix;

            // 构建热力图数据
            const strategies = [...new Set(Object.keys(matrix).map(([s, r]) => s))];
            const regimes = [...new Set(Object.keys(matrix).map(([s, r]) => r))];

            const data = Object.entries(matrix).map(([key, value]) => {
                const [strategy, regime] = key.split(',');
                const strategyIndex = strategies.indexOf(strategy);
                const regimeIndex = regimes.indexOf(regime);
                return [strategyIndex, regimeIndex, value.win_rate * 100, value.total_trades];
            });

            const option = {
                tooltip: {
                    formatter: (params) => {
                        const [x, y, winRate, trades] = params.value;
                        return `${strategies[x]} × ${regimes[y]}<br/>胜率：${winRate.toFixed(1)}%<br/>交易数：${trades}`;
                    },
                },
                grid: {
                    left: '15%',
                    right: '5%',
                    top: '10%',
                    bottom: '15%',
                },
                xAxis: {
                    type: 'category',
                    data: regimes,
                    axisLabel: {
                        interval: 0,
                        rotate: 30,
                    },
                },
                yAxis: {
                    type: 'category',
                    data: strategies,
                },
                visualMap: {
                    min: 0,
                    max: 100,
                    calculable: true,
                    orient: 'horizontal',
                    left: 'center',
                    bottom: '0%',
                    inRange: {
                        color: ['#f0f9ff', '#bae6fd', '#38bdf8', '#0284c7', '#0c4a6e'],
                    },
                },
                series: [{
                    name: '胜率',
                    type: 'heatmap',
                    data: data,
                    label: {
                        show: true,
                        formatter: (params) => {
                            const winRate = params.value[2];
                            const trades = params.value[3];
                            return `${winRate.toFixed(0)}%\n(${trades})`;
                        },
                        fontSize: 10,
                    },
                }],
            };

            chart.setOption(option);
            window.addEventListener('resize', () => chart.resize());
        };

        // 渲染信号相关性散点图
        const renderCorrelationScatter = () => {
            if (!correlationScatter.value) return;

            const chart = echarts.init(correlationScatter.value);
            const corr = correlationData.value.score_pnl_correlation;

            if (!corr || !corr.scatter_data || corr.scatter_data.length === 0) {
                return;
            }

            const data = corr.scatter_data.map(item => [item.score, item.pnl, item.code]);

            const option = {
                tooltip: {
                    formatter: (params) => {
                        const [score, pnl, code] = params.value;
                        return `代码：${code}<br/>信号分：${score.toFixed(2)}<br/>盈亏：${pnl.toFixed(0)} HKD`;
                    },
                },
                grid: {
                    left: '3%',
                    right: '4%',
                    bottom: '3%',
                    containLabel: true,
                },
                xAxis: {
                    type: 'value',
                    name: '信号分数',
                    min: 0,
                    max: 1,
                },
                yAxis: {
                    type: 'value',
                    name: '盈亏 (HKD)',
                },
                series: [{
                    name: '信号 vs 盈亏',
                    type: 'scatter',
                    data: data,
                    symbolSize: 8,
                    itemStyle: {
                        color: 'rgba(59, 130, 246, 0.6)',
                    },
                }],
            };

            chart.setOption(option);
            window.addEventListener('resize', () => chart.resize());
        };

        // 渲染置信度柱状图
        const renderConfidenceBar = () => {
            if (!confidenceBar.value) return;

            const chart = echarts.init(confidenceBar.value);
            const data = correlationData.value.confidence_vs_winrate;

            if (!data || !data.buckets) return;

            const categories = Object.keys(data.buckets);
            const winRates = categories.map(cat => data.buckets[cat].win_rate * 100);
            const trades = categories.map(cat => data.buckets[cat].trades);

            const option = {
                tooltip: {
                    formatter: (params) => {
                        const index = params.dataIndex;
                        return `${categories[index]}<br/>胜率：${winRates[index].toFixed(1)}%<br/>交易数：${trades[index]}`;
                    },
                },
                grid: {
                    left: '3%',
                    right: '4%',
                    bottom: '3%',
                    containLabel: true,
                },
                xAxis: {
                    type: 'category',
                    data: categories,
                    axisLabel: {
                        interval: 0,
                        rotate: 15,
                    },
                },
                yAxis: {
                    type: 'value',
                    name: '胜率 (%)',
                    max: 100,
                },
                series: [{
                    name: '胜率',
                    type: 'bar',
                    data: winRates.map((rate, i) => ({
                        value: rate,
                        itemStyle: {
                            color: rate >= 50 ? '#10b981' : rate >= 40 ? '#f59e0b' : '#ef4444',
                        },
                    })),
                    label: {
                        show: true,
                        formatter: '{c}%',
                        position: 'top',
                    },
                }],
            };

            chart.setOption(option);
            window.addEventListener('resize', () => chart.resize());
        };

        // 初始化
        onMounted(async () => {
            await refreshData();
        });

        return {
            loading,
            error,
            lastUpdated,
            filters,
            kpi,
            equityCurve,
            attributionData,
            correlationData,
            recommendations,
            tradeDetails,
            equityChart,
            drawdownChart,
            attributionHeatmap,
            correlationScatter,
            confidenceBar,
            refreshData,
            applyFilters,
            exportReport,
            applyRecommendation,
            rejectRecommendation,
            priorityLabel,
            typeLabel,
            getScoreClass,
        };
    },
}).mount('#app');
