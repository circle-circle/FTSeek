// ── 全局账户净值缓存 (仓位计算使用) ──
var _bpEquity = 0;
async function loadEquity() {
    try {
        var r = await fetch('/api/dashboard/snapshot');
        if (!r.ok) return;
        var d = await r.json();
        var acc = d.account || {};
        _bpEquity = Number(acc.total_assets || acc.equity || acc.nav || 0) || 0;
    } catch(e) { /* 静默失败 */ }
}

// 计算 mini 仓位建议 (1% 风险，5% 仓位上限)
function calcMiniPos(entry, sl, tp, direction, equity) {
    if (!(equity > 0 && entry > 0 && sl > 0)) return null;
    var priceRisk = Math.abs(entry - sl);
    if (priceRisk <= 0) return null;
    var riskPct = 0.01;
    var maxPct  = 0.05;
    var sharesByRisk = Math.floor(equity * riskPct / priceRisk);
    var sharesByPct  = Math.floor(equity * maxPct  / entry);
    var shares = Math.max(0, Math.min(sharesByRisk, sharesByPct));
    if (shares <= 0) return null;
    var cost = shares * entry;
    var rr = 0;
    if (tp > 0) {
        var reward = direction === 'SHORT' ? (entry - tp) : (tp - entry);
        rr = Math.max(0, reward) / priceRisk;
    }
    return { shares: shares, cost: cost, rr: rr, riskPct: riskPct };
}

function esc(s){var d=document.createElement('div');d.textContent=s||'';return d.innerHTML;}
function fmtK(n){return n==null?'--':Math.abs(n)>=1e6?(n/1e6).toFixed(1)+'M':Math.abs(n)>=1e3?(n/1e3).toFixed(1)+'K':Number(n).toFixed(0);}

// ── Deep Analysis Panel (Task #29) ──
function daId(symbol) { return 'da-panel-' + String(symbol || '').replace(/\./g, '-'); }
function daBtnId(symbol) { return 'da-btn-' + String(symbol || '').replace(/\./g, '-'); }

// Task #30: 缓存当前 plan 的 targets, 供 executeFromAnalysis 查找入场区间/仓位
var currentPlanTargets = [];

// 2026-06-20: 单个target生成深度研究 (v2: 阶段进度反馈 + 缓存感知)
var _drStages = [
    {label: '📡 数据采集', dur: 5000},
    {label: '🧠 多空辩论', dur: 30000},
    {label: '⚖ 综合裁决', dur: 30000},
    {label: '🛡 风险辩论', dur: 30000},
    {label: '✅ 完成', dur: 1000},
];
var _drTimer = null;

async function generateSingleResearch(symbol, btn) {
    if (btn) { btn.disabled = true; btn.dataset._orig = btn.textContent; }

    // 阶段进度模拟 (真实时间约90-120s)
    var stageIdx = 0;
    function updateStage() {
        if (btn) btn.textContent = _drStages[stageIdx].label + '…';
        stageIdx++;
        if (stageIdx < _drStages.length) {
            _drTimer = setTimeout(updateStage, _drStages[stageIdx - 1].dur);
        }
    }
    updateStage();

    try {
        var resp = await fetch('/api/battle-plan/deep-research/' + encodeURIComponent(symbol), { method: 'POST' });
        var data = await resp.json();
        if (_drTimer) { clearTimeout(_drTimer); _drTimer = null; }

        if (resp.ok && data.success) {
            if (data.cached) {
                // 缓存命中 — 无需刷新，直接恢复按钮
                if (btn) { btn.textContent = '✅ 已有分析'; btn.disabled = false; }
                setTimeout(function(){ if(btn) btn.textContent = btn.dataset._orig || '🧪 生成深度研究'; }, 2000);
            } else {
                if (btn) btn.textContent = '✅ 完成，刷新中…';
                setTimeout(function(){ location.reload(); }, 500);
            }
        } else {
            var msg = '❌ 失败: ' + (data && (data.error || data.message) || 'HTTP ' + resp.status);
            if (btn) { btn.textContent = msg; btn.disabled = false; }
            setTimeout(function(){ if(btn) btn.textContent = btn.dataset._orig || '🧪 生成深度研究'; }, 4000);
        }
    } catch(e) {
        if (_drTimer) { clearTimeout(_drTimer); _drTimer = null; }
        if (btn) { btn.textContent = '❌ 网络错误: ' + e.message; btn.disabled = false; }
        setTimeout(function(){ if(btn) btn.textContent = btn.dataset._orig || '🧪 生成深度研究'; }, 4000);
    }
}

// Task #30: 按需生成深度分析 (v3: 调用 Deep Research endpoint, v2: 阶段进度)
var _drBatchTimer = null;
var _drBatchStages = [
    {label: '📡 批量数据采集 (3标的)', dur: 8000},
    {label: '🧠 第1标的: 多空辩论', dur: 30000},
    {label: '⚖ 第1标的: 综合裁决', dur: 30000},
    {label: '🛡 第1标的: 风险辩论', dur: 30000},
    {label: '🧠 第2标的: 多空辩论', dur: 30000},
    {label: '⚖ 第2标的: 综合裁决', dur: 30000},
    {label: '🧠 第3标的: 多空辩论', dur: 30000},
    {label: '⚖ 第3标的: 综合裁决', dur: 30000},
    {label: '✅ 批量完成', dur: 1000},
];
async function generateDeepAnalysis(ev) {
    if (ev) ev.stopPropagation();
    var btn = ev && ev.target;
    if (!btn) return;
    var allBtns = document.querySelectorAll('.da-generate-btn');
    allBtns.forEach(function(b){ b.disabled = true; b.dataset._orig = b.textContent; });

    // 阶段进度
    var sIdx = 0;
    function tickStage() {
        allBtns.forEach(function(b){ b.textContent = _drBatchStages[sIdx].label + '…'; });
        sIdx++;
        if (sIdx < _drBatchStages.length) {
            _drBatchTimer = setTimeout(tickStage, _drBatchStages[sIdx - 1].dur);
        }
    }
    tickStage();

    try {
        var resp = await fetch('/api/battle-plan/deep-research/generate', { method: 'POST' });
        if (!resp.ok) {
            resp = await fetch('/api/battle-plan/deep-analysis/generate', { method: 'POST' });
        }
        var data = await resp.json();
        if (_drBatchTimer) { clearTimeout(_drBatchTimer); _drBatchTimer = null; }
        if (resp.ok && data.success) {
            allBtns.forEach(function(b){ b.textContent = '✅ 完成，刷新中…'; });
            setTimeout(function(){ location.reload(); }, 500);
            return;
        }
        var msg = '❌ 失败: ' + (data && (data.error || data.message) || ('HTTP ' + resp.status));
        allBtns.forEach(function(b){ b.textContent = msg; });
        setTimeout(function(){
            allBtns.forEach(function(b){
                b.textContent = b.dataset._orig || '生成深度研究';
                b.disabled = false;
            });
        }, 4000);
    } catch (e) {
        if (_drBatchTimer) { clearTimeout(_drBatchTimer); _drBatchTimer = null; }
        allBtns.forEach(function(b){ b.textContent = '❌ 网络错误'; });
        setTimeout(function(){
            allBtns.forEach(function(b){
                b.textContent = b.dataset._orig || '生成深度研究';
                b.disabled = false;
            });
        }, 4000);
    }
}

// Task #30: 由深度分析面板直接下单
function executeFromAnalysis(symbol, direction, ev) {
    if (ev) ev.stopPropagation();
    var target = (currentPlanTargets || []).find(function(t){ return t && t.symbol === symbol; });
    if (!target) { alert('未找到目标信息：' + symbol); return; }

    // 首选 entry_zone[0] (入场下限), 其次 confirmed_entry_price, 最后 当前价
    var ez = target.entry_zone;
    var price = null;
    if (Array.isArray(ez) && ez.length >= 1 && ez[0] > 0) price = Number(ez[0]);
    else if (target.confirmed_entry_price) price = Number(target.confirmed_entry_price);
    else if (target._current_price) price = Number(target._current_price);

    var posPct = Number(target.position_size_pct || 4);
    var dirText = direction === 'LONG' ? '做多' : (direction === 'SHORT' ? '做空' : direction);
    var priceStr = price ? ('$' + price.toFixed(2)) : '市价';
    if (!confirm('确认执行 ' + dirText + ' ' + symbol + ' @ ' + priceStr + '\n仓位: ' + posPct + '%')) return;

    // 计算股数: 依赖账户净值 + 仓位百分比 / 价格
    var qty = 0;
    if (_bpEquity > 0 && price > 0) {
        qty = Math.max(1, Math.floor(_bpEquity * (posPct / 100) / price));
    }

    var orderBody = {
        symbol: symbol,                                  // 保持 US.XXX 格式, 后端下单服务接受
        direction: direction,                            // LONG / SHORT
        action: direction === 'SHORT' ? 'SELL' : 'BUY',
        order_type: price ? 'LIMIT' : 'MARKET',
        price: price,
        quantity: qty,
        position_size_pct: posPct,
        reason: '深度分析面板直达下单 (Task #30)',
        idempotency_key: 'da_' + symbol + '_' + Date.now(),
    };

    fetch('/api/orders', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(orderBody),
    }).then(function(r){ return r.json().then(function(d){ return { ok: r.ok, d: d }; }); })
      .then(function(res){
          var d = res.d || {};
          if (res.ok && d.success !== false && !d.error) {
              var oid = d.order_id || (d.order && d.order.order_id) || 'N/A';
              alert('下单成功！订单ID: ' + oid);
          } else {
              alert('下单失败: ' + (d.error || d.message || ('HTTP ' + (res.ok ? 200 : 'err'))));
          }
      })
      .catch(function(e){ alert('下单请求失败: ' + e.message); });
}

function toggleDeepAnalysis(symbol, ev) {
    if (ev) ev.stopPropagation();
    var panel = document.getElementById(daId(symbol));
    var btn = document.getElementById(daBtnId(symbol));
    if (!panel) return;
    if (panel.style.display === 'none' || !panel.style.display) {
        panel.style.display = 'block';
        if (btn) btn.innerHTML = '深度分析 ▲';
    } else {
        panel.style.display = 'none';
        if (btn) btn.innerHTML = '深度分析 ▼';
    }
}

function _daRenderPoint(point) {
    if (!point) return '';
    var strength = String(point.strength || 'LOW').toUpperCase();
    if (['HIGH','MEDIUM','LOW'].indexOf(strength) < 0) strength = 'LOW';
    var sourceType = String(point.source_type || '').toLowerCase();
    var sourceLabels = {technical:'技术面',fundamental:'基本面',news:'新闻',sentiment:'情绪',macro:'宏观'};
    var html = '<div class="da-point">';
    html += '<span class="da-strength da-strength-' + strength + '">' + strength + '</span>';
    if (sourceType && sourceLabels[sourceType]) {
        html += '<span class="da-source da-source-' + sourceType + '">' + sourceLabels[sourceType] + '</span>';
    }
    if (point.thesis) html += '<span class="da-thesis">' + esc(point.thesis) + '</span>';
    if (point.evidence) html += '<div class="da-evidence">' + esc(point.evidence) + '</div>';
    html += '</div>';
    return html;
}

function _daRenderCausal(chain) {
    if (!chain) return '';
    // 将 → / -> / － > 等各种符号统一高亮
    var s = String(chain).replace(/\s*(→|->|⇒|－>|—>|>)\s*/g, ' <span class="da-arrow">→</span> ');
    return s;
}

function renderDeepAnalysis(target) {
    var da = target && target.deep_analysis;
    if (!da || typeof da !== 'object') return '';
    var symbol = target.symbol || '';
    var html = '<div id="' + daId(symbol) + '" class="da-panel" style="display:none;" onclick="event.stopPropagation()">';

    // ══ v2: 数据质量状态条 (对标 MU_research-deep.md 数据采集状态表) ══
    var dq = da.data_quality;
    if (dq && typeof dq === 'object') {
        var statusIcons = {'完整':'✅','降级':'⚠️','缺失':'❌'};
        html += '<div class="da-data-quality">';
        html += '<span class="da-dq-title">数据质量</span>';
        var dims = [['技术面',dq.technical],['基本面',dq.fundamental],['新闻',dq.news],['宏观',dq.macro]];
        dims.forEach(function(d){
            var icon = statusIcons[d[1]] || '❓';
            var cls = d[1] === '完整' ? 'dq-ok' : (d[1] === '降级' ? 'dq-warn' : 'dq-miss');
            html += '<span class="da-dq-item ' + cls + '">' + icon + ' ' + esc(d[0]) + '</span>';
        });
        if (dq.overall_completeness) {
            html += '<span class="da-dq-summary">' + esc(dq.overall_completeness) + '</span>';
        }
        html += '</div>';
    }

    // ══ v2: 执行摘要 (决策区优先) ══
    if (da.executive_summary) {
        html += '<div class="da-exec-summary">' + esc(da.executive_summary) + '</div>';
    }

    // ══ Task #30: 结论方向 + 执行下单 (面板顶部) ══
    var verdictObj = (da.verdict && typeof da.verdict === 'object') ? da.verdict : {};
    var dc = String(verdictObj.direction_confidence || 'MEDIUM').toUpperCase();
    if (['HIGH','MEDIUM','LOW'].indexOf(dc) < 0) dc = 'MEDIUM';
    var dir = String(target.direction || '').toUpperCase();
    var dirText = dir === 'LONG' ? '做多' : (dir === 'SHORT' ? '做空' : '观望');
    var dirCls = dir === 'LONG' ? 'da-dir-long' : (dir === 'SHORT' ? 'da-dir-short' : 'da-dir-flat');
    var btnCls = 'da-execute-btn' + (dir === 'SHORT' ? ' short' : '');
    var symAttr = String(symbol).replace(/'/g, "\\'");
    html += '<div class="da-conclusion">';
    html += '<div class="da-conclusion-header">';
    html += '<span class="da-direction ' + dirCls + '">' + dirText + '</span>';
    html += '<span class="da-confidence da-strength-' + dc + '">确信度 ' + dc + '</span>';
    html += '</div>';
    if (verdictObj.key_disagreement) {
        html += '<div class="da-conclusion-reason">核心分歧：' + esc(verdictObj.key_disagreement) + '</div>';
    }
    if (verdictObj.adopted_from_bear) {
        html += '<div class="da-conclusion-adopted">风险采纳：' + esc(verdictObj.adopted_from_bear) + '</div>';
    }
    if (dir === 'LONG' || dir === 'SHORT') {
        html += '<button class="' + btnCls + '" onclick="executeFromAnalysis(\'' + symAttr + '\', \'' + dir + '\', event)">';
        html += '执行' + dirText + ' →</button>';
    }
    html += '</div>';

    // 多头论证
    var bull = Array.isArray(da.bull_case) ? da.bull_case : [];
    if (bull.length) {
        html += '<div class="da-section da-bull"><h4>🟢 多头论证 <span style="color:#6e7681;font-weight:400;letter-spacing:0;">(' + bull.length + ')</span></h4>';
        bull.forEach(function(p) { html += _daRenderPoint(p); });
        html += '</div>';
    }

    // 空头反驳
    var bear = Array.isArray(da.bear_case) ? da.bear_case : [];
    if (bear.length) {
        html += '<div class="da-section da-bear"><h4>🔴 空头反驳 <span style="color:#6e7681;font-weight:400;letter-spacing:0;">(' + bear.length + ')</span></h4>';
        bear.forEach(function(p) { html += _daRenderPoint(p); });
        html += '</div>';
    }

    // 裁决
    var verdict = da.verdict;
    if (verdict && typeof verdict === 'object') {
        var dc = String(verdict.direction_confidence || 'LOW').toUpperCase();
        if (['HIGH','MEDIUM','LOW'].indexOf(dc) < 0) dc = 'LOW';
        html += '<div class="da-section da-verdict"><h4>⚖ 裁决</h4>';
        html += '<div class="da-verdict-row"><span class="lbl">方向确信度</span><span class="val"><span class="da-strength da-strength-' + dc + '">' + dc + '</span></span></div>';
        if (verdict.key_disagreement) {
            html += '<div class="da-verdict-row"><span class="lbl">核心分歧</span><span class="val">' + esc(verdict.key_disagreement) + '</span></div>';
        }
        if (verdict.adopted_from_bear) {
            html += '<div class="da-verdict-row"><span class="lbl">采纳空头</span><span class="val">' + esc(verdict.adopted_from_bear) + '</span></div>';
        }
        if (verdict.data_adjusted) {
            html += '<div class="da-verdict-row"><span class="lbl">数据调整</span><span class="val" style="color:#fbbf24">⚠ ' + esc(verdict.data_adjusted) + '</span></div>';
        }
        html += '</div>';
    }

    // 因果传导 + 历史参照
    if (da.causal_chain || da.historical_reference) {
        html += '<div class="da-section da-chain"><h4>🔗 因果传导</h4>';
        if (da.causal_chain) html += '<div class="da-causal">' + _daRenderCausal(da.causal_chain) + '</div>';
        if (da.historical_reference) html += '<div class="da-history">历史参照: ' + esc(da.historical_reference) + '</div>';
        html += '</div>';
    }

    // 基本面快照
    var fs = da.fundamental_snapshot;
    if (fs && typeof fs === 'object') {
        var rows = [
            ['营收增长', fs.revenue_growth],
            ['前瞻PE', fs.pe_forward],
            ['分析师共识', fs.analyst_consensus],
            ['下一催化', fs.next_catalyst],
        ].filter(function(r) { return r[1] != null && r[1] !== ''; });
        if (rows.length) {
            html += '<div class="da-section da-fundamental"><h4>📊 基本面</h4>';
            html += '<table class="da-table"><tbody>';
            rows.forEach(function(r) {
                html += '<tr><td>' + esc(r[0]) + '</td><td>' + esc(String(r[1])) + '</td></tr>';
            });
            html += '</tbody></table>';
            html += '</div>';
        }
    }

    // 风控方案
    var rt = da.risk_tiers;
    if (rt && typeof rt === 'object') {
        var tiers = [
            ['aggressive', '激进', rt.aggressive],
            ['moderate', '中性', rt.moderate],
            ['conservative', '保守', rt.conservative],
        ].filter(function(t) { return t[2] && typeof t[2] === 'object'; });
        if (tiers.length) {
            html += '<div class="da-section da-risk"><h4>🛡 风控方案</h4>';
            tiers.forEach(function(t) {
                var cls = t[0]; var label = t[1]; var data = t[2];
                html += '<div class="da-risk-tier da-' + cls + '">';
                html += '<span class="da-tier-label">' + label + '</span>';
                if (data.action) html += esc(data.action);
                if (data.trigger) html += ' <span class="da-risk-trigger">| 触发: ' + esc(data.trigger) + '</span>';
                if (data.risk) html += ' <span class="da-risk-note">| 风险: ' + esc(data.risk) + '</span>';
                html += '</div>';
            });
            html += '</div>';
        }
    }

    // 执行计划
    var ep = da.execution_plan;
    if (ep && typeof ep === 'object') {
        var steps = Array.isArray(ep.steps) ? ep.steps : [];
        if (steps.length || ep.event_buffer) {
            html += '<div class="da-section da-execution"><h4>📋 执行计划</h4>';
            if (steps.length) {
                html += '<ol>';
                steps.forEach(function(s) { html += '<li>' + esc(String(s)) + '</li>'; });
                html += '</ol>';
            }
            if (ep.event_buffer) {
                html += '<div class="da-event-buffer">' + esc(ep.event_buffer) + '</div>';
            }
            if (ep.invalidation) {
                html += '<div class="da-invalidation">❌ 失效条件: ' + esc(ep.invalidation) + '</div>';
            }
            html += '</div>';
        }
    }

    html += '</div>';
    return html;
}

// ── Evidence parser: 将后端 evidence 字符串解析为可视化标签 ──
function parseEv(s) {
    var t = String(s || '');
    if (t.indexOf('辩论置信度') >= 0) {
        var weak = t.indexOf('(中等)') >= 0;
        return { label: t.replace(/^辩论置信度[:：]?/, '辩论 '), type: weak ? 'warn' : 'good', icon: weak ? '⚠' : '✅', source: 'debate' };
    }
    if (t.indexOf('R:R') >= 0) {
        var soso = t.indexOf('(一般)') >= 0;
        var m = t.match(/R:R[=：:]?\s*([0-9.]+)/);
        var rr = m ? parseFloat(m[1]) : 0;
        var type = rr >= 2.0 ? 'good' : rr >= 1.5 ? 'warn' : 'bad';
        if (soso && type === 'good') type = 'warn';
        var icon = type === 'good' ? '✅' : type === 'warn' ? '⚠' : '❌';
        return { label: t.replace(/^R:R[=：:]?\s*/, 'R:R '), type: type, icon: icon, source: 'rr' };
    }
    if (t.indexOf('历史验证') >= 0) {
        if (t.indexOf('GREEN') >= 0) return { label: t.replace(/^历史验证[:：]?/, '历史 '), type: 'good', icon: '✅', source: 'history' };
        if (t.indexOf('RED') >= 0)   return { label: t.replace(/^历史验证[:：]?/, '历史 '), type: 'bad',  icon: '❌', source: 'history' };
        if (t.indexOf('YELLOW') >= 0)return { label: t.replace(/^历史验证[:：]?/, '历史 '), type: 'warn', icon: '⚠', source: 'history' };
        return { label: t, type: 'gray', icon: '·', source: 'history' };
    }
    if (t.indexOf('技术确认') >= 0) {
        return { label: t.replace(/^技术确认[:：]?/, '技术 '), type: 'good', icon: '✅', source: 'tech' };
    }
    if (t.indexOf('⛔') >= 0 || t.indexOf('风险预算') >= 0) {
        return { label: t.replace(/^⛔\s*/, ''), type: 'bad', icon: '⛔', source: 'risk' };
    }
    return { label: t, type: 'gray', icon: '·', source: 'other' };
}

// ── 计算共识度: 4 个核心来源中有多少 good ──
function computeConsensus(parsed) {
    var sources = { debate: 0, rr: 0, history: 0, tech: 0 };
    parsed.forEach(function(p) {
        if (p.source in sources && p.type === 'good') sources[p.source] = 1;
    });
    var good = sources.debate + sources.rr + sources.history + sources.tech;
    var total = 4;
    var cls = good >= 3 ? 'strong' : good >= 2 ? 'medium' : 'weak';
    var label = good >= 3 ? '强' : good >= 2 ? '中' : '弱';
    return { good: good, total: total, cls: cls, label: label, pct: (good / total) * 100 };
}

async function loadDecisionHub() {
    try {
        var resp = await fetch('/api/decision-hub');
        if (!resp.ok) return;
        var data = await resp.json();
        var cmdBar = document.getElementById('cmdBar');
        if (!cmdBar) return;
        cmdBar.style.display = 'flex';

        // Update Command Bar with enhanced data from decision-hub API
        var ds = data.day_score;
        if (ds) {
            var scoreEl = document.getElementById('cmdScore');
            var labelEl = document.getElementById('cmdLabel');
            var budgetEl = document.getElementById('cmdBudget');
            if (scoreEl) { scoreEl.textContent = ds.score || '--'; scoreEl.className = 'cmd-score ' + (ds.level || 'NORMAL'); }
            var levelMap = { STRONG:'积极交易', NORMAL:'正常操作', CAUTIOUS:'谨慎观望', AVOID:'不交易' };
            if (labelEl) { labelEl.textContent = levelMap[ds.level] || ds.level || '—'; }
            if (budgetEl) {
                var budgetVal = '$' + Math.round(ds.daily_risk_budget || 0).toLocaleString();
                budgetEl.textContent = '风险预算 ' + budgetVal;
            }
        }
    } catch(e) {
        console.debug('[CmdBar] load error:', e);
    }
}

async function loadPlan() {
    try {
        var ctrl = new AbortController();
        var tid = setTimeout(function(){ ctrl.abort(); }, 15000);
        // Fetch all endpoints in parallel: actions API + dashboard snapshot (for equity) + decision-hub + yesterday review
        var results = await Promise.all([
            fetch('/api/battle-plan', {signal: ctrl.signal}),
            fetch('/api/battle-plan/actions', {signal: ctrl.signal}).catch(function(){ return null; }),
            loadEquity(),
            loadDecisionHub().catch(function(){ /* 决策仓失败不影响主流程 */ })
        ]);
        clearTimeout(tid);
        var resPlan = results[0];
        var resActions = results[1];
        if (!resPlan.ok) throw new Error('HTTP ' + resPlan.status);
        var d = await resPlan.json();
        var actData = null;
        try { if (resActions && resActions.ok) actData = await resActions.json(); } catch(e) { actData = null; }
        if (d.error) { document.getElementById('plan').innerHTML = '<div class="loading">' + esc(d.error) + '</div>'; return; }
        render(d, actData);
    } catch(e) {
        var msg = e.name === 'AbortError' ? '请求超时(15s)，请刷新重试' : '加载失败: ' + esc(e.message);
        document.getElementById('plan').innerHTML = '<div class="loading">' + msg + '</div>';
    }
}

function render(d, actData) {
    var el = document.getElementById('plan');
    var targets = d.targets || [];
    currentPlanTargets = targets.slice();
    var restricted = d.restricted || [];
    var regime = d.market_regime || 'UNKNOWN';
    var confidence = d.confidence || 0;
    var reasoning = d.regime_reasoning || '';
    var warnings = d.risk_warnings || [];
    var adjustments = d.strategy_adjustments || [];

    // ── Build lookup maps from actions API ──
    var actActions = (actData && actData.actions) || {};
    var mustDo = actActions.must_do || [];
    var actOppMap = {};
    (actActions.opportunities || []).forEach(function(a) { if (a && a.symbol) actOppMap[a.symbol] = a; });
    var actMustMap = {};
    mustDo.forEach(function(a) { if (a && a.symbol) actMustMap[a.symbol] = a; });

    // ── Process targets into opportunities ──
    var restrictedSet = {};
    restricted.forEach(function(r) { restrictedSet[r] = true; restrictedSet[r.replace('US.','')] = true; });

    var opps = [];
    targets.forEach(function(t) {
        var sym = t.symbol || '';
        var bare = sym.replace('US.','');
        if (restrictedSet[sym] || restrictedSet[bare]) return;

        var conf = t.confidence || confidence || 0;
        var entry = t.entry_zone || [];
        var sl = t.stop_loss || 0;
        var tp = t.take_profit || 0;
        var rr = 0;
        if (entry.length === 2 && sl > 0 && tp > 0) {
            var mid = (entry[0] + entry[1]) / 2;
            var reward = t.direction === 'SHORT' ? mid - tp : tp - mid;
            var risk = t.direction === 'SHORT' ? sl - mid : mid - sl;
            rr = risk > 0 ? reward / risk : 0;
        }

        var cur = t._current_price || t.current_price || 0;
        var triggerStatus = '';
        if (cur > 0 && entry.length === 2) {
            if (cur >= entry[0] && cur <= entry[1]) triggerStatus = '✅ 在入场区间';
            else if (t.direction === 'LONG' && cur > entry[1]) triggerStatus = '⚠ 高于区间';
            else if (t.direction === 'SHORT' && cur < entry[0]) triggerStatus = '⚠ 低于区间';
            else triggerStatus = '⏳ 等待进入区间';
        }

        // Deep research or legacy deep analysis
        var dr = t.deep_research || null;
        var da = t.deep_analysis || null;

        opps.push({
            symbol: sym, direction: t.direction || 'LONG',
            confidence: conf, entry: entry, sl: sl, tp: tp, rr: rr,
            cur: cur, trigger: triggerStatus,
            reason: t.rationale || '', urgency: (t.urgency || '').toUpperCase(),
            data_cited: t.data_cited || [], source: t.discovery_source || '',
            trigger_condition: t.trigger_condition || '',
            invalidation: t.invalidation || '',
            position_size: t.position_size_pct || 0,
            verdict_txt: (t.verdict || '').toUpperCase(),
            factor_score: t.factor_score || 0,
            composite_rank: t.composite_rank || 0,
            rank_position: t.rank_position || 0,
            is_primary: t.is_primary || false,
            discovery_time: t.discovery_time || '',
            model_used: t.model_used || '',
            execution_rules: t.execution_rules || [],
            why_primary: t.why_primary || '',
            executive_summary: t.executive_summary || '',
            catalyst: t.catalyst || {},
            deep_research: dr,
            deep_analysis: da,
            factor_validation: t.factor_validation || {},
        });
    });

    // Sort: primary first, then by composite_rank DESC
    opps.sort(function(a, b) {
        if (a.is_primary && !b.is_primary) return -1;
        if (!a.is_primary && b.is_primary) return 1;
        return b.composite_rank - a.composite_rank;
    });

    // Avoid list
    var avoid = [];
    restricted.forEach(function(sym) {
        var reason = (d.restricted_reasons || {})[sym] || '禁区标的';
        avoid.push({ symbol: sym, reason: reason });
    });

    // ── Render ──
    var html = '';

    // ── Layer -1: Pause Bar (暂停状态条) ──
    var pauseBar = document.getElementById('pauseBar');
    var pauseText = document.getElementById('pauseText');
    if (d._paused && pauseBar) {
        pauseBar.style.display = 'flex';
        if (pauseText) {
            var reason = d._pause_reason || d._paused_reason || '自动交易已暂停';
            pauseText.textContent = '⏸ 自动交易已暂停 — ' + esc(reason);
        }
    } else if (pauseBar) {
        pauseBar.style.display = 'none';
    }

    // ── Layer 0: Command Bar (替代 Decision Hub + header) ──
    var regimeLabel = {RISK_ON:'偏多',RISK_OFF:'偏空',NEUTRAL:'震荡',ROTATION:'轮动',HIGH_VOL:'高波动',DEGRADED:'数据不足'};
    var cmdBar = document.getElementById('cmdBar');
    if (cmdBar) {
        cmdBar.style.display = 'flex';
        var scoreEl = document.getElementById('cmdScore');
        var labelEl = document.getElementById('cmdLabel');
        var budgetEl = document.getElementById('cmdBudget');
        var posEl = document.getElementById('cmdPositions');
        var dayScore = Math.round((confidence || 0) * 100);
        if (scoreEl) { scoreEl.textContent = dayScore || '--'; scoreEl.className = 'cmd-score ' + regime; }
        if (labelEl) { labelEl.textContent = (regimeLabel[regime] || regime) + ' · ' + opps.length + '个目标'; }
        if (budgetEl) { budgetEl.textContent = '风险预算 $' + (_bpEquity > 0 ? fmtK(_bpEquity * 0.01) : '--'); }
        var pa = d.position_actions || [];
        if (posEl) { posEl.textContent = pa.length + '仓位'; }
    }

    // ── must_do (critical actions) ──

    // ── Portfolio Synthesis (组合级分析) ──
    var ps = d.portfolio_synthesis;
    if (ps && ps.action_list) {
        var agSrc = ps._meta || {};
        var agTxt = agSrc.agreement === 'HIGH' ? '✅ 两模型一致' : agSrc.agreement === 'MEDIUM' ? '⚠ 部分一致' : agSrc.agreement === 'LOW' ? '⚠ 分歧较大' : '单模型';
        var riskCls = (ps.risk_level || '').toUpperCase() === 'HIGH' ? 'high' : (ps.risk_level || '').toUpperCase() === 'MEDIUM' ? 'mid' : 'low';
        html += '<div class="ps-panel">';
        html += '<div class="ps-header">';
        html += '<span class="ps-title">🎯 组合分析</span>';
        html += '<span class="ps-direction">' + esc(ps.recommended_direction || '—') + '</span>';
        html += '<span class="ps-risk ' + riskCls + '">风险 ' + esc(ps.risk_level || '—') + '</span>';
        html += '<span class="ps-agreement">' + agTxt + '</span>';
        html += '</div>';
        html += '<div class="ps-assessment">' + esc(ps.market_assessment || '') + '</div>';
        // Action list table
        html += '<div class="ps-table-wrap"><table class="ps-table"><thead><tr>';
        html += '<th>#</th><th>标的</th><th>方向</th><th>操作</th><th>确信</th><th>仓位</th><th>入场</th><th>止损</th><th>核心理由</th>';
        html += '</tr></thead><tbody>';
        ps.action_list.forEach(function(a) {
            var sym = (a.symbol || '').replace('US.', '');
            var action = (a.action || '').toUpperCase();
            var dir = (a.direction || '').toUpperCase();
            var conv = a.conviction || 0;
            var pos = a.position_pct || '—';
            var entry = a.entry || '—';
            var sl = a.stop_loss || '—';
            var reason = a.core_reason || '';
            var actionCls = {BUY:'buy', SELL:'sell', CONDITIONAL:'cond', WATCH:'watch'}[action] || 'watch';
            var dirCls = dir === 'LONG' ? 'long' : (dir === 'SHORT' ? 'short' : '');
            var convCls = conv >= 70 ? 'high' : conv >= 50 ? 'mid' : 'low';
            html += '<tr>';
            html += '<td class="ps-rank">' + esc(a.rank || '') + '</td>';
            html += '<td class="ps-sym">' + esc(sym) + '</td>';
            html += '<td class="ps-dir ' + dirCls + '">' + dir + '</td>';
            html += '<td class="ps-action ' + actionCls + '">' + action + '</td>';
            html += '<td class="ps-conv ' + convCls + '">' + conv + '%</td>';
            html += '<td class="ps-pos">' + esc(pos) + '</td>';
            html += '<td class="ps-entry">' + esc(entry) + '</td>';
            html += '<td class="ps-sl">' + esc(sl) + '</td>';
            html += '<td class="ps-reason">' + esc(reason) + '</td>';
            html += '</tr>';
        });
        html += '</tbody></table></div>';
        // Upgrade conditions for CONDITIONAL/WATCH
        var upgradeItems = ps.action_list.filter(function(a) { return a.upgrade_condition && (a.action === 'CONDITIONAL' || a.action === 'WATCH'); });
        if (upgradeItems.length) {
            html += '<div class="ps-upgrade">';
            html += '<div class="ps-upgrade-hd">📋 升级条件</div>';
            upgradeItems.forEach(function(a) {
                html += '<div class="ps-upgrade-item"><span class="ps-upgrade-sym">' + esc((a.symbol||'').replace('US.','')) + '</span> ' + esc(a.upgrade_condition) + '</div>';
            });
            html += '</div>';
        }
        if (ps.portfolio_summary) {
            html += '<div class="ps-summary">' + esc(ps.portfolio_summary) + '</div>';
        }
        html += '</div>';
    }

    // must_do (critical actions)
    if (mustDo.length) {
        html += '<div class="ap-must">';
        html += '<div class="ap-must-hd">🚨 必须执行 <span class="cnt">(' + mustDo.length + ')</span></div>';
        mustDo.forEach(function(m) {
            var sym = (m.symbol || '').replace('US.','');
            var typ = (m.type || '').toUpperCase();
            var icon = typ === 'EXIT' ? '🚪' : typ === 'REDUCE' ? '✂️' : typ === 'CLOSE' ? '🛑' : '⚠';
            var url = m.action_url || ('/trade?symbol=' + encodeURIComponent(m.symbol || '') + '&op=CLOSE');
            html += '<div class="ap-must-item">';
            html += '<div class="ap-must-row1">';
            html += '<span class="ap-must-icon">' + icon + '</span>';
            html += '<span class="ap-must-type ' + esc(typ) + '">' + esc(typ || 'ACTION') + '</span>';
            html += '<span class="ap-sym" style="font-size:13px">' + esc(sym) + '</span>';
            if (m.direction) html += '<span class="ap-dir ' + esc(m.direction) + '">' + esc(m.direction) + '</span>';
            html += '<a href="' + esc(url) + '" class="ap-must-btn">立即处理 →</a>';
            html += '</div>';
            if (m.reason || m.detail) {
                html += '<div class="ap-must-reason-line">';
                if (m.reason) html += esc(m.reason);
                if (m.detail) html += ' <span class="ap-must-detail">· ' + esc(m.detail) + '</span>';
                html += '</div>';
            }
            html += '</div>';
        });
        html += '</div>';
    }

    // Non-trading day banner
    if (d._is_trading_day === false) {
        var reason = d._weekend ? '今日周末，美股休市' : d._stale ? '此计划为 ' + esc(d.date || '历史') + ' 生成，非今日数据' : '非交易日';
        html += '<div class="ap-pass">📅 ' + reason + '。新计划将在下个交易日盘前 (ET 08:30) 自动生成</div>';
    }

    // ── Layer 2: Position Monitor (提前到卡片前, 保护利润优先) ──
    var pa = d.position_actions || [];
    if (pa.length) {
        html += '<div class="pos-monitor">';
        html += '<div class="pos-monitor-hd">持仓监控 <span class="cnt">(' + pa.length + ')</span></div>';
        var actOrder = {EXIT: 0, REDUCE: 1, HOLD: 2, ADD: 3};
        pa.sort(function(a,b){ return (actOrder[a.action]||9) - (actOrder[b.action]||9); });
        pa.forEach(function(p) {
            var pSym = (p.symbol || '').replace('US.','');
            var pDir = (p.direction || 'LONG').toUpperCase();
            var pnl = p.current_pnl_pct || 0;
            var pnlDisplay = Math.abs(pnl) > 200 ? null : pnl;
            var pnlStr = pnlDisplay === null ? 'N/A' : (pnlDisplay >= 0 ? '+' + pnlDisplay.toFixed(1) + '%' : pnlDisplay.toFixed(1) + '%');
            var pnlCls = pnlDisplay === null ? '' : (pnlDisplay >= 0 ? 'pos' : 'neg');
            var fs = p.factor_score || 0;
            var actCls = p.action === 'EXIT' ? 'danger' : p.action === 'REDUCE' ? 'warn' : '';
            html += '<div class="pos-row ' + actCls + '">';
            html += '<span class="pos-sym">' + esc(pSym) + '</span>';
            html += '<span class="pos-dir ' + pDir + '">' + pDir + '</span>';
            html += '<span class="pos-pnl ' + pnlCls + '">' + pnlStr + '</span>';
            if (fs > 0) html += '<span class="pos-fs">因子' + (fs*100).toFixed(0) + '</span>';
            html += '<span class="pos-action">' + esc(p.action) + '</span>';
            html += '</div>';
        });
        html += '</div>';
    }

    // ── 市况一屏 (Market Overview) ──
    var regimeLabels = {RISK_ON:'偏多',RISK_OFF:'偏空',NEUTRAL:'震荡',ROTATION:'轮动',HIGH_VOL:'高波动',DEGRADED:'数据不足'};
    html += '<div class="mkt-overview">';
    html += '<div class="mkt-overview-hd"><span class="mo-icon">📊</span> 市况一屏</div>';
    html += '<div class="mkt-regime-block">';
    html += '<span class="mo-regime-badge ' + esc(regime) + '">' + esc(regimeLabels[regime] || regime) + '</span>';
    html += '<span class="mo-regime-reasoning">' + esc(reasoning || '无市场研判') + '</span>';
    html += '</div>';
    if (opps.length > 0) {
        html += '<div class="mo-table-wrap"><table class="mo-table"><thead><tr>';
        html += '<th>#</th><th>Ticker</th><th>方向</th><th>置信度</th><th>催化</th><th>动量</th><th>情绪</th><th>裁决</th>';
        html += '</tr></thead><tbody>';
        opps.forEach(function(o, oi) {
            var catScore = (o.catalyst && o.catalyst.score != null) ? o.catalyst.score : '—';
            var momScore = (o.factor_score != null) ? (o.factor_score * 100).toFixed(0) : '—';
            var sentDir = '—';
            var sentCls = 'neutral';
            if (o.deep_research && o.deep_research.analyst_reports && o.deep_research.analyst_reports.sentiment && o.deep_research.analyst_reports.sentiment.direction) {
                sentDir = o.deep_research.analyst_reports.sentiment.direction;
                var sd = String(sentDir).toLowerCase();
                if (sd === 'bullish' || sd === 'positive') { sentCls = 'positive'; sentDir = '看多'; }
                else if (sd === 'bearish' || sd === 'negative') { sentCls = 'negative'; sentDir = '看空'; }
                else { sentCls = 'neutral'; sentDir = '中性'; }
            }
            var catCls = (typeof catScore === 'number' && catScore >= 7) ? 'high' : (typeof catScore === 'number' && catScore >= 4) ? 'mid' : 'low';
            var momCls = (typeof o.factor_score === 'number' && o.factor_score >= 0.7) ? 'high' : (typeof o.factor_score === 'number' && o.factor_score >= 0.4) ? 'mid' : 'low';
            var verdict = o.verdict_txt || (o.direction === 'LONG' ? 'BUY' : 'SELL');
            html += '<tr>';
            html += '<td class="mo-rank">' + (oi + 1) + '</td>';
            html += '<td class="mo-sym">' + esc(o.symbol.replace('US.','')) + '</td>';
            html += '<td class="mo-dir ' + o.direction + '">' + (o.direction === 'LONG' ? '做多' : '做空') + '</td>';
            html += '<td class="mo-conf">' + o.confidence + '%</td>';
            html += '<td class="mo-score ' + catCls + '">' + catScore + '</td>';
            html += '<td class="mo-score ' + momCls + '">' + momScore + '</td>';
            html += '<td class="mo-sentiment ' + sentCls + '">' + sentDir + '</td>';
            html += '<td class="mo-verdict ' + verdict + '">' + verdict + '</td>';
            html += '</tr>';
        });
        html += '</tbody></table></div>';
    }
    html += '</div>';

    // ── Layer 2.5: PreMarket Scan Summary (盘前扫描摘要) ──
    if (d._scan_summary) {
        html += '<div class="premarket-summary">';
        html += '<div class="premarket-hd">🔍 盘前扫描摘要</div>';
        html += '<div class="premarket-body">' + esc(d._scan_summary) + '</div>';
        html += '</div>';
    }

    // ── Layer 2.6: Sector Rotation (板块轮动) ──
    if (d._sectors && d._sectors.length > 0) {
        html += '<div class="sector-rotation">';
        html += '<div class="sector-hd">📊 板块轮动</div>';
        html += '<div class="sector-grid">';
        d._sectors.forEach(function(s) {
            var changeCls = s.change_pct > 0 ? 'positive' : (s.change_pct < 0 ? 'negative' : 'neutral');
            html += '<div class="sector-item">';
            html += '<span class="sector-name">' + esc(s.name) + '</span>';
            html += '<span class="sector-change ' + changeCls + '">' + (s.change_pct > 0 ? '+' : '') + s.change_pct.toFixed(2) + '%</span>';
            html += '</div>';
        });
        html += '</div></div>';
    }

    // ── Layer 2.7: Economic Events (经济事件时间线) ──
    if (d._economic_events && d._economic_events.length > 0) {
        html += '<div class="economic-events">';
        html += '<div class="events-hd">📅 今日经济事件</div>';
        html += '<div class="events-timeline">';
        d._economic_events.forEach(function(evt) {
            var impactCls = evt.impact === 'HIGH' ? 'high' : (evt.impact === 'MEDIUM' ? 'medium' : 'low');
            html += '<div class="event-item ' + impactCls + '">';
            html += '<span class="event-time">' + esc(evt.time || '--:--') + '</span>';
            html += '<span class="event-name">' + esc(evt.name) + '</span>';
            html += '<span class="event-impact">' + esc(evt.impact) + '</span>';
            html += '</div>';
        });
        html += '</div></div>';
    }

    // ── Layer 2.8: No Trade Windows (禁止交易窗口警告) ──
    if (d._no_trade_windows && d._no_trade_windows.length > 0) {
        html += '<div class="no-trade-warning">';
        html += '<div class="ntw-hd">⛔ 禁止交易窗口</div>';
        html += '<div class="ntw-body">';
        d._no_trade_windows.forEach(function(w) {
            html += '<div class="ntw-item">';
            html += '<span class="ntw-time">' + esc(w.start_time) + ' - ' + esc(w.end_time) + '</span>';
            html += '<span class="ntw-reason">' + esc(w.reason) + '</span>';
            html += '</div>';
        });
        html += '</div></div>';
    }

    // ── Layer 2.9: Validation Reports (校验报告) ──
    if (d._validation_reports && d._validation_reports.length > 0) {
        html += '<div class="validation-reports">';
        html += '<div class="vr-hd">✅ 计划校验报告</div>';
        html += '<div class="vr-body">';
        
        var rejects = d._validation_reports.filter(function(r) { return r.severity === 'REJECT'; });
        var fixes = d._validation_reports.filter(function(r) { return r.severity === 'FIX'; });
        var flags = d._validation_reports.filter(function(r) { return r.severity === 'FLAG'; });
        
        if (rejects.length > 0) {
            html += '<div class="vr-section reject">';
            html += '<div class="vr-label">🚫 拒绝 (' + rejects.length + ')</div>';
            rejects.forEach(function(r) {
                html += '<div class="vr-item">' + esc(r.symbol) + ': ' + esc(r.message) + '</div>';
            });
            html += '</div>';
        }
        
        if (fixes.length > 0) {
            html += '<div class="vr-section fix">';
            html += '<div class="vr-label">🔧 自动修正 (' + fixes.length + ')</div>';
            fixes.forEach(function(r) {
                html += '<div class="vr-item">' + esc(r.symbol) + ': ' + esc(r.message) + '</div>';
            });
            html += '</div>';
        }
        
        if (flags.length > 0) {
            html += '<div class="vr-section flag">';
            html += '<div class="vr-label">⚠️ 警告 (' + flags.length + ')</div>';
            flags.forEach(function(r) {
                html += '<div class="vr-item">' + esc(r.symbol) + ': ' + esc(r.message) + '</div>';
            });
            html += '</div>';
        }
        
        html += '</div></div>';
    }

    // ── Layer 3 & 4: Investment Targets ──
    if (opps.length === 0) {
        html += '<div class="ap-pass">📋 今日无高确信度目标，建议观望为主</div>';
    }

    opps.forEach(function(o, idx) {
        var sym = o.symbol.replace('US.','');
        var uid = 'dr_' + idx;
        var dr = o.deep_research;
        var da = o.deep_analysis;

        // Determine action from deep_research or fallback
        var action = 'BUY';
        var execSummary = o.executive_summary || '';
        var conviction = o.confidence;
        var buildPlan = null;
        var indSummary = null;
        var riskTriggers = [];
        var keyLevels = null;
        var riskTiers = null;
        var bullCase = [];
        var bearCase = [];
        var debateVerdict = {};
        var dataQuality = {};

        if (dr) {
            var v = dr.verdict || {};
            action = v.action || (o.direction === 'LONG' ? 'BUY' : 'SELL');
            execSummary = dr.executive_summary || execSummary;
            conviction = v.conviction || conviction;
            buildPlan = dr.build_plan || null;
            indSummary = dr.indicators_summary || null;
            riskTriggers = dr.risk_triggers || [];
            keyLevels = dr.key_levels || null;
            riskTiers = dr.risk_tiers || null;
            bullCase = dr.bull_case || [];
            bearCase = dr.bear_case || [];
            debateVerdict = dr.debate_verdict || {};
            dataQuality = dr.data_quality || {};
        } else if (da) {
            execSummary = da.executive_summary || execSummary;
            var dc = (da.verdict || {}).direction_confidence || 'MEDIUM';
            conviction = dc === 'HIGH' ? Math.max(conviction, 75) : dc === 'LOW' ? Math.min(conviction, 40) : conviction;
            bullCase = da.bull_case || [];
            bearCase = da.bear_case || [];
            debateVerdict = da.verdict || {};
            riskTiers = da.risk_tiers || null;
            dataQuality = da.data_quality || {};
            action = o.direction === 'LONG' ? 'BUY' : 'SELL';
        } else {
            action = o.direction === 'LONG' ? 'BUY' : 'SELL';
        }

        var convClass = conviction >= 70 ? 'HIGH' : conviction >= 45 ? 'MEDIUM' : 'LOW';
        var isAvoid = action === 'AVOID' || action === 'HOLD';

        // ── Card Start ──
        html += '<div class="dr-verdict-card action-' + action + (isAvoid ? ' dr-card-muted' : '') + '">';

        // Header: action badge + symbol(linked) + price + direction + rank
        html += '<div class="dr-header">';
        html += '<span class="dr-action-badge ' + action + '">' + action + '</span>';
        html += '<a href="/stock/' + encodeURIComponent(o.symbol) + '" class="dr-symbol" title="查看详情" onclick="event.stopPropagation()">' + esc(sym) + '</a>';
        if (o.cur > 0) html += '<span class="dr-price">$' + Number(o.cur).toFixed(2) + '</span>';
        if (o.direction) html += '<span class="ap-dir ' + o.direction + '">' + o.direction + '</span>';
        if (o.is_primary) html += '<span class="dr-rank-badge">★ 主攻 #' + (o.rank_position || 1) + '</span>';

        // Data quality dots inline in header
        var dqDims = [];
        if (dataQuality) {
            var dimLabels = {technical:'技术',fundamental:'基本面',news:'新闻',sentiment:'情绪',macro:'宏观'};
            for (var dk in dimLabels) {
                if (dataQuality[dk]) {
                    var st = dataQuality[dk];
                    var cls = st === '完整' || st === 'complete' ? 'complete' : (st === '降级' || st === 'degraded' ? 'degraded' : 'missing');
                    dqDims.push({label: dimLabels[dk], cls: cls});
                }
            }
        }
        if (dqDims.length) {
            html += '<span class="dr-header-dq">';
            dqDims.forEach(function(dd) {
                html += '<span class="dr-dq-dot ' + dd.cls + '" title="' + esc(dd.label) + ': ' + dd.cls + '"></span>';
            });
            html += '</span>';
        }
        html += '</div>';

        // Executive summary (1 line, truncated)
        if (execSummary) {
            html += '<div class="dr-exec-summary"><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:block">' + esc(execSummary) + '</span></div>';
        }

        // AVOID/HOLD: show reason prominently
        if (isAvoid) {
            html += '<div class="dr-avoid-reason">⚠ ' + esc(execSummary || '建议回避/观望') + '</div>';
        }

        // Param strip (1 line)
        if (o.entry.length === 2) {
            html += '<div class="dr-param-strip">';
            html += '<span class="ps-item"><span class="ps-lbl">入场</span><span class="ps-val">$' + o.entry[0].toFixed(2) + '–' + o.entry[1].toFixed(2) + '</span></span>';
            if (o.sl) html += '<span class="ps-item"><span class="ps-lbl">止损</span><span class="ps-val sl">$' + o.sl.toFixed(2) + '</span></span>';
            if (o.tp) html += '<span class="ps-item"><span class="ps-lbl">止盈</span><span class="ps-val tp">$' + o.tp.toFixed(2) + '</span></span>';
            if (o.rr > 0) html += '<span class="ps-item"><span class="ps-lbl">R:R</span><span class="ps-val">' + o.rr.toFixed(1) + ':1</span></span>';
            html += '</div>';
        }

        // Execute bar (always visible for BUY/SELL, not for AVOID/HOLD)
        if (action === 'BUY' || action === 'SELL') {
            var miniEntry = (o.entry.length === 2) ? (o.entry[0] + o.entry[1]) / 2 : o.cur;
            var miniPos = calcMiniPos(miniEntry, o.sl, o.tp, o.direction, _bpEquity);
            html += '<div class="dr-execute-bar" onclick="event.stopPropagation()">';
            var qParams = ['symbol=' + encodeURIComponent(o.symbol)];
            qParams.push('direction=' + o.direction);
            if (miniEntry > 0) { qParams.push('entry=' + miniEntry.toFixed(2)); qParams.push('price=' + miniEntry.toFixed(2)); }
            if (o.sl > 0) qParams.push('sl=' + o.sl.toFixed(2));
            if (o.tp > 0) qParams.push('tp=' + o.tp.toFixed(2));
            qParams.push('order_type=LIMIT');
            if (miniPos) qParams.push('qty=' + miniPos.shares);
            var tradeUrl = '/trade?' + qParams.join('&');
            html += '<a href="' + esc(tradeUrl) + '" class="dr-execute-btn ' + action + '">' + (action === 'BUY' ? '确认买入 →' : '确认卖出 →') + '</a>';
            if (miniPos) {
                html += '<span class="dr-execute-meta">' + miniPos.shares + '股 · $' + miniPos.cost.toLocaleString(undefined,{maximumFractionDigits:0}) + ' · 1%R</span>';
            }
            html += '</div>';
        }

        // Expand button (only if there's deep_research to show)
        if (dr || da) {
            html += '<div class="dr-expand-btn" onclick="var r=document.getElementById(\'' + uid + '\');if(r){r.classList.toggle(\'open\');this.classList.toggle(\'open\');}">';
            html += '<span>📖 完整分析</span>';
            html += '<span class="arrow">▼</span>';
            html += '</div>';
        }

        // ── Collapsible research report ──
        // CONDITIONAL (no dr/da): default open, no toggle button needed
        var reportOpen = (!dr && !da) ? ' open' : '';
        html += '<div class="dr-report' + reportOpen + '" id="' + uid + '">';

        // If no deep_research, show factor summary instead of blank
        if (!dr && !da) {
            html += '<div class="dr-no-research">';
            // Factor breakdown with descriptions
            html += '<div class="dr-nr-section">';
            html += '<div class="dr-nr-hd">🔍 因子诊断 <span class="dr-nr-hint">hover查看说明</span></div>';
            html += '<div class="dr-nr-grid">';
            html += '<div class="dr-nr-item" title="多因子加权综合评分(0-100)。≥80=强信号, 60-79=中等, <60=弱"><span class="nr-label">综合因子分</span><span class="nr-val ' + (o.factor_score >= 0.8 ? 'high' : o.factor_score >= 0.6 ? 'mid' : 'low') + '">' + (o.factor_score * 100).toFixed(0) + '</span></div>';
            html += '<div class="dr-nr-item" title="在当日候选池中的排名百分位。越高=越靠前"><span class="nr-label">排名分</span><span class="nr-val">' + (o.composite_rank * 100).toFixed(0) + '</span></div>';
            html += '<div class="dr-nr-item" title="系统对该信号的置信度。≥70=高确信, 45-69=中等, <45=低"><span class="nr-label">置信度</span><span class="nr-val">' + o.confidence + '%</span></div>';
            html += '<div class="dr-nr-item" title="信号发现来源: core_pool=盘前核心池, INTRADAY_ALERT=盘中异动扫描"><span class="nr-label">发现来源</span><span class="nr-val">' + esc(o.source || '—') + '</span></div>';
            html += '<div class="dr-nr-item" title="因子计算模型: deterministic_rules=规则引擎, quick_factor=快速评分"><span class="nr-label">模型</span><span class="nr-val">' + esc(o.model_used || '—') + '</span></div>';
            html += '<div class="dr-nr-item" title="信号紧急度: HIGH=需立即关注, MEDIUM=观察, LOW=不急"><span class="nr-label">紧急度</span><span class="nr-val ' + (o.urgency === 'HIGH' ? 'high' : '') + '">' + esc(o.urgency || '—') + '</span></div>';
            html += '</div>';

            // Factor validation status (if available)
            var fv = o.factor_validation;
            var fvKeys = Object.keys(fv);
            if (fvKeys.length) {
                var fvLabels = {
                    sector_momentum: '板块动量',
                    relative_strength: '相对强度',
                    ma_alignment: '均线排列',
                    volume_breakout: '量能突破',
                    rsi_sweet_spot: 'RSI位置',
                    insider_signal: '内部人信号'
                };
                var fvDescs = {
                    sector_momentum: '所属板块当日涨跌',
                    relative_strength: '相对SPY的超额收益',
                    ma_alignment: 'MA5/10/20多空排列',
                    volume_breakout: '成交量 vs 均量',
                    rsi_sweet_spot: 'RSI在40-60健康区间',
                    insider_signal: '内部人买入/卖出'
                };
                html += '<div class="dr-fv-grid">';
                fvKeys.forEach(function(k) {
                    var val = fv[k] || '—';
                    var cls = val === 'CONFIRMED' ? 'ok' : (val === 'WEAK' ? 'warn' : 'neutral');
                    var icon = val === 'CONFIRMED' ? '✅' : (val === 'WEAK' ? '⚠️' : '·');
                    html += '<span class="dr-fv-item ' + cls + '" title="' + esc(fvDescs[k] || k) + '">' + icon + ' ' + esc(fvLabels[k] || k) + ' <b>' + esc(val) + '</b></span>';
                });
                html += '</div>';
            }

            // Data cited
            if (o.data_cited && o.data_cited.length) {
                html += '<div class="dr-nr-cited">';
                html += '<span class="nr-cited-label">数据依据 (实时):</span>';
                o.data_cited.forEach(function(d) {
                    html += '<span class="nr-cited-item">' + esc(d) + '</span>';
                });
                html += '</div>';
            } else {
                html += '<div class="dr-nr-cited">';
                html += '<span class="nr-cited-label" style="color:#f87171">⚠ 缺少数据依据 — 此信号仅有异动检测，未经过多因子验证</span>';
                html += '</div>';
            }
            // Rationale
            if (o.reason) {
                html += '<div class="dr-nr-rationale">' + esc(o.reason) + '</div>';
            }
            // Generate button
            html += '<button class="dr-generate-btn" onclick="generateSingleResearch(\'' + esc(o.symbol) + '\', this)">🧪 生成深度研究</button>';
            html += '</div>';
            html += '</div>'; // close dr-report
            html += '</div>'; // close dr-verdict-card
            return;
        }

        // Build plan
        if (buildPlan) {
            html += '<div class="dr-build-plan">';
            html += '<div class="dr-build-plan-hd">分批建仓方案</div>';
            html += '<div class="dr-bp-grid">';
            var batches = [
                {key: 'batch_1', label: '底仓'},
                {key: 'batch_2', label: '加仓'},
                {key: 'batch_3', label: '末仓'},
            ];
            batches.forEach(function(b) {
                var bp = buildPlan[b.key];
                if (bp) {
                    html += '<div class="dr-bp-cell">';
                    html += '<span class="bp-label">' + esc(b.label) + ' ' + esc(bp.pct || '') + '</span>';
                    html += '<span class="bp-zone">' + esc(bp.zone || '') + '</span>';
                    if (bp.condition) html += '<span class="bp-condition">' + esc(bp.condition) + '</span>';
                    html += '</div>';
                }
            });
            html += '</div>';
            if (buildPlan.event_buffer) {
                html += '<div class="dr-bp-event">' + esc(buildPlan.event_buffer) + '</div>';
            }
            html += '</div>';
        } else if (o.entry.length === 2) {
            html += '<div class="dr-build-plan">';
            html += '<div class="dr-build-plan-hd">建仓参数</div>';
            html += '<div class="dr-params-grid">';
            html += '<div class="dr-param-cell"><span class="pc-label">入场区间</span><span class="pc-val">$' + o.entry[0].toFixed(2) + ' – $' + o.entry[1].toFixed(2) + '</span></div>';
            if (o.sl) html += '<div class="dr-param-cell"><span class="pc-label">止损</span><span class="pc-val sl">$' + o.sl.toFixed(2) + '</span></div>';
            if (o.tp) html += '<div class="dr-param-cell"><span class="pc-label">止盈</span><span class="pc-val tp">$' + o.tp.toFixed(2) + '</span></div>';
            if (o.rr > 0) html += '<div class="dr-param-cell"><span class="pc-label">R:R</span><span class="pc-val">' + o.rr.toFixed(1) + ':1</span></div>';
            if (o.position_size) html += '<div class="dr-param-cell"><span class="pc-label">仓位</span><span class="pc-val">' + o.position_size + '%</span></div>';
            html += '</div></div>';
        }

        // Indicators
        if (indSummary) {
            html += '<div class="dr-indicators">';
            var indItems = [
                {label: '趋势', val: indSummary.trend},
                {label: '动量', val: indSummary.momentum},
                {label: '波动', val: indSummary.volatility},
                {label: '量价', val: indSummary.volume},
            ];
            indItems.forEach(function(item) {
                if (item.val && item.val !== 'N/A') {
                    html += '<div><span class="ind-label">' + esc(item.label) + '</span><span class="ind-val">' + esc(item.val) + '</span></div>';
                }
            });
            html += '</div>';
        }

        // Factor validation (VALID cards) — shows which factors passed/failed
        var fvValid = o.factor_validation || {};
        var fvValidKeys = Object.keys(fvValid);
        if (fvValidKeys.length) {
            var fvLabels2 = {
                sector_momentum: '板块动量',
                relative_strength: '相对强度',
                ma_alignment: '均线排列',
                volume_breakout: '量能突破',
                rsi_sweet_spot: 'RSI位置',
                insider_signal: '内部人信号'
            };
            var fvDescs2 = {
                sector_momentum: '所属板块当日涨跌',
                relative_strength: '相对SPY的超额收益',
                ma_alignment: 'MA5/10/20多空排列',
                volume_breakout: '成交量 vs 均量',
                rsi_sweet_spot: 'RSI在40-60健康区间',
                insider_signal: '内部人买入/卖出'
            };
            html += '<div class="dr-fv-grid" style="margin-bottom:8px">';
            fvValidKeys.forEach(function(k) {
                var val = fvValid[k] || '—';
                var cls = val === 'CONFIRMED' ? 'ok' : (val === 'WEAK' ? 'warn' : 'neutral');
                var icon = val === 'CONFIRMED' ? '✅' : (val === 'WEAK' ? '⚠️' : '·');
                html += '<span class="dr-fv-item ' + cls + '" title="' + esc(fvDescs2[k] || k) + '">' + icon + ' ' + esc(fvLabels2[k] || k) + ' <b>' + esc(val) + '</b></span>';
            });
            html += '</div>';
        }

        // Data cited (VALID cards)
        if (o.data_cited && o.data_cited.length) {
            html += '<div class="dr-nr-cited" style="margin-bottom:8px">';
            html += '<span class="nr-cited-label">数据依据 (实时):</span>';
            o.data_cited.forEach(function(d) {
                html += '<span class="nr-cited-item">' + esc(d) + '</span>';
            });
            html += '</div>';
        }

        // Bull/Bear debate
        if (bullCase.length || bearCase.length) {
            html += '<div class="dr-debate">';
            html += '<div class="dr-debate-col bull">';
            html += '<div class="dr-debate-hd">🟢 多头论证 (' + bullCase.length + ')</div>';
            bullCase.forEach(function(p) {
                html += '<div class="dr-argument">';
                if (p.strength) html += '<span class="dr-arg-strength ' + esc(p.strength) + '">' + esc(p.strength) + '</span>';
                if (p.source_type) html += '<span class="dr-arg-source ' + esc(p.source_type) + '">' + esc(p.source_type) + '</span>';
                html += esc(p.thesis || '');
                if (p.evidence) html += '<span class="dr-arg-evidence">' + esc(p.evidence) + '</span>';
                html += '</div>';
            });
            html += '</div>';
            html += '<div class="dr-debate-col bear">';
            html += '<div class="dr-debate-hd">🔴 空头论证 (' + bearCase.length + ')</div>';
            bearCase.forEach(function(p) {
                html += '<div class="dr-argument">';
                if (p.strength) html += '<span class="dr-arg-strength ' + esc(p.strength) + '">' + esc(p.strength) + '</span>';
                if (p.source_type) html += '<span class="dr-arg-source ' + esc(p.source_type) + '">' + esc(p.source_type) + '</span>';
                html += esc(p.thesis || '');
                if (p.evidence) html += '<span class="dr-arg-evidence">' + esc(p.evidence) + '</span>';
                html += '</div>';
            });
            html += '</div>';
            html += '</div>';
        }

        // Verdict summary
        if (debateVerdict && typeof debateVerdict === 'object') {
            html += '<div class="dr-verdict-summary">';
            if (debateVerdict.key_disagreement) {
                html += '<div class="vs-row"><span class="vs-label">核心分歧</span><span class="vs-val">' + esc(debateVerdict.key_disagreement) + '</span></div>';
            }
            if (debateVerdict.adopted_from_bear) {
                html += '<div class="vs-row"><span class="vs-label">采纳风险</span><span class="vs-val" style="color:#fbbf24">' + esc(debateVerdict.adopted_from_bear) + '</span></div>';
            }
            if (debateVerdict.strongest_bull) {
                html += '<div class="vs-row"><span class="vs-label">最强多头</span><span class="vs-val" style="color:#4ade80">' + esc(debateVerdict.strongest_bull) + '</span></div>';
            }
            if (debateVerdict.strongest_bear) {
                html += '<div class="vs-row"><span class="vs-label">最强空头</span><span class="vs-val" style="color:#f87171">' + esc(debateVerdict.strongest_bear) + '</span></div>';
            }
            html += '</div>';
        }

        // Risk tiers
        if (riskTiers && typeof riskTiers === 'object') {
            var tiers = [
                {key: 'aggressive', label: '激进'},
                {key: 'moderate', label: '中性'},
                {key: 'conservative', label: '保守'},
            ];
            var hasTiers = tiers.some(function(t){return riskTiers[t.key] && typeof riskTiers[t.key] === 'object';});
            if (hasTiers) {
                html += '<div class="dr-risk-tiers">';
                tiers.forEach(function(t) {
                    var tier = riskTiers[t.key];
                    if (tier && typeof tier === 'object') {
                        html += '<div class="dr-rt-tier ' + t.key + '">';
                        html += '<span class="tier-label">' + esc(t.label) + '</span>';
                        if (tier.action) html += '<div>' + esc(tier.action) + '</div>';
                        if (tier.trigger) html += '<div class="tier-trigger">' + esc(tier.trigger) + '</div>';
                        if (tier.risk) html += '<div style="font-size:10px;color:#f87171;margin-top:3px">⚠ ' + esc(tier.risk) + '</div>';
                        html += '</div>';
                    }
                });
                html += '</div>';
            }
        }

        // Data quality audit
        if (dataQuality && typeof dataQuality === 'object') {
            html += '<div class="dr-data-audit">';
            html += '<span class="dr-da-hd">数据完整性</span>';
            var dqAll = [
                ['技术面', dataQuality.technical],
                ['基本面', dataQuality.fundamental],
                ['新闻', dataQuality.news],
                ['情绪', dataQuality.sentiment],
            ];
            dqAll.forEach(function(d) {
                if (d[1]) {
                    var cls = (d[1]==='完整'||d[1]==='complete') ? 'ok' : (d[1]==='降级'||d[1]==='degraded' ? 'warn' : 'miss');
                    var icon = cls==='ok' ? '✅' : cls==='warn' ? '⚠️' : '❌';
                    html += '<span class="dr-da-item ' + cls + '">' + icon + ' ' + esc(d[0]) + '</span>';
                }
            });
            if (dataQuality.overall || dataQuality.overall_completeness) {
                html += '<span style="color:#6e7681;font-size:10px;margin-left:auto">' + esc(dataQuality.overall || dataQuality.overall_completeness) + '</span>';
            }
            if (dataQuality.fetched_at) {
                var fa = dataQuality.fetched_at;
                try {
                    var fetchTime = new Date(fa);
                    var now = new Date();
                    var diffMin = Math.round((now - fetchTime) / 60000);
                    var timeStr = diffMin < 60 ? diffMin + '分钟前' : (diffMin < 1440 ? Math.round(diffMin/60) + '小时前' : Math.round(diffMin/1440) + '天前');
                    var freshCls = diffMin < 120 ? 'fresh' : (diffMin < 720 ? 'stale' : 'old');
                    html += '<span class="dr-da-fresh ' + freshCls + '" title="' + esc(fa) + '">🕐 ' + timeStr + '</span>';
                } catch(e) {
                    html += '<span style="color:#484f58;font-size:10px" title="' + esc(fa) + '">🕐 ' + esc(fa.slice(11,19)) + 'Z</span>';
                }
            }
            if (dataQuality.data_sources) {
                var ds = dataQuality.data_sources;
                var srcParts = [];
                if (ds.financials === 'ok') srcParts.push('Finnhub');
                if (ds.news === 'ok') srcParts.push('News');
                if (ds.analyst === 'ok') srcParts.push('Analyst');
                if (srcParts.length > 0) {
                    html += '<span style="color:#484f58;font-size:9px;margin-left:4px" title="数据源: ' + esc(srcParts.join(', ')) + '">src:' + srcParts.length + '</span>';
                }
            }
            html += '</div>';
            if (dataQuality.impact_note) {
                var noteColor = dataQuality.impact_note.indexOf('数据完整') >= 0 ? '#4ade80' : '#8b949e';
                html += '<div style="font-size:11px;color:' + noteColor + ';margin-bottom:10px;padding-left:4px">' + (dataQuality.impact_note.indexOf('数据完整') >= 0 ? '✅ ' : '⚠ ') + esc(dataQuality.impact_note) + '</div>';
            }
        }

        // Causal chain + historical reference
        if (dr && dr.causal_chain) {
            html += '<div style="margin-bottom:12px;padding:8px 12px;background:rgba(167,139,250,0.06);border-left:2px solid rgba(167,139,250,0.4);border-radius:0 4px 4px 0;font-size:12px;color:#c9d1d9">';
            html += '<span style="color:#a78bfa;font-weight:700;font-size:10px;letter-spacing:0.1em;text-transform:uppercase;display:block;margin-bottom:4px">🔗 因果传导</span>';
            var chain = String(dr.causal_chain).replace(/\s*(→|->|⇒|>|—>)\s*/g, ' <span style="color:#a78bfa;font-weight:700">→</span> ');
            html += chain;
            if (dr.historical_reference) {
                html += '<div style="margin-top:4px;padding-top:4px;border-top:1px dashed rgba(167,139,250,0.2);color:#8b949e;font-size:11px">📜 ' + esc(dr.historical_reference) + '</div>';
            }
            html += '</div>';
        }

        // Risk triggers table
        if (riskTriggers.length) {
            html += '<div class="dr-risk-triggers">';
            html += '<div class="dr-rt-hd">风险触发条件</div>';
            html += '<table class="dr-rt-table">';
            riskTriggers.forEach(function(rt) {
                html += '<tr>';
                html += '<td>' + esc(rt.condition || '') + '</td>';
                html += '<td>' + esc(rt.action || '') + '</td>';
                html += '</tr>';
            });
            html += '</table></div>';
        }

        // Key levels grid
        if (keyLevels && typeof keyLevels === 'object') {
            html += '<div class="dr-key-levels">';
            html += '<div class="dr-kl-hd">价格关键位</div>';
            html += '<div class="dr-kl-grid">';
            var klItems = [
                {key:'current', label:'当前价', cls:'current'},
                {key:'hard_stop', label:'硬止损', cls:'stop'},
                {key:'support_1', label:'第一支撑', cls:''},
                {key:'support_2', label:'第二支撑', cls:''},
                {key:'resistance_1', label:'第一阻力', cls:'resist'},
                {key:'resistance_2', label:'第二阻力', cls:'resist'},
                {key:'extreme_down', label:'极端下行', cls:'stop'},
            ];
            klItems.forEach(function(kl) {
                var val = keyLevels[kl.key];
                if (val != null && val !== 'N/A' && val !== '') {
                    html += '<div class="dr-kl-item ' + kl.cls + '">';
                    html += '<span class="kl-label">' + esc(kl.label) + '</span>';
                    html += '<span class="kl-val">$' + (typeof val === 'number' ? Number(val).toFixed(2) : esc(String(val))) + '</span>';
                    html += '</div>';
                }
            });
            html += '</div></div>';
        }

        // Fundamental snapshot
        if (dr && dr.analyst_reports && dr.analyst_reports.fundamental) {
            var fundR = dr.analyst_reports.fundamental;
            var fin = fundR.financials || {};
            var val = fundR.valuation || {};
            var cluster = fundR.analyst_cluster || {};
            var hasF = (fin && Object.keys(fin).some(function(k){return fin[k] && fin[k] !== 'N/A' && k !== 'source' && k !== 'fetched_at'})) ||
                       (val && Object.keys(val).some(function(k){return val[k] != null})) ||
                       (cluster && (cluster.cluster || cluster.total_analysts > 0));
            if (hasF) {
                html += '<div style="margin-bottom:12px">';
                html += '<div style="font:700 10px/1 IBM Plex Mono,monospace;color:#d29922;letter-spacing:0.16em;text-transform:uppercase;margin-bottom:6px">📊 基本面快照</div>';
                if (fin.fetched_at) html += '<div style="font-size:10px;color:#484f58;margin-bottom:4px">数据源: ' + esc(fin.source || 'API') + ' @ ' + esc(fin.fetched_at) + '</div>';
                html += '<table class="da-table"><tbody>';
                if (fin.market_cap) html += '<tr><td>市值</td><td>' + esc(String(fin.market_cap)) + '</td></tr>';
                if (fin.pe_ttm) html += '<tr><td>PE (TTM)</td><td>' + esc(String(fin.pe_ttm)) + '</td></tr>';
                if (fin.forward_pe) html += '<tr><td>Forward PE</td><td>' + esc(String(fin.forward_pe)) + '</td></tr>';
                if (fin.pb) html += '<tr><td>PB</td><td>' + esc(String(fin.pb)) + '</td></tr>';
                if (fin.roe) html += '<tr><td>ROE</td><td>' + esc(String(fin.roe)) + '</td></tr>';
                if (fin.eps_ttm) html += '<tr><td>EPS (TTM)</td><td>' + esc(String(fin.eps_ttm)) + '</td></tr>';
                if (fin.revenue_yoy) html += '<tr><td>营收增长</td><td>' + esc(String(fin.revenue_yoy)) + '</td></tr>';
                if (fin.gross_margin) html += '<tr><td>毛利率</td><td>' + esc(String(fin.gross_margin)) + '</td></tr>';
                if (fin.net_margin) html += '<tr><td>净利率</td><td>' + esc(String(fin.net_margin)) + '</td></tr>';
                if (fin.beta) html += '<tr><td>Beta</td><td>' + esc(String(fin.beta)) + '</td></tr>';
                if (fin.revenue && !fin.market_cap) html += '<tr><td>营收</td><td>' + esc(String(fin.revenue)) + '</td></tr>';
                if (fin.eps && !fin.eps_ttm) html += '<tr><td>EPS</td><td>' + esc(String(fin.eps)) + '</td></tr>';
                if (val.pe_ttm && !fin.pe_ttm) html += '<tr><td>PE (TTM)</td><td>' + esc(String(val.pe_ttm)) + '</td></tr>';
                if (val.beta && !fin.beta) html += '<tr><td>Beta</td><td>' + esc(String(val.beta)) + '</td></tr>';
                if (cluster.total_analysts > 0) {
                    html += '<tr><td>分析师</td><td>买入' + esc(String(cluster.buy_pct || 'N/A')) + ' 持有' + esc(String(cluster.hold_pct || 'N/A')) + ' 卖出' + esc(String(cluster.sell_pct || 'N/A')) + ' (' + cluster.total_analysts + '家, ' + esc(cluster.trend || '') + ')</td></tr>';
                }
                if (cluster.cluster) {
                    var c = cluster.cluster;
                    html += '<tr><td>目标价簇</td><td>$' + c.low + '-$' + c.high + ' (中位$' + c.median + ', ' + c.count + '家)</td></tr>';
                    if (cluster.position_in_cluster) html += '<tr><td>当前位置</td><td>' + esc(cluster.position_in_cluster) + '</td></tr>';
                }
                html += '</tbody></table></div>';
            }
        }

        // News timeline
        if (dr && dr.analyst_reports && dr.analyst_reports.news) {
            var newsR = dr.analyst_reports.news;
            var newsTL = newsR.news_timeline || [];
            var catalysts = newsR.catalysts || [];
            if (newsTL.length || catalysts.length) {
                html += '<div style="margin-bottom:12px">';
                html += '<div style="font:700 10px/1 IBM Plex Mono,monospace;color:#a78bfa;letter-spacing:0.16em;text-transform:uppercase;margin-bottom:6px">📰 新闻催化</div>';
                catalysts.forEach(function(c) {
                    html += '<div style="padding:4px 0;font-size:12px;color:#c9d1d9;border-top:1px dashed #21262d">';
                    html += '<span style="color:#fbbf24;font-weight:700;font-size:10px">催化</span> ';
                    html += esc(c.event || '') + ' <span style="color:#6e7681">(' + esc(c.date || '?') + ')</span>';
                    html += '</div>';
                });
                newsTL.slice(0, 5).forEach(function(n) {
                    var sentCls = (n.sentiment||'').toLowerCase() === 'positive' ? 'color:#4ade80' : (n.sentiment||'').toLowerCase() === 'negative' ? 'color:#f87171' : 'color:#8b949e';
                    html += '<div style="padding:3px 0;font-size:11.5px;color:#c9d1d9;border-top:1px dashed #21262d">';
                    if (n.date) html += '<span style="color:#6e7681;font-size:10px">' + esc(String(n.date).slice(0,10)) + '</span> ';
                    html += '<span style="' + sentCls + '">[' + esc(n.sentiment || 'neutral') + ']</span> ';
                    html += esc((n.headline || '').slice(0, 100));
                    html += '</div>';
                });
                html += '</div>';
            }
        }

        // Sentiment
        if (dr && dr.analyst_reports && dr.analyst_reports.sentiment) {
            var sentR = dr.analyst_reports.sentiment;
            if (sentR.stocktwits || sentR.conclusion) {
                html += '<div style="margin-bottom:12px">';
                html += '<div style="font:700 10px/1 IBM Plex Mono,monospace;color:#f472b6;letter-spacing:0.16em;text-transform:uppercase;margin-bottom:6px">💬 社交情绪</div>';
                if (sentR.stocktwits) {
                    var st = sentR.stocktwits;
                    var bullPct = st.bull_bear_ratio ? Math.round(st.bull_bear_ratio * 100) : 50;
                    html += '<div style="display:flex;align-items:center;gap:8px;font-size:12px;margin-bottom:4px">';
                    html += '<span style="color:#4ade80;font-weight:700">' + (st.bullish || 0) + ' 多</span>';
                    html += '<span style="flex:1;height:4px;background:#21262d;border-radius:2px;overflow:hidden;max-width:100px"><span style="display:block;height:100%;width:' + bullPct + '%;background:#4ade80;border-radius:2px"></span></span>';
                    html += '<span style="color:#f87171;font-weight:700">' + (st.bearish || 0) + ' 空</span>';
                    if (st.watchlist_count) html += '<span style="color:#6e7681;font-size:10px">· ' + fmtK(st.watchlist_count) + ' 关注</span>';
                    html += '</div>';
                }
                html += '<div style="font-size:11.5px;color:#8b949e">' + esc(sentR.conclusion || '') + '</div>';
                html += '</div>';
            }
        }

        // Invalidation
        if (o.invalidation) {
            html += '<div class="da-invalidation">❌ 失效条件: ' + esc(o.invalidation) + '</div>';
        }

        html += '</div>'; // end dr-report
        html += '</div>'; // end dr-verdict-card
    });

    // ── Position Management rendered in Layer 2 above, skip legacy section ──

    // ── Layer 5: Context Footer (Avoid list + Discovery stats) ──
    if (avoid.length) {
        html += '<div class="ctx-footer">';
        html += '<span class="ctx-label">回避:</span>';
        avoid.forEach(function(a) {
            var sym = a.symbol.replace('US.','');
            html += '<span class="avoid-chip" title="' + esc(a.reason) + '">' + esc(sym) + '</span>';
        });
        html += '</div>';
    }

    // Discovery meta (v2: 多源统计)
    var dm = d.discovery_meta || {};
    if (dm.core_pool_size || dm.event_discovered) {
        html += '<div class="ctx-footer discovery">';
        html += '<span class="ctx-label">发现:</span>';
        html += '<span>核心池' + (dm.core_pool_size || 0) + '</span>';
        if (dm.event_discovered) html += '<span>+ 多源发现' + dm.event_discovered + '</span>';
        if (dm.quality_gate_removed) html += '<span>→ 质量闸剔除' + dm.quality_gate_removed + '</span>';
        html += '<span>→ 终池' + (dm.final_pool_size || 0) + '</span>';
        if (dm.event_symbols && dm.event_symbols.length) {
            html += '<span class="discovery-new-syms" title="新发现标的">★ ' + dm.event_symbols.slice(0,5).map(function(s){return s.replace('US.','')}).join(', ') + '</span>';
        }
        html += '</div>';
    }

    // Intraday scan status
    var ps = d.pipeline_stats || {};
    if (ps.last_intraday_scan) {
        var scanTime = ps.last_intraday_scan;
        var scanTimeStr = scanTime ? scanTime.slice(11,16) + ' UTC' : '';
        var intradayCount = ps.intraday_injected || 0;
        html += '<div class="ctx-footer intraday-scan">';
        html += '<span class="intraday-scan-icon">🔍</span>';
        html += '<span>盘中扫描: ' + scanTimeStr + '</span>';
        if (intradayCount > 0) html += '<span class="intraday-scan-count">+ ' + intradayCount + ' 盘中发现</span>';
        html += '</div>';
    }

    // Footer
    html += '<div style="text-align:center;padding:12px;font-size:11px;color:#484f58">';
    html += '更新于 ' + new Date().toLocaleTimeString('zh-CN',{hour12:false});
    var stats = d.pipeline_stats || {};
    if (stats.deep_research_count) html += ' · Deep Research: ' + stats.deep_research_count + ' targets';
    if (stats.total_sec) html += ' · Pipeline: ' + stats.total_sec + 's';
    html += '</div>';

    el.innerHTML = html;
}

loadPlan();
setInterval(loadPlan, 60000);