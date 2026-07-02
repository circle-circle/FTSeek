// ═══════════════════════════════════════════════════════════════
// dashboard.js — Dashboard page logic (extracted from dashboard.html)
// ═══════════════════════════════════════════════════════════════
(function() {
    // ── Helpers ──
    function fmt(n,d){d=d||2;return n==null||isNaN(n)?'--':Number(n).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});}
    function fmtK(n){return n==null?'--':Math.abs(n)>=1e6?(n/1e6).toFixed(1)+'M':Math.abs(n)>=1e3?(n/1e3).toFixed(1)+'K':n.toFixed(0);}
    function pnlCls(n){return n>=0?'green':'red';}

    // ── Source type label helper (used by today-closed + review) ──
    function _srcLabel(s) {
        if (s === 'MANUAL') return '<span style="font-size:9px;padding:1px 4px;border-radius:2px;background:rgba(88,166,255,0.15);color:#58a6ff">手动</span>';
        if (s === 'POSITION_SYNC') return '<span style="font-size:9px;padding:1px 4px;border-radius:2px;background:rgba(251,191,36,0.15);color:#fbbf24">外部</span>';
        if (s === 'PLAYBOOK_SIM' || s === 'PLAYBOOK_LIVE') return '<span style="font-size:9px;padding:1px 4px;border-radius:2px;background:rgba(192,132,252,0.15);color:#c084fc">PB</span>';
        if (s === 'ORB_ENGINE') return '<span style="font-size:9px;padding:1px 4px;border-radius:2px;background:rgba(45,212,191,0.15);color:#2dd4bf">ORB</span>';
        if (s === 'MAIN_PIPELINE') return '<span style="font-size:9px;padding:1px 4px;border-radius:2px;background:rgba(74,222,128,0.15);color:#4ade80">主线</span>';
        return '<span style="font-size:9px;padding:1px 4px;border-radius:2px;background:rgba(74,222,128,0.15);color:#4ade80">自动</span>';
    }

    // escHtml 由 /static/js/utils.js 全局提供

    // ── Safe render wrapper (防止一个卡片报错导致整页空白) ──
    function safeRender(name, fn) {
        try {
            var args = Array.prototype.slice.call(arguments, 2);
            return fn.apply(null, args);
        } catch (e) {
            console.error('[dashboard] render ' + name + ' failed:', e);
        }
    }

    // ── Collapsible Sections ──
    function toggleSection(id) {
        var body = document.getElementById(id+'Body');
        var arrow = document.getElementById(id+'Arrow');
        body.classList.toggle('open');
        arrow.classList.toggle('open');
        if (window.Prefs) Prefs.set('sec_' + id, body.classList.contains('open'));
    }

    // ── Tab Switching (lazy load) ──
    var _tabLoaded = {};
    function switchTab(name) {
        document.querySelectorAll('#analyticsTabs .tab').forEach(function(t){ t.classList.toggle('active', t.dataset.tab === name); });
        document.querySelectorAll('.tab-panel').forEach(function(p){ p.classList.toggle('active', p.id === 'tab-'+name); });
        if (!_tabLoaded[name]) { _tabLoaded[name] = true; loadTabContent(name); }
    }
    function loadTabContent(name) {
        var loaders = { equity: loadEquity, review: loadReview, tradeReview: loadTradeReview };
        if (loaders[name]) loaders[name]();
    }
    function switchToAnalytics() {
        var body = document.getElementById('analyticsBody');
        if (!body.classList.contains('open')) toggleSection('analytics');
    }

    // ══════════════════════════════════════════════════════
    //  统一 Snapshot — 一次请求替代 8+ 并行 API
    // ══════════════════════════════════════════════════════

    var _lastSnapshot = null;

    async function loadSnapshot() {
        try {
            var res = await fetch('/api/dashboard/snapshot');
            _lastSnapshot = await res.json();
            renderSnapshot(_lastSnapshot);
        } catch (e) {
            console.error('Snapshot error:', e);
            _dashFailCount = (_dashFailCount || 0) + 1;
            if (_dashFailCount >= 3) {
                var strip = document.querySelector('.status-strip');
                if (strip && !strip.querySelector('.api-err-banner')) {
                    var ban = document.createElement('div');
                    ban.className = 'api-err-banner';
                    ban.style.cssText = 'width:100%;text-align:center;color:#f87171;font-size:12px;padding:6px;background:rgba(248,113,113,0.1);border-radius:6px;margin-top:6px';
                    ban.textContent = '⚠️ 数据加载失败 — 请检查网络或刷新页面';
                    strip.appendChild(ban);
                }
            }
        }
    }

    var _dashFailCount = 0;

    function renderSnapshot(d) {
        if (!d) return;
        _dashFailCount = 0;
        var oldBan = document.querySelector('.api-err-banner');
        if (oldBan) oldBan.remove();

        var positions = d.positions || [];

        // ── Dispatch to independent section renderers ──
        safeRender('statusStrip', renderStatusStrip, d.pnl || {}, d.account || {}, d.margin || {}, positions, d.decision);
        safeRender('modeTags', renderModeTags, d.is_live);
        safeRender('marketPills', renderMarketPills, d.market || {});
        safeRender('secondaryPills', renderSecondaryPills, d.margin || {}, d.today_orders, d.earnings || {}, d.exposure || {});
        safeRender('todayClosed', renderTodayClosed, d.today_closed || [], d.today_closed_total || 0);
        safeRender('graduation', renderGraduation, d.graduation || null, d.auto_account || 'sim');
        safeRender('factorStats', renderFactorStats, d.factor_stats || []);
        safeRender('unifiedPositions', renderUnifiedPositions, d.lifecycle || [], positions);
        safeRender('positionTotals', renderPositionTotals, positions, d.exposure || {});
        safeRender('actionZone', renderActionZone, d.lifecycle || [], d.earnings || {});
        safeRender('dailyReport', renderDailyReport, d);
        safeRender('decisionSummary', renderDecisionSummary, d.decision);
        safeRender('concentrationView', renderConcentrationView, d.concentration);
        safeRender('okr', renderOKR, d.okr_summary || null, d.pnl || {});

        // ── Shared position data (used by thesis/protection features) ──
        _posData = positions;
        _totalMv = positions.reduce(function(s,p){ return s + (p.market_val || p.market_value || 0); }, 0);

        // ── Bottom bar + staleness indicator ──
        var bottomEl = document.getElementById('bottomUpdated');
        if (bottomEl) {
            var nowTs = Date.now();
            _lastDataTime = nowTs;
            bottomEl.textContent = '上次刷新: ' + new Date().toLocaleTimeString('zh-CN', {hour12:false});
            bottomEl.style.color = '';
        }

        // ── Refresh indicator ──
        var rdot = document.getElementById('refreshDot');
        if (rdot) {
            rdot.classList.add('updated');
            clearTimeout(rdot._timer);
            rdot._timer = setTimeout(function(){ rdot.classList.remove('updated'); }, 2000);
        }
    }

    // ── Staleness Detection: 如果数据超过 90s 未刷新，显示警告 ──
    var _lastDataTime = Date.now();
    setInterval(function() {
        var elapsed = (Date.now() - _lastDataTime) / 1000;
        var bottomEl = document.getElementById('bottomUpdated');
        if (bottomEl && elapsed > 90) {
            bottomEl.style.color = '#fbbf24';
            bottomEl.textContent = '⚠ 数据已 ' + Math.round(elapsed) + 's 未刷新';
        }
    }, 10000);

    // ── Status Strip Core (PnL + system dot + position count) ──
    function renderStatusStrip(pnl, account, margin, positions, decision) {
        var pnlToday = pnl.today_pl || pnl.total || 0;
        var pnlLabel = pnl.today_pl_label || '今日';
        var isSettlement = pnlLabel.indexOf('T-1') >= 0 || pnlLabel.indexOf('结算') >= 0;
        var pnlEl = document.getElementById('pillPnl');
        pnlEl.textContent = (pnlToday >= 0 ? '+$' : '-$') + fmt(Math.abs(pnlToday));
        // P0-T1: 结算数据用灰色, 实时数据用红绿
        pnlEl.className = 'v ' + (isSettlement ? 'settlement' : pnlCls(pnlToday));
        // 显示语义标签 (T-1结算/今日浮动)
        var labelEl = document.getElementById('pillPnlLabel');
        if (labelEl) {
            labelEl.textContent = pnlLabel;
            labelEl.style.color = isSettlement ? '#fbbf24' : '#8b949e';
        }
        var corePnl = document.getElementById('corePnl');
        if (corePnl) corePnl.style.borderColor = isSettlement ? 'rgba(251,191,36,0.3)' : (pnlToday >= 0 ? 'rgba(74,222,128,0.3)' : 'rgba(248,113,113,0.3)');

        document.getElementById('pillPositions').textContent = positions.length;

        var pillEquity = document.getElementById('pillEquity');
        if (pillEquity) pillEquity.innerHTML = '💰 <span class="v">$' + fmtK(account.total_assets || 0) + '</span>';

        var marginPct = margin.usage_pct || 0;
        var dot = document.getElementById('sysDot');
        var label = document.getElementById('sysLabel');
        var waterLevel = (decision && decision.risk) ? decision.risk.water_level : 'unknown';
        if (_riskCmdPaused || marginPct > 80 || waterLevel === 'red') {
            dot.className = 'dot danger'; label.textContent = '系统警告';
        } else if (marginPct > 60 || waterLevel === 'yellow') {
            dot.className = 'dot warn'; label.textContent = '注意';
        } else {
            dot.className = 'dot ok'; label.textContent = '正常';
        }

        // Exposure inline
        var expInline = document.getElementById('pillExposureInline');
        if (expInline && (account.long_mv || account.short_mv)) {
            var gross = (account.long_mv || 0) + (account.short_mv || 0);
            expInline.textContent = '$' + fmtK(gross);
        }

        // Sim / Live realized PnL split
        var simLiveEl = document.getElementById('pillSimLive');
        if (simLiveEl) {
            var simPnl = pnl.today_realized_sim || pnl.realized_sim || 0;
            var livePnl = pnl.today_realized_live || pnl.realized_live || 0;
            var simStr = (simPnl >= 0 ? '+$' : '-$') + fmt(Math.abs(simPnl));
            var liveStr = (livePnl >= 0 ? '+$' : '-$') + fmt(Math.abs(livePnl));
            simLiveEl.style.display = '';
            simLiveEl.innerHTML = '🟢 模拟 <span class="' + (simPnl >= 0 ? 'green' : 'red') + '">' + simStr + '</span> · 🔴 实盘 <span class="' + (livePnl >= 0 ? 'green' : 'red') + '">' + liveStr + '</span>';
        }
    }

    // ── Mode Tags (nav + footer) ──
    function renderModeTags(isLive) {
        var cls = isLive ? 'mode-tag live' : 'mode-tag sim';
        var text = isLive ? '🔴 实盘' : '🟢 模拟';
        ['modeTag', 'modeTag2', 'modeTag3', 'bottomMode'].forEach(function(id) {
            var el = document.getElementById(id);
            if (el) { el.className = cls; el.textContent = text; }
        });
    }

    // ── Market Pills (SPY + VIX) ──
    function renderMarketPills(market) {
        var spy = market.spy;
        if (spy) {
            var pct = spy.change_pct;
            document.getElementById('pillSpy').innerHTML = 'SPY <span class="' + pnlCls(pct) + '">' + (pct>=0?'+':'') + pct.toFixed(2) + '%</span>';
        }
        var vix = market.vix;
        if (vix) {
            var cls = vix < 15 ? 'green' : vix < 25 ? '' : vix < 35 ? 'yellow' : 'red';
            document.getElementById('pillVix').innerHTML = 'VIX <span class="' + cls + '">' + vix.toFixed(1) + '</span>';
        }
        updateMarketStatus();
    }

    // ── Secondary Pills (margin, activity, earnings, exposure) ──
    function renderSecondaryPills(margin, todayOrders, earnings, exposure) {
        var marginPct = margin.usage_pct || 0;
        var pillMarginVal = document.getElementById('pillMarginVal');
        if (pillMarginVal) {
            pillMarginVal.textContent = marginPct.toFixed(0) + '%';
            var pillMargin = document.getElementById('pillMargin');
            if (pillMargin) pillMargin.style.borderColor = marginPct > 70 ? '#f87171' : marginPct > 50 ? '#fbbf24' : '#30363d';
        }

        var toStats = todayOrders ? todayOrders.stats || {} : {};
        var actTotal = (toStats.active || 0) + (toStats.filled || 0);
        var pillActivity = document.getElementById('pillActivity');
        var pillActivityVal = document.getElementById('pillActivityVal');
        if (pillActivity && pillActivityVal) {
            if (actTotal > 0) {
                pillActivity.style.display = '';
                pillActivityVal.textContent = (toStats.active || 0) + '活跃/' + (toStats.filled || 0) + '成交';
            } else {
                pillActivity.style.display = 'none';
            }
        }

        var eToday = (earnings.today || []).length;
        var eUpcoming = (earnings.upcoming || []).length;
        var pillEarnings = document.getElementById('pillEarnings');
        var pillEarningsVal = document.getElementById('pillEarningsVal');
        if (pillEarnings && pillEarningsVal) {
            if (eToday > 0 || eUpcoming > 0) {
                pillEarnings.style.display = '';
                pillEarningsVal.textContent = eToday > 0 ? eToday + '今日财报' : eUpcoming + '近期财报';
                pillEarnings.style.borderColor = eToday > 0 ? '#fbbf24' : '#30363d';
            } else {
                pillEarnings.style.display = 'none';
            }
        }
    }

    // ── Today's Closed Trades ──
    // ── 今日已平 状态 (跨刷新保持) ──
    var _tcSort = 'pnl_desc';   // pnl_desc|pnl_asc|time|hold
    var _tcView = 'detail';     // detail|aggregate
    var _tcPage = 1;
    var _tcPageSize = 10;

    function _tcFmtHold(hh) {
        hh = hh || 0;
        return hh <= 0 ? '-' : (hh < 1 ? Math.round(hh * 60) + 'm' : hh.toFixed(1) + 'h');
    }
    function _tcReasonTag(reason) {
        var m = {'SL_ATR_STOP':['止损','#f87171'],'TIME_EXIT':['时间','#8b949e'],'PRE_CLOSE_EXIT':['盘末','#60a5fa'],'EOD_FORCE_CLOSE':['EOD兜底','#fbbf24'],'MAX_HOLD_T5':['T+5到期','#c084fc'],'OVERNIGHT_GAP_STOP':['跳空止损','#fb7185'],'ORB_BREAKDOWN':['突破失败','#f87171'],'ORB_TAKE_PROF':['止盈','#4ade80'],'ORB_FLOOR_STOP':['底线止损','#f87171'],'ORB_EOD_EXIT':['ORB盘末','#fbbf24']};
        return m[reason] || [reason || '', '#484f58'];
    }
    function _tcSortFn(a, b) {
        if (_tcSort === 'pnl_desc') return (b.pnl_usd||0) - (a.pnl_usd||0);
        if (_tcSort === 'pnl_asc')  return (a.pnl_usd||0) - (b.pnl_usd||0);
        if (_tcSort === 'hold')     return (b.holding_hours||0) - (a.holding_hours||0);
        return (b.exit_time_raw||'').localeCompare(a.exit_time_raw||''); // time desc
    }

    function renderTodayClosed(closedList, totalCount) {
        var tcCard = document.getElementById('todayClosedCard');
        if (!tcCard) return;
        if (!closedList || closedList.length === 0) { tcCard.style.display = 'none'; return; }
        tcCard.style.display = '';

        var totalPnl = closedList.reduce(function(s,t){ return s + (t.pnl_usd || 0); }, 0);
        var wins = closedList.filter(function(t){ return t.pnl_usd > 0; }).length;
        var losses = closedList.filter(function(t){ return t.pnl_usd < 0; }).length;
        var winRate = closedList.length ? Math.round(wins / closedList.length * 100) : 0;

        document.getElementById('tcCount').textContent = '(' + (totalCount || closedList.length) + ')';
        var tcWlEl = document.getElementById('tcWl');
        if (tcWlEl) tcWlEl.innerHTML = '<span style="color:#4ade80">' + wins + '胜</span> / <span style="color:#f87171">' + losses + '亏</span> (' + winRate + '%)';
        var tcPnlEl = document.getElementById('tcPnl');
        tcPnlEl.textContent = (totalPnl >= 0 ? '+$' : '-$') + fmt(Math.abs(totalPnl));
        tcPnlEl.className = totalPnl >= 0 ? 'green' : 'red';

        // ── 排序 ──
        var sorted = closedList.slice().map(function(t){ t.exit_time_raw = t.exit_time || ''; return t; }).sort(_tcSortFn);

        // ── 视图: 明细 / 按股票聚合 ──
        var rowArr = [];
        if (_tcView === 'aggregate') {
            var agg = {};
            sorted.forEach(function(t){
                var k = t.symbol;
                if (!agg[k]) agg[k] = {symbol:k, n:0, wins:0, pnl:0, holdSum:0};
                agg[k].n++; agg[k].pnl += (t.pnl_usd||0); agg[k].holdSum += (t.holding_hours||0);
                if (t.pnl_usd > 0) agg[k].wins++;
            });
            Object.values(agg).sort(function(a,b){ return b.pnl - a.pnl; }).forEach(function(a){
                var wr = Math.round(a.wins / a.n * 100);
                var cls = a.pnl >= 0 ? 'green' : 'red';
                var wrColor = wr >= 55 ? '#4ade80' : (wr >= 40 ? '#fbbf24' : '#f87171');
                rowArr.push('<div style="display:flex;align-items:center;gap:8px;padding:5px 0;font-size:12px;border-bottom:1px solid #21262d">' +
                    '<span style="font-weight:600;min-width:54px">' + a.symbol + '</span>' +
                    '<span style="font-size:10px;color:#8b949e">' + a.n + '笔</span>' +
                    '<span style="font-size:10px;color:' + wrColor + '">' + a.wins + '胜' + (a.n-a.wins) + '亏 ' + wr + '%</span>' +
                    '<span style="margin-left:auto;font-weight:600" class="' + cls + '">' + (a.pnl >= 0 ? '+$' : '-$') + fmt(Math.abs(a.pnl)) + '</span>' +
                    '<span style="min-width:52px;text-align:right;font-size:11px;color:#484f58">avg ' + _tcFmtHold(a.holdSum/a.n) + '</span>' +
                    '</div>');
            });
        } else {
            sorted.forEach(function(t){
                var cls = t.pnl_pct >= 0 ? 'green' : 'red';
                var rm = _tcReasonTag(t.exit_reason);
                var dirMap = {'LONG':'多','SHORT':'空'};
                var dir = dirMap[t.direction] || t.direction || '';
                var dirCls = t.direction === 'SHORT' ? '#f87171' : '#4ade80';
                rowArr.push('<div style="display:flex;align-items:center;gap:6px;padding:4px 0;font-size:12px;border-bottom:1px solid #21262d">' +
                    '<span style="font-weight:600;min-width:48px">' + t.symbol + '</span>' +
                    '<span style="font-size:10px;font-weight:600;color:' + dirCls + ';min-width:14px">' + dir + '</span>' +
                    (rm[0] ? '<span style="font-size:10px;padding:1px 5px;border-radius:8px;background:' + rm[1] + '22;color:' + rm[1] + ';border:1px solid ' + rm[1] + '44">' + rm[0] + '</span>' : '') +
                    (_srcLabel(t.source_type || '')) +
                    '<span style="margin-left:auto;font-weight:600" class="' + cls + '">' + (t.pnl_pct >= 0 ? '+' : '') + t.pnl_pct + '%</span>' +
                    '<span class="' + cls + '" style="min-width:62px;text-align:right">' + (t.pnl_usd >= 0 ? '+$' : '-$') + fmt(Math.abs(t.pnl_usd)) + '</span>' +
                    '<span style="min-width:38px;text-align:right;font-size:11px;color:#484f58" title="持仓时长">' + _tcFmtHold(t.holding_hours) + '</span>' +
                    '</div>');
            });
        }

        // ── 分页 (明细视图分页, 聚合视图行少全显示) ──
        var total = rowArr.length;
        var totalPages = Math.max(1, Math.ceil(total / _tcPageSize));
        if (_tcPage > totalPages) _tcPage = totalPages;
        if (_tcPage < 1) _tcPage = 1;
        var start = (_tcPage - 1) * _tcPageSize;
        var pagedRows = (_tcView === 'detail')
            ? rowArr.slice(start, start + _tcPageSize).join('')
            : rowArr.join('');

        // ── 控制条 ──
        var controls = '<div style="display:flex;align-items:center;gap:8px;padding:4px 14px;font-size:11px;flex-wrap:wrap">' +
            '<span style="color:#8b949e">排序</span>' +
            '<select id="tcSortSel" style="background:#161b22;color:#e1e4e8;border:1px solid #30363d;border-radius:4px;padding:1px 4px;font-size:11px">' +
                '<option value="pnl_desc"' + (_tcSort==='pnl_desc'?' selected':'') + '>收益↓</option>' +
                '<option value="pnl_asc"' + (_tcSort==='pnl_asc'?' selected':'') + '>收益↑</option>' +
                '<option value="time"' + (_tcSort==='time'?' selected':'') + '>时间</option>' +
                '<option value="hold"' + (_tcSort==='hold'?' selected':'') + '>持仓时长</option>' +
            '</select>' +
            '<span style="color:#8b949e;margin-left:6px">视图</span>' +
            '<span id="tcViewToggle" style="display:flex;gap:0">' +
                '<button data-v="detail" style="background:' + (_tcView==='detail'?'#238636':'#21262d') + ';color:#e1e4e8;border:1px solid #30363d;padding:1px 8px;font-size:11px;border-radius:4px 0 0 4px;cursor:pointer">明细</button>' +
                '<button data-v="aggregate" style="background:' + (_tcView==='aggregate'?'#238636':'#21262d') + ';color:#e1e4e8;border:1px solid #30363d;padding:1px 8px;font-size:11px;border-radius:0 4px 4px 0;cursor:pointer">按股票</button>' +
            '</span>' +
            '<span style="margin-left:auto;color:#484f58">第 ' + _tcPage + '/' + totalPages + ' 页 · 共 ' + total + ' 笔</span>' +
            (_tcPage > 1 ? '<button id="tcPrev" style="background:#21262d;color:#8b949e;border:1px solid #30363d;padding:1px 8px;font-size:11px;border-radius:4px;cursor:pointer">‹</button>' : '') +
            (_tcPage < totalPages ? '<button id="tcNext" style="background:#21262d;color:#8b949e;border:1px solid #30363d;padding:1px 8px;font-size:11px;border-radius:4px;cursor:pointer">›</button>' : '') +
            '</div>';

        document.getElementById('todayClosedList').innerHTML = controls + '<div style="padding:0 14px 6px">' + pagedRows + '</div>';

        // ── 绑定事件 ──
        var sortSel = document.getElementById('tcSortSel');
        if (sortSel) sortSel.onchange = function(){ _tcSort = this.value; _tcPage = 1; renderTodayClosed(closedList, totalCount); };
        var togBtns = document.querySelectorAll('#tcViewToggle button');
        togBtns.forEach(function(b){ b.onclick = function(){ _tcView = b.getAttribute('data-v'); _tcPage = 1; renderTodayClosed(closedList, totalCount); }; });
        var prev = document.getElementById('tcPrev'); if (prev) prev.onclick = function(){ _tcPage--; renderTodayClosed(closedList, totalCount); };
        var next = document.getElementById('tcNext'); if (next) next.onclick = function(){ _tcPage++; renderTodayClosed(closedList, totalCount); };
    }


    // ══════════════════════════════════════
    //  OKR Card (盈利目标进度)
    // ══════════════════════════════════════
    function renderOKR(okr, pnl) {
        var card = document.getElementById('okrCard');
        if (!card) return;

        var hasData = okr && okr.has_data;
        var c = (okr && okr.cumulative) || {};
        var tg = (okr && okr.targets) || {};
        var score = (okr && okr.overall_score) || 0;

        // If no OKR data, hide the card (consistent with other analytics cards)
        if (!hasData) {
            card.style.display = 'none';
            return;
        }

        card.style.display = '';

        // Overall score / update time
        var scoreEl = document.getElementById('okrOverallScore');
        if (scoreEl) {
            scoreEl.textContent = score ? '综合 ' + score.toFixed(0) + '%' : '';
            scoreEl.style.color = score >= 70 ? '#4ade80' : (score >= 40 ? '#fbbf24' : '#8b949e');
        }
        var updatedEl = document.getElementById('okrUpdatedAt');
        if (updatedEl) updatedEl.textContent = (okr && okr.updated_at) ? okr.updated_at.substring(0, 10) : '';

        // Helper for single bar
        function krBar(label, val, target, fmtFn, color) {
            val = val || 0;
            target = target || 0;
            var pct = target ? Math.min(100, Math.round(Math.abs(val) / target * 100)) : 0;
            var met = val >= target && target > 0;
            var displayVal = fmtFn ? fmtFn(val) : val;
            return '<div style="flex:1;min-width:140px">' +
                '<div style="display:flex;justify-content:space-between;font-size:10px;color:#8b949e;margin-bottom:3px">' +
                '<span>' + label + '</span>' +
                '<span style="color:' + (met ? '#4ade80' : color) + '">' + displayVal + ' / ' + target + '</span></div>' +
                '<div style="height:5px;background:#21262d;border-radius:3px;overflow:hidden">' +
                '<div style="height:100%;width:' + pct + '%;background:' + (met ? '#4ade80' : color) + ';border-radius:3px;transition:width 0.3s"></div></div></div>';
        }

        // Use default targets when missing
        var targets = {
            win_rate: tg.win_rate || 55,
            weekly_pnl: tg.weekly_pnl || 1000,
            sample: tg.sample || 30,
            ic_t3: tg.ic_t3 || 0.05
        };

        var barsHtml = '<div style="display:flex;gap:10px;flex-wrap:wrap">' +
            krBar('胜率', c.win_rate, targets.win_rate, function(v){return (v||0)+'%';}, '#60a5fa') +
            krBar('周PnL', c.weekly_pnl, targets.weekly_pnl, function(v){return '$'+(v||0);}, '#fbbf24') +
            krBar('样本', c.sample, targets.sample, function(v){return v||0;}, '#a78bfa') +
            krBar('IC(T3)', c.ic_t3, targets.ic_t3, function(v){return v==null?'N/A':Number(v).toFixed(3);}, '#a78bfa') +
            '</div>';

        // Add a small summary line for today's contribution
        var today = (okr && okr.today) || {};
        if (today.count) {
            barsHtml += '<div style="margin-top:8px;padding-top:8px;border-top:1px solid #21262d;font-size:11px;color:#8b949e">' +
                '今日已平 ' + today.count + ' 笔 · 胜率 ' + (today.win_rate||0) + '% · 盈亏 ' + (today.total_pnl >= 0 ? '+$' : '-$') + fmt(Math.abs(today.total_pnl||0)) +
                '</div>';
        }

        document.getElementById('okrBars').innerHTML = barsHtml;
    }

    // ══════════════════════════════════════
    //  Daily Report (交易日报 — 收盘后显示)
    // ══════════════════════════════════════
    function renderDailyReport(d) {
        var el = document.getElementById('dailyReport');
        if (!el) return;

        // 判断是否应该显示日报: 收盘后 (ET 16:00+) 或周末
        var now = new Date();
        // 使用 Intl API 正确计算 ET 时间 (自动处理 DST/EST 切换)
        var etDate = new Date(now.toLocaleString('en-US', {timeZone: 'America/New_York'}));
        var etH = etDate.getHours();
        var dow = etDate.getDay();
        var isWeekend = (dow === 0 || dow === 6);
        var isAfterClose = (etH >= 16 || etH < 4); // ET 16:00 ~ 次日 04:00

        if (!isWeekend && !isAfterClose) {
            el.style.display = 'none';
            return;
        }

        var a = d.account || {};
        var p = d.pnl || {};
        var positions = d.positions || [];
        var closed = d.today_closed || [];
        var equity = a.total_assets || 0;
        var totalPnl = p.total || 0;
        var pnlPct = equity > 0 ? (totalPnl / (equity - totalPnl) * 100) : 0;
        var realized = p.realized_all || p.realized || 0;
        var unrealized = p.unrealized || 0;

        // 最佳/最差持仓
        var sorted = positions.slice().sort(function(a, b) { return (b.unrealized_pnl||0) - (a.unrealized_pnl||0); });
        var best = sorted[0] || null;
        var worst = sorted[sorted.length - 1] || null;

        // 交易统计
        var tradeCount = closed.length;
        var winCount = closed.filter(function(t) { return (t.pnl_usd||0) > 0; }).length;
        var winRate = tradeCount > 0 ? (winCount / tradeCount * 100) : 0;

        // 最近交易日: 周末→上周五, 周一凌晨→上周五, 其他→今天(ET)
        var lastTradeDay = new Date(etDate);
        if (dow === 0) lastTradeDay.setDate(lastTradeDay.getDate() - 2); // Sun → Fri
        else if (dow === 6) lastTradeDay.setDate(lastTradeDay.getDate() - 1); // Sat → Fri
        else if (dow === 1 && etH < 4) lastTradeDay.setDate(lastTradeDay.getDate() - 3); // Mon early → Fri
        var dateStr = lastTradeDay.getFullYear() + '-' + String(lastTradeDay.getMonth()+1).padStart(2,'0') + '-' + String(lastTradeDay.getDate()).padStart(2,'0');
        var dayNames = ['周日','周一','周二','周三','周四','周五','周六'];
        var dayName = dayNames[lastTradeDay.getDay()];
        var periodLabel = isWeekend ? '最近交易日' : (isAfterClose ? '今日收盘' : '今日盘中');

        var pnlCls = totalPnl >= 0 ? 'green' : 'red';
        var pnlSign = totalPnl >= 0 ? '+' : '-';

        var html = '<div class="daily-report">';
        html += '<div class="dr-header">';
        html += '<span class="dr-title">📊 交易日报</span>';
        html += '<span class="dr-date">' + periodLabel + ' ' + dateStr + ' ' + dayName + '</span>';
        html += '</div>';

        // 核心指标
        html += '<div class="dr-grid">';
        html += '<div class="dr-metric"><div class="label">总权益</div><div class="value" style="color:#e1e4e8">$' + fmtK(equity) + '</div></div>';
        html += '<div class="dr-metric"><div class="label">总盈亏</div><div class="value ' + pnlCls + '">' + pnlSign + '$' + fmt(Math.abs(totalPnl)) + ' (' + (pnlPct>=0?'+':'') + pnlPct.toFixed(1) + '%)</div></div>';
        html += '<div class="dr-metric"><div class="label">已实现</div><div class="value ' + (realized>=0?'green':'red') + '">' + (realized>=0?'+':'') + '$' + fmt(realized) + '</div></div>';
        html += '<div class="dr-metric"><div class="label">浮盈</div><div class="value ' + (unrealized>=0?'green':'red') + '">' + (unrealized>=0?'+':'') + '$' + fmt(unrealized) + '</div></div>';
        html += '</div>';

        // 最佳/最差
        html += '<div class="dr-trades">';
        if (best && (best.unrealized_pnl||0) > 0) {
            var bestSym = (best.symbol||'').replace('US.','');
            var bestPnl = best.unrealized_pnl || 0;
            var bestPct = best.pnl_pct || 0;
            html += '<div class="dr-trade"><span class="tag best">最佳</span> <b>' + bestSym + '</b> ';
            html += '<span class="green">+$' + fmt(bestPnl) + ' (+' + bestPct.toFixed(1) + '%)</span></div>';
        } else {
            html += '<div class="dr-trade" style="color:#484f58">无盈利持仓</div>';
        }
        if (worst && (worst.unrealized_pnl||0) < 0) {
            var worstSym = (worst.symbol||'').replace('US.','');
            var worstPnl = worst.unrealized_pnl || 0;
            var worstPct = worst.pnl_pct || 0;
            html += '<div class="dr-trade"><span class="tag worst">最差</span> <b>' + worstSym + '</b> ';
            html += '<span class="red">-$' + fmt(Math.abs(worstPnl)) + ' (' + worstPct.toFixed(1) + '%)</span></div>';
        } else {
            html += '<div class="dr-trade" style="color:#484f58">无亏损持仓</div>';
        }
        html += '</div>';

        // 交易统计
        if (tradeCount > 0) {
            html += '<div style="margin-top:8px;font-size:11px;color:#8b949e">';
            html += '今日交易 ' + tradeCount + ' 笔 · 胜率 ' + winRate.toFixed(0) + '%';
            html += '</div>';
        }

        html += '</div>';
        el.innerHTML = html;
        el.style.display = 'block';
    }


    // ══════════════════════════════════════
    //  Decision Summary (决策摘要)
    // ══════════════════════════════════════
    function renderDecisionSummary(dec) {
        var el = document.getElementById('decisionSummary');
        if (!el || !dec) { if (el) el.style.display = 'none'; return; }

        var act = dec.actions || {};
        var risk = dec.risk || {};
        var perf = dec.performance || {};

        // Line 1: Pending actions
        var l1 = '';
        var mustDo = act.must_do || 0;
        var opps = act.opportunities || 0;
        if (mustDo > 0) {
            l1 += '<span class="ds-tag urgent">' + mustDo + ' 项必须执行</span> ';
        }
        if (opps > 0) {
            l1 += '<span class="ds-tag opp">' + opps + ' 个机会</span> ';
        }
        if (mustDo === 0 && opps === 0) {
            l1 += '<span class="ds-tag ok">今日无紧急操作</span> ';
        }
        var regimeMap = {'RISK_ON':'偏多','RISK_OFF':'偏空','NEUTRAL':'震荡','ROTATION':'轮动','HIGH_VOL':'高波动','DEGRADED':'数据不足'};
        var regimeLabel = regimeMap[act.regime] || '';
        if (regimeLabel) {
            l1 += '<span style="color:var(--text-secondary);font-size:12px;margin-left:4px">市场' + regimeLabel + '</span>';
        }

        // Line 2: Risk context
        var vixVal = risk.vix || 0;
        var vixCls = vixVal < 15 ? 'ds-green' : vixVal < 25 ? '' : vixVal < 35 ? 'ds-yellow' : 'ds-red';
        var marginVal = risk.margin_pct || 0;
        var l2 = 'VIX <span class="' + vixCls + '">' + vixVal.toFixed(1) + '</span>' +
                 ' <span style="color:var(--text-dim)">·</span> ' +
                 '保证金 <span class="' + (marginVal > 70 ? 'ds-red' : marginVal > 50 ? 'ds-yellow' : '') + '">' + marginVal.toFixed(0) + '%</span>' +
                 ' <span style="color:var(--text-dim)">·</span> ' +
                 (risk.can_trade ? '<span class="ds-green">可开新仓</span>' : '<span class="ds-red">暂停开仓</span>');

        // Line 3: Performance
        var todayPl = perf.today_pl || 0;
        var weekRealized = perf.week_realized || 0;
        var weekWR = perf.week_win_rate || 0;
        var l3 = '今日 <span class="' + (todayPl >= 0 ? 'ds-green' : 'ds-red') + '">' +
                 (todayPl >= 0 ? '+$' : '-$') + fmt(Math.abs(todayPl)) + '</span>' +
                 ' <span style="color:var(--text-dim)">·</span> ' +
                 '本周已实现 <span class="' + (weekRealized >= 0 ? 'ds-green' : 'ds-red') + '">' +
                 (weekRealized >= 0 ? '+$' : '-$') + fmt(Math.abs(weekRealized)) + '</span>';
        if (perf.week_trades > 0) {
            l3 += ' <span style="color:var(--text-dim)">·</span> 胜率 ' + weekWR.toFixed(0) + '%';
        }

        document.getElementById('dsLine1').innerHTML = l1;
        document.getElementById('dsLine2').innerHTML = l2;
        document.getElementById('dsLine3').innerHTML = l3;
        el.style.display = 'block';
    }


    // ══════════════════════════════════════
    //  Concentration View (组合集中度)
    // ══════════════════════════════════════
    function renderConcentrationView(conc) {
        var card = document.getElementById('concentrationCard');
        if (!card || !conc || !conc.has_data) { if (card) card.style.display = 'none'; return; }

        card.style.display = 'block';

        // Warning badge in header
        var warnEl = document.getElementById('concWarnings');
        if (warnEl) {
            if (conc.warnings && conc.warnings.length > 0) {
                warnEl.innerHTML = '<span style="color:var(--yellow)">' + conc.warnings.length + '⚠</span>';
            } else {
                warnEl.textContent = '';
            }
        }

        // Sector breakdown bars
        var sectorEl = document.getElementById('concSectorBreakdown');
        if (sectorEl && conc.sectors && conc.sectors.length > 0) {
            var maxPct = Math.max.apply(null, conc.sectors.map(function(s) { return s.pct; }));
            sectorEl.innerHTML = conc.sectors.map(function(s) {
                var barWidth = maxPct > 0 ? (s.pct / maxPct * 100) : 0;
                var cls = s.pct > 40 ? 'conc-danger' : s.pct > 25 ? 'conc-warn' : '';
                var barColor = s.pct > 40 ? 'var(--red)' : s.pct > 25 ? 'var(--yellow)' : 'var(--accent)';
                return '<div class="conc-bar-row">' +
                    '<span class="conc-bar-label">' + escHtml(s.name) + '</span>' +
                    '<div class="conc-bar-track"><div class="conc-bar-fill" style="width:' + barWidth + '%;background:' + barColor + '"></div></div>' +
                    '<span class="conc-bar-pct ' + cls + '">' + s.pct.toFixed(0) + '%</span>' +
                    '<span style="font-size:11px;color:var(--text-dim);min-width:48px;text-align:right">$' + fmtK(s.market_val) + '</span>' +
                    '</div>';
            }).join('');
        }

        // Direction summary
        var dirEl = document.getElementById('concDirectionSummary');
        if (dirEl && conc.direction) {
            var longN = conc.direction.LONG || 0;
            var shortN = conc.direction.SHORT || 0;
            var dirHtml = '<span style="color:var(--green)">' + longN + ' LONG</span> / <span style="color:var(--red)">' + shortN + ' SHORT</span>';
            if (conc.same_direction) {
                dirHtml += ' <span style="color:var(--yellow);margin-left:8px">— 全部同向 ⚠</span>';
            }
            dirEl.innerHTML = dirHtml;

            // Warnings
            if (conc.warnings && conc.warnings.length > 0) {
                dirEl.innerHTML += conc.warnings.map(function(w) {
                    return '<div class="conc-warn">⚠ ' + escHtml(w) + '</div>';
                }).join('');
            }
        }
    }

    // ══════════════════════════════════════
    //  Unified Positions (排序 + 热力图 + 统一视图)
    // ══════════════════════════════════════
    var _lifecycleData = [];
    var _currentSort = (window.Prefs && Prefs.get('pos_sort', 'urgency')) || 'urgency';
    var _heatmapVisible = false;

    function sortPositions(mode) {
        _currentSort = mode;
        if (window.Prefs) Prefs.set('pos_sort', mode);
        document.querySelectorAll('.sort-btn').forEach(function(b) {
            b.classList.toggle('active', b.getAttribute('data-sort') === mode);
        });
        renderSortedList();
    }

    function getSortedData() {
        var data = _lifecycleData.slice();
        switch (_currentSort) {
            case 'pnl':
                data.sort(function(a, b) { return Math.abs(b.unrealized_pnl || 0) - Math.abs(a.unrealized_pnl || 0); });
                break;
            case 'equity':
                data.sort(function(a, b) { return (b.equity_pct || 0) - (a.equity_pct || 0); });
                break;
            case 'sl':
                data.sort(function(a, b) { return (a.sl_distance_pct || 99) - (b.sl_distance_pct || 99); });
                break;
            default: // urgency
                var order = { danger: 0, warning: 1, normal: 2 };
                data.sort(function(a, b) { return (order[a.alert_level] || 2) - (order[b.alert_level] || 2); });
        }
        return data;
    }

    function renderSortedList() {
        var el = document.getElementById('positionsList');
        var sorted = getSortedData();
        el.innerHTML = sorted.map(function(lc) { return renderPositionItem(lc); }).join('');
    }

    function toggleHeatmap() {
        _heatmapVisible = !_heatmapVisible;
        var view = document.getElementById('heatmapView');
        var list = document.getElementById('positionsList');
        var btn = document.getElementById('heatmapToggle');
        if (_heatmapVisible) {
            view.style.display = 'block';
            list.style.display = 'none';
            btn.style.borderColor = '#58a6ff';
            btn.style.color = '#58a6ff';
            renderHeatmap();
        } else {
            view.style.display = 'none';
            list.style.display = '';
            btn.style.borderColor = '#30363d';
            btn.style.color = '#8b949e';
        }
    }

    function renderHeatmap() {
        var el = document.getElementById('heatmapView');
        if (!_lifecycleData.length) { el.innerHTML = ''; return; }
        var totalMv = _lifecycleData.reduce(function(s, lc) { return s + (lc.equity_pct || 0); }, 0) || 1;
        // Grid: 2 columns, cell height proportional to equity_pct
        var cols = window.innerWidth < 600 ? 2 : 3;
        var html = '<div class="heatmap-grid" style="grid-template-columns:repeat(' + cols + ',1fr)">';
        // Sort by equity_pct desc for visual impact
        var sorted = _lifecycleData.slice().sort(function(a, b) { return (b.equity_pct || 0) - (a.equity_pct || 0); });
        sorted.forEach(function(lc) {
            var sym = (lc.symbol || '').replace('US.', '');
            var pct = lc.pnl_pct || 0;
            var eqPct = lc.equity_pct || 0;
            var weight = Math.max(eqPct / totalMv, 0.1);
            var minH = 48;
            var maxH = 120;
            var h = Math.round(minH + (maxH - minH) * weight);
            // Color: green for profit, red for loss, intensity by magnitude
            var absPct = Math.min(Math.abs(pct), 10);
            var intensity = Math.round(40 + absPct * 12);
            var bg, fg;
            if (pct >= 0) {
                bg = 'rgba(74,222,128,' + (intensity / 255).toFixed(2) + ')';
                fg = pct > 2 ? '#fff' : '#4ade80';
            } else {
                bg = 'rgba(248,113,113,' + (intensity / 255).toFixed(2) + ')';
                fg = pct < -2 ? '#fff' : '#f87171';
            }
            var pnlStr = (pct >= 0 ? '+' : '') + pct.toFixed(1) + '%';
            html += '<div class="heatmap-cell" style="background:' + bg + ';min-height:' + h + 'px" onclick="window.location=\'/stock/' + lc.symbol + '\'">';
            html += '<span class="hm-sym" style="color:' + fg + '">' + sym + '</span>';
            html += '<span class="hm-pnl" style="color:' + fg + '">' + pnlStr + '</span>';
            html += '<span style="font-size:9px;color:rgba(255,255,255,0.5)">' + eqPct.toFixed(0) + '%</span>';
            html += '</div>';
        });
        html += '</div>';
        el.innerHTML = html;
    }

    // ── Graduation 毕业门禁 (高信号: verdict + 切换开关) ──
    function renderGraduation(grad, autoAccount) {
        var card = document.getElementById('graduationCard');
        if (!card) return;
        var verdictEl = document.getElementById('gradVerdict');
        var checksEl = document.getElementById('gradChecks');
        var simBtn = document.getElementById('autoAccountSim');
        var realBtn = document.getElementById('autoAccountReal');
        if (!grad) {
            verdictEl.textContent = 'N/A'; verdictEl.style.background = '#21262d'; verdictEl.style.color = '#8b949e';
            checksEl.innerHTML = '<span style="color:#484f58">毕业数据未就绪</span>';
            return;
        }
        var graduated = grad.graduated;
        var checks = grad.checks || {};
        // verdict badge
        verdictEl.textContent = graduated ? '✅ 已毕业' : '⏳ 积累中';
        verdictEl.style.background = graduated ? '#1a4731' : '#3d2e00';
        verdictEl.style.color = graduated ? '#4ade80' : '#fbbf24';
        // 4 check chips (高信号, 配色 met/unmet)
        function chip(label, met, val) {
            var c = met ? '#4ade80' : '#f87171';
            return '<span style="display:flex;align-items:center;gap:4px">' +
                '<span style="color:' + c + '">' + (met ? '✓' : '✗') + '</span>' +
                '<span style="color:#8b949e">' + label + '</span>' +
                '<span style="color:#e1e4e8;font-weight:600">' + val + '</span></span>';
        }
        var ic = grad.ic_t3 == null ? 'N/A' : (grad.ic_t3 >= 0 ? '+' : '') + Number(grad.ic_t3).toFixed(3);
        var sample = grad.sample_count || 0;
        var icT1 = grad.ic_t1 == null ? 'N/A' : (grad.ic_t1 >= 0 ? '+' : '') + Number(grad.ic_t1).toFixed(3);
        var icT5 = grad.ic_t5 == null ? 'N/A' : (grad.ic_t5 >= 0 ? '+' : '') + Number(grad.ic_t5).toFixed(3);
        checksEl.innerHTML =
            chip('IC(T3)', checks.ic_positive, ic + '/0.05') +
            chip('样本', checks.sample_sufficient, sample + '/30') +
            chip('无5连亏', checks.no_consecutive_5_losses, checks.no_consecutive_5_losses ? 'OK' : '触发') +
            '<div style="margin-top:4px;padding-top:4px;border-top:1px solid #21262d;font-size:11px;color:#8b949e">' +
            'IC覆盖: T+1=' + (grad.sample_t1||0) + ' T+3=' + (grad.sample_t3||0) + ' T+5=' + (grad.sample_t5||0) + ' 条' +
            ' | IC(T1)=' + icT1 + ' IC(T5)=' + icT5 + '</div>';
        // auto_account 按钮 (real 未毕业灰色)
        function setBtn(btn, active, enabled) {
            btn.style.background = active ? '#238636' : '#21262d';
            btn.style.color = enabled ? '#e1e4e8' : '#484f58';
            btn.style.cursor = enabled ? 'pointer' : 'not-allowed';
            btn.style.opacity = enabled ? '1' : '0.5';
        }
        var isSim = autoAccount === 'sim';
        setBtn(simBtn, isSim, true);
        setBtn(realBtn, !isSim, graduated);  // real 需毕业才可点
        simBtn.onclick = function() { _switchAutoAccount('sim'); };
        realBtn.onclick = function() { if (graduated) _switchAutoAccount('real'); else alert('未毕业, 无法切实盘\n' + (grad.reason || '')); };
    }
    function _switchAutoAccount(account) {
        if (!confirm('确认自动交易账户切换到: ' + (account === 'real' ? '实盘' : '模拟') + '?\n(需重启 ftseek-us 生效)')) return;
        fetch('/api/auto-trade/auto-account', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({account: account})})
            .then(function(r){return r.json();})
            .then(function(d){
                if (d.ok) { alert('已切换 → ' + account + '\n需重启 ftseek-us 服务生效'); location.reload(); }
                else { var e = d.error || {}; alert('切换失败: ' + (e.message || JSON.stringify(d)) + (e.data && e.data.graduation ? '\n' + (e.data.graduation.reason||'') : '')); }
            })
            .catch(function(e){ alert('请求失败: ' + e); });
    }

    // ── Factor Performance 因子性能 (打磨杠杆, 高信号低噪声) ──
    function renderFactorStats(stats) {
        var list = document.getElementById('factorList');
        if (!list) return;
        var card = document.getElementById('factorCard');
        if (!stats || !stats.length) { list.innerHTML = '<div style="font-size:11px;color:#484f58;padding:4px 0">暂无因子数据 (待 sim 积累)</div>'; return; }
        // 信噪比: 只显示 ≥3 笔的因子 (少样本是噪声), 按笔数降序, 最多 10
        var rows = stats.filter(function(f){ return (f.total_trades || 0) >= 3; })
            .sort(function(a,b){ return (b.total_trades||0) - (a.total_trades||0); })
            .slice(0, 10);
        if (!rows.length) { list.innerHTML = '<div style="font-size:11px;color:#484f58;padding:4px 0">因子样本不足 (需 ≥3 笔)</div>'; return; }
        var verdictMap = {positive: ['强','#4ade80'], negative: ['弱','#f87171'], observing: ['观察','#8b949e']};
        list.innerHTML = rows.map(function(f) {
            var v = verdictMap[f.verdict] || [f.verdict||'-', '#8b949e'];
            var wr = f.win_rate || 0;
            var wrColor = wr >= 0.55 ? '#4ade80' : (wr >= 0.45 ? '#fbbf24' : '#f87171');
            return '<div style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:11px;border-bottom:1px solid #21262d">' +
                '<span style="font-weight:600;min-width:120px;color:#e1e4e8">' + (f.factor_key||'') + '</span>' +
                '<span style="color:#8b949e;min-width:34px">' + (f.total_trades||0) + '笔</span>' +
                '<span style="color:' + wrColor + ';min-width:42px">' + Math.round(wr*100) + '%</span>' +
                '<span style="font-size:10px;padding:1px 6px;border-radius:8px;background:' + v[1] + '22;color:' + v[1] + ';border:1px solid ' + v[1] + '44">' + v[0] + '</span>' +
                (f.bayesian_win_rate != null ? '<span style="color:#484f58;font-size:10px;margin-left:auto">贝叶斯' + Math.round(f.bayesian_win_rate*100) + '%</span>' : '') +
                '</div>';
        }).join('');
    }

    function renderUnifiedPositions(lifecycle, rawPositions) {
        var el = document.getElementById('positionsList');
        var countEl = document.getElementById('posCount');

        if (!lifecycle || !lifecycle.length) {
            el.innerHTML = '<div style="text-align:center;padding:16px;color:#8b949e;font-size:13px">暂无持仓</div>';
            countEl.textContent = '';
            _lifecycleData = [];
            return;
        }

        _lifecycleData = lifecycle;

        var dangerCount = lifecycle.filter(function(l){ return l.alert_level === 'danger'; }).length;
        var warnCount = lifecycle.filter(function(l){ return l.alert_level === 'warning'; }).length;
        countEl.innerHTML = '(' + lifecycle.length + ')' +
            (dangerCount > 0 ? ' <span style="color:#f87171">' + dangerCount + '⚠</span>' : '') +
            (warnCount > 0 ? ' <span style="color:#fbbf24">' + warnCount + '⚡</span>' : '');

        renderSortedList();

        // Update heatmap if visible
        if (_heatmapVisible) renderHeatmap();


    }

    function renderPositionItem(lc) {
        var sym = (lc.symbol || '').replace(/^US\./, '');
        var isUrgent = lc.alert_level === 'danger' || lc.alert_level === 'warning';
        var cardCls = lc.alert_level === 'danger' ? 'danger' : lc.alert_level === 'warning' ? 'warning' : '';
        var dirCls = lc.direction === 'LONG' ? 'long' : 'short';
        var pc = lc.pnl_pct >= 0 ? 'green' : 'red';
        var dirIcon = lc.direction === 'LONG' ? '▲' : '▼';
        var pnlStr = (lc.pnl_pct >= 0 ? '+' : '') + lc.pnl_pct.toFixed(1) + '%';

        if (!isUrgent) {
            // ── Compact mode (1 line) ──
            var holdStr = lc.hold_days != null ? 'D' + lc.hold_days : '';
            var thesisDot = '';
            var pnlUsd = lc.pnl_usd || 0;
            var pnlUsdStr = (pnlUsd >= 0 ? '+' : '-') + '$' + Math.abs(pnlUsd).toFixed(0);
            var eqPct = lc.equity_pct || 0;
            var curPrice = lc.current_price || 0;
            var entryPrice = lc.entry_price || 0;
            var priceStr = curPrice > 0 ? '$' + curPrice.toFixed(2) : '--';
            var costStr = entryPrice > 0 ? '$' + entryPrice.toFixed(2) : '';
            return '<div class="pos-compact" onclick="togglePosExpand(\'' + lc.symbol + '\')" style="cursor:pointer">' +
                '<div class="pos-compact-row">' +
                '<span class="sig-dot ' + (lc.alert_level === 'normal' ? 'ok' : '') + '"></span>' +
                '<a href="/stock/' + lc.symbol + '" onclick="event.stopPropagation()" style="color:#e1e4e8;text-decoration:none;font-weight:600;font-size:13px;min-width:48px">' + sym + '</a>' +
                '<span class="lc-dir ' + dirCls + '" style="font-size:10px;padding:1px 5px">' + dirIcon + ' ' + (lc.quantity || 0) + '</span>' +
                '<span style="font-size:12px;color:#e1e4e8;min-width:60px;text-align:right" title="现价/成本">' + priceStr + '</span>' +
                '<span class="' + pc + '" style="font-weight:600;font-size:13px;min-width:50px;text-align:right">' + pnlStr + '</span>' +
                '<span class="' + pc + '" style="font-size:11px;min-width:48px;text-align:right">' + pnlUsdStr + '</span>' +
                '<span style="font-size:11px;color:#8b949e;min-width:32px;text-align:right" title="占权益比">' + eqPct.toFixed(0) + '%</span>' +
                (holdStr ? '<span style="font-size:11px;color:#8b949e">' + holdStr + '</span>' : '') +
                thesisDot +
                '<span style="margin-left:auto;font-size:11px;color:#484f58">▶</span>' +
                '</div>' +
                '<div class="pos-expand-detail" id="posDetail_' + lc.symbol + '" style="display:none">' +
                    renderExpandedContent(lc) +
                '</div>' +
            '</div>';
        } else {
            // ── Expanded mode (full card for danger/warning) ──
            return '<div class="lc-card ' + cardCls + '">' +
                renderExpandedContent(lc) +
            '</div>';
        }
    }

    // ── Position Totals (LONG/SHORT/净敞口 合计行) ──
    function renderPositionTotals(positions, exposure) {
        var el = document.getElementById('posTotals');
        if (!el || !positions || !positions.length) { if(el) el.style.display='none'; return; }
        var longMv = 0, shortMv = 0, totalPnl = 0;
        positions.forEach(function(p) {
            var mv = (p.cur_price || p.current_price || 0) * (p.qty || p.quantity || p.size || 0);
            var pnl = p.unrealized_pnl || 0;
            if ((p.direction || '').toUpperCase() === 'SHORT') { shortMv += mv; }
            else { longMv += mv; }
            totalPnl += pnl;
        });
        var net = longMv - shortMv;
        var netCls = net >= 0 ? 'green' : 'red';
        el.style.display = 'flex';
        el.innerHTML =
            '<span>LONG <b style="color:#e1e4e8">$' + fmtK(longMv) + '</b></span>' +
            '<span>SHORT <b style="color:#e1e4e8">$' + fmtK(shortMv) + '</b></span>' +
            '<span>净 <b class="' + netCls + '">' + (net>=0?'+$':'-$') + fmtK(Math.abs(net)) + '</b></span>' +
            '<span style="margin-left:auto">浮盈 <b class="' + (totalPnl>=0?'green':'red') + '">' + (totalPnl>=0?'+$':'-$') + fmt(Math.abs(totalPnl)) + '</b></span>';
    }

    // ── Action Zone (仅在有行动项时出现) ──
    function renderActionZone(lifecycle, earnings) {
        var el = document.getElementById('actionZone');
        var list = document.getElementById('actionList');
        if (!el || !list) return;
        var actions = [];

        // 1. Protection triggered alerts
        (lifecycle || []).forEach(function(lc) {
            var sym = (lc.symbol || '').replace(/^US\./, '');
            if (lc.stop_loss_triggered) {
                actions.push({level:'danger', icon:'🔴', text: sym + ' 已触发止损 $' + (lc.stop_loss||0).toFixed(2), action:'平仓', href:'/trade?symbol='+lc.symbol+'&op=CLOSE'});
            }
            if (lc.trailing_stop_triggered) {
                actions.push({level:'warning', icon:'🟢', text: sym + ' 已触发追踪止盈 $' + (lc.trailing_stop_price||0).toFixed(2), action:'确认平仓', href:'/trade?symbol='+lc.symbol+'&op=CLOSE'});
            }
            // Near stop-loss warning (< 2%)
            if (lc.sl_distance_pct != null && lc.sl_distance_pct >= 0 && lc.sl_distance_pct < 2 && !lc.stop_loss_triggered) {
                actions.push({level:'warning', icon:'⚠️', text: sym + ' 距止损仅 ' + lc.sl_distance_pct.toFixed(1) + '%', action:null});
            }
        });

        // 2. Earnings alerts for held positions
        var eToday = (earnings && earnings.today) || [];
        eToday.forEach(function(e) {
            actions.push({level:'warning', icon:'📊', text: (e.symbol||'').replace('US.','') + ' 今日盘后财报', action:null});
        });

        if (!actions.length) { el.style.display = 'none'; return; }

        el.style.display = 'block';
        list.innerHTML = actions.map(function(a) {
            var bg = a.level === 'danger' ? 'rgba(248,113,113,0.08)' : 'rgba(251,191,36,0.06)';
            var border = a.level === 'danger' ? 'rgba(248,113,113,0.3)' : 'rgba(251,191,36,0.3)';
            var actionHtml = a.action && a.href ? '<a href="'+a.href+'" style="margin-left:auto;font-size:11px;padding:2px 8px;background:#3a1a1a;color:#f87171;border:1px solid rgba(248,113,113,0.3);border-radius:4px;text-decoration:none;white-space:nowrap">'+a.action+'</a>' : '';
            return '<div style="display:flex;align-items:center;gap:8px;padding:6px 14px;background:'+bg+';border:1px solid '+border+';border-radius:6px;margin-bottom:4px;font-size:12px">' +
                '<span>'+a.icon+'</span>' +
                '<span style="color:#e1e4e8">'+a.text+'</span>' +
                actionHtml +
                '</div>';
        }).join('');
    }

    function renderExpandedContent(lc) {
        var sym = (lc.symbol || '').replace(/^US\./, '');
        var dirCls = lc.direction === 'LONG' ? 'long' : 'short';
        var pc = lc.pnl_pct >= 0 ? 'green' : 'red';
        var pnlStr = (lc.pnl_pct >= 0 ? '+' : '') + lc.pnl_pct.toFixed(1) + '%';

        // Header
        var pnlUsd = lc.pnl_usd || 0;
        var pnlUsdStr = (pnlUsd >= 0 ? '+' : '-') + '$' + Math.abs(pnlUsd).toFixed(0);
        var eqPct = lc.equity_pct || 0;
        var html = '<div class="lc-header">' +
            '<div style="display:flex;align-items:center;gap:8px">' +
            '<a href="/stock/' + lc.symbol + '" class="lc-sym">' + sym + '</a>' +
            '<span class="lc-dir ' + dirCls + '">' + (lc.direction === 'LONG' ? '▲ LONG' : '▼ SHORT') + ' ' + (lc.quantity || 0) + '</span>' +
            '<span style="font-size:11px;color:#8b949e">' + eqPct.toFixed(0) + '%权益</span>' +
            '</div>' +
            '<div style="text-align:right">' +
            '<span class="lc-pnl ' + pc + '">' + pnlStr + '</span>' +
            '<span class="' + pc + '" style="font-size:12px;margin-left:6px">' + pnlUsdStr + '</span>' +
            '</div>' +
            '</div>';

        // Meta line
        var holdHtml = '';
        if (lc.hold_days !== null && lc.hold_days !== undefined) {
            var holdCls = lc.hold_days > (lc.expected_hold_days || 99) ? 'hold-warn' : 'hold-ok';
            holdHtml = '<span class="' + holdCls + '">Day ' + lc.hold_days + '/' + (lc.expected_hold_days || '?') + '</span>';
        }
        html += '<div class="lc-meta">' + holdHtml + '</div>';

        // Recommendations (only for danger/warning)
        var recs = lc.recommendations || [];
        if (recs.length) {
            html += '<ul class="lc-recs">';
            recs.slice(0, 3).forEach(function(r) { html += '<li>' + r + '</li>'; });
            html += '</ul>';
        }

        // SL + TP with progress bar
        var sl = lc.stop_loss || 0;
        var tp = lc.take_profit || 0;
        var curPrice = lc.current_price || 0;
        var entryPrice = lc.entry_price || 0;
        if (sl > 0 && curPrice > 0 && entryPrice > 0) {
            var slDist = lc.sl_distance_pct || 0;
            // Progress bar: entry → current → SL (LONG: SL below, SHORT: SL above)
            var barPct = 0;
            if (lc.direction === 'LONG') {
                var range = entryPrice - sl;
                barPct = range > 0 ? Math.max(0, Math.min(100, (curPrice - sl) / range * 100)) : 50;
            } else {
                var range = sl - entryPrice;
                barPct = range > 0 ? Math.max(0, Math.min(100, (sl - curPrice) / range * 100)) : 50;
            }
            var barColor = barPct > 50 ? '#4ade80' : barPct > 20 ? '#fbbf24' : '#f87171';
            if (slDist < 0) {
                html += '<div class="lc-sl"><span class="danger-text">⚠ 已跌破止损 $' + sl.toFixed(2) + '</span></div>';
            } else {
                var slCls = slDist < 2 ? 'danger-text' : slDist < 5 ? 'warn-text' : '';
                var estTag = lc.atr_estimated ? ' <span style="font-size:9px;color:#fbbf24" title="ATR缓存未命中，使用估算值">≈</span>' : '';
                html += '<div style="margin:6px 0 2px">' +
                    '<div style="display:flex;justify-content:space-between;font-size:11px;color:#8b949e;margin-bottom:2px">' +
                    '<span>SL $' + sl.toFixed(2) + estTag + ' <span class="' + slCls + '">(' + slDist.toFixed(1) + '%)</span></span>';
                if (tp > 0) {
                    var tpDist = lc.tp_distance_pct || 0;
                    html += '<span>TP $' + tp.toFixed(2) + ' (' + (tpDist >= 0 ? '+' : '') + tpDist.toFixed(1) + '%)</span>';
                }
                html += '</div>' +
                    '<div style="height:4px;background:#21262d;border-radius:2px;overflow:hidden">' +
                    '<div style="height:100%;width:' + barPct.toFixed(0) + '%;background:' + barColor + ';border-radius:2px;transition:width 0.3s"></div>' +
                    '</div></div>';
            }
        }

        // Action buttons
        html += '<div class="lc-actions">' +
            '<a href="/trade?symbol=' + lc.symbol + '&op=CLOSE" class="btn" style="background:#3a1a1a;color:#f87171;border:1px solid rgba(248,113,113,0.3)">平仓</a>' +
            '<a href="/trade?symbol=' + lc.symbol + '" class="btn" style="background:#161b22;color:#8b949e;border:1px solid #30363d">交易</a>' +
            '</div>';

        return html;
    }

    // ── Thesis Challenge ──
    async function loadChallenge(symbol, btn) {
        var statusEl = document.getElementById('challengeStatus_' + symbol);
        var resultEl = document.getElementById('challenge_' + symbol);
        if (!resultEl) return;

        // Show if already rendered (cached)
        if (resultEl.style.display !== 'none' && resultEl.innerHTML) {
            resultEl.style.display = resultEl.style.display === 'none' ? 'block' : 'none';
            return;
        }

        if (btn) { btn.disabled = true; btn.textContent = '⏳ 审视中...'; }
        if (statusEl) statusEl.textContent = '调用LLM中...';

        try {
            // First check cache
            var cached = await fetch('/api/positions/' + encodeURIComponent(symbol) + '/challenge');
            var cacheData = await cached.json();
            if (cacheData && cacheData.verdict) {
                renderChallenge(symbol, cacheData);
                if (btn) { btn.textContent = '🔍 AI审视'; btn.disabled = false; }
                return;
            }

            // Call LLM
            var res = await fetch('/api/positions/' + encodeURIComponent(symbol) + '/challenge', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({})
            });
            var data = await res.json();
            if (data.error) {
                if (statusEl) statusEl.textContent = '❌ ' + data.error;
                if (btn) { btn.textContent = '🔍 AI审视'; btn.disabled = false; }
                return;
            }
            renderChallenge(symbol, data);
            if (statusEl) statusEl.textContent = data.cached ? '(缓存)' : '(新)';
        } catch (e) {
            if (statusEl) statusEl.textContent = '❌ 网络错误';
        }
        if (btn) { btn.textContent = '🔍 AI审视'; btn.disabled = false; }
    }

    function renderChallenge(symbol, data) {
        var el = document.getElementById('challenge_' + symbol);
        if (!el) return;
        var isValid = data.verdict === 'VALID';
        var cls = isValid ? 'valid' : 'invalid';
        var icon = isValid ? '✅' : '❌';
        var verdictText = isValid ? '理由成立' : '理由不成立';
        var confColor = data.confidence >= 70 ? '#4ade80' : data.confidence >= 40 ? '#fbbf24' : '#f87171';

        el.className = 'challenge-result ' + cls;
        el.style.display = 'block';

        var html = '<div class="challenge-verdict">' + icon + ' <span style="color:' + (isValid ? '#4ade80' : '#f87171') + '">' + verdictText + '</span>' +
            '<span class="challenge-conf" style="margin-left:auto">置信度 <span style="color:' + confColor + ';font-weight:700">' + data.confidence + '%</span></span>' +
            '</div>';
        
        if (data.weakness) html += '<div class="challenge-weakness">⚠ ' + escHtml(data.weakness) + '</div>';
        if (data.fatal_if) html += '<div class="challenge-fatal">🎯 ' + escHtml(data.fatal_if) + '</div>';

        // Check results dimension pills
        var cr = data.check_results || {};
        if (cr.technical || cr.sector || cr.thesis_quality) {
            var pills = [];
            var dimLabels = {technical:'技术面', sector:'板块', risk_reward:'风险收益', history:'历史', thesis_quality:'理由质量'};
            var dimIcons = {technical:'📊', sector:'🏭', risk_reward:'⚖️', history:'📜', thesis_quality:'📝'};
            var colorMap = {
                'SUPPORT':'color:#4ade80', 'AGAINST':'color:#f87171', 'NEUTRAL':'color:#8b949e',
                'OK':'color:#4ade80', 'POOR':'color:#f87171',
                'POSITIVE':'color:#4ade80', 'NEGATIVE':'color:#f87171', 'NONE':'color:#484f58',
                'STRONG':'color:#4ade80', 'WEAK':'color:#fbbf24', 'EMPTY':'color:#f87171'
            };
            for (var dim in cr) {
                var val = cr[dim];
                var label = dimLabels[dim] || dim;
                var iconD = dimIcons[dim] || '•';
                var style = colorMap[val] || 'color:#8b949e';
                pills.push('<span style="font-size:10px;' + style + ';padding:1px 6px;background:#161b22;border:1px solid #30363d;border-radius:3px;white-space:nowrap">' + iconD + ' ' + label + ': ' + val + '</span>');
            }
            if (pills.length) html += '<div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:6px">' + pills.join('') + '</div>';
        }

        html += '<div class="challenge-conf" style="margin-top:4px">' + (data.cached ? '缓存' : '🧠 ' + (data.model_used || '')) + ' · ' + (data.created_at || '').substring(5, 16) + '</div>';

        el.innerHTML = html;
    }

    function togglePosExpand(symbol) {
        var el = document.getElementById('posDetail_' + symbol);
        if (el) {
            el.style.display = el.style.display === 'none' ? 'block' : 'none';
        }
    }

    // ══════════════════════════════════════
    //  Thesis inline editor
    // ══════════════════════════════════════
    function editThesis(symbol, el) {
        var current = el.classList.contains('empty') ? '' : el.textContent;
        el.outerHTML = '<div id="thesisEdit_' + symbol.replace(/\./g,'_') + '">' +
            '<textarea class="thesis-edit-area" id="thesisText_' + symbol.replace(/\./g,'_') + '" placeholder="为什么买这个？一句话够了">' + current + '</textarea>' +
            '<div class="thesis-edit-row">' +
                '<label>失效价</label><input type="number" step="0.01" id="thesisInv_' + symbol.replace(/\./g,'_') + '" placeholder="0.00">' +
                '<label>预期天数</label><input type="number" id="thesisDays_' + symbol.replace(/\./g,'_') + '" value="5" style="width:50px">' +
                '<button class="btn" onclick="saveThesis(\'' + symbol + '\')" style="background:#238636;border:none;color:#fff;font-size:11px;padding:4px 12px;min-height:auto;margin-left:auto">保存</button>' +
                '<button class="btn" onclick="cancelThesis(\'' + symbol + '\')" style="background:#21262d;border:1px solid #30363d;color:#8b949e;font-size:11px;padding:4px 12px;min-height:auto">取消</button>' +
            '</div></div>';
        document.getElementById('thesisText_' + symbol.replace(/\./g,'_')).focus();
    }

    async function saveThesis(symbol) {
        var suf = symbol.replace(/\./g,'_');
        var thesis = document.getElementById('thesisText_' + suf).value.trim();
        var inv = parseFloat(document.getElementById('thesisInv_' + suf).value) || 0;
        var days = parseInt(document.getElementById('thesisDays_' + suf).value) || 5;

        try {
            var resp = await fetch('/api/positions/' + encodeURIComponent(symbol) + '/thesis', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({thesis: thesis, invalidation_price: inv, expected_hold_days: days})
            });
            var r = await resp.json();
            if (r.status === 'ok') {
                loadSnapshot(); // refresh
            } else {
                alert('保存失败: ' + (r.error || 'unknown'));
            }
        } catch(e) {
            alert('保存失败: ' + e.message);
        }
    }

    function cancelThesis(symbol) {
        loadSnapshot(); // just refresh to restore original
    }

    // ══════════════════════════════════════
    //  Protection API (止损/追踪止盈)
    // ══════════════════════════════════════
    async function setStopLoss(symbol, price) {
        var p = parseFloat(price);
        if (!p || p <= 0) return;
        try {
            var resp = await fetch('/api/protection/stoploss/' + encodeURIComponent(symbol), {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({price: p})
            });
            var r = await resp.json();
            if (r.success) loadSnapshot();
        } catch(e) { console.error('setStopLoss failed:', e); }
    }

    async function confirmProtection(symbol, reason) {
        if (!confirm('确认平仓 ' + symbol.replace('US.','') + '？\n理由: ' + reason)) return;
        try {
            var resp = await fetch('/api/protection/confirm/' + encodeURIComponent(symbol), {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({reason: reason})
            });
            var r = await resp.json();
            if (r.success) {
                loadSnapshot();
            } else {
                alert('平仓失败: ' + (r.error || 'unknown'));
            }
        } catch(e) { alert('平仓失败: ' + e.message); }
    }

    async function cancelProtection(symbol) {
        try {
            var resp = await fetch('/api/protection/cancel/' + encodeURIComponent(symbol), {
                method: 'POST'
            });
            var r = await resp.json();
            if (r.success) loadSnapshot();
        } catch(e) { console.error('cancelProtection failed:', e); }
    }

    // ══════════════════════════════════════
    //  Position Data (供 attention list 使用, UI 在交易页)
    // ══════════════════════════════════════
    var _posData = [];
    var _totalMv = 0;

    // ══════════════════════════════════════
    //  Market Status
    // ══════════════════════════════════════
    function updateMarketStatus() {
        var now = new Date();
        var et = new Date(now.toLocaleString('en-US', {timeZone: 'America/New_York'}));
        var t = et.getHours() * 60 + et.getMinutes();
        var badge = document.getElementById('marketBadge');
        var badge2 = document.getElementById('marketBadge2');
        if (t >= 570 && t < 960) {
            var cls = 'market-badge open', txt = '🟢 盘中';
        } else if (t >= 240 && t < 570) {
            var cls = 'market-badge pre', txt = '🟡 盘前';
        } else if (t >= 960 && t < 1200) {
            var cls = 'market-badge after', txt = '🟠 盘后';
        } else {
            var cls = 'market-badge closed', txt = '🔴 休市';
        }
        if (badge) { badge.className = cls; badge.textContent = txt; }
        if (badge2) { badge2.className = cls; badge2.textContent = txt; }
    }

    // ══════════════════════════════════════
    //  Analytics Tab Loaders (2 tabs)
    // ══════════════════════════════════════

    // -- Equity Curve --
    var _equityChart = null;
    async function loadEquity() {
        try {
            var d = await apiFetch('/api/analytics/equity');
            var history = d.history || [];
            
            // Stats bar
            var statsEl = document.getElementById('equityStats');
            if (statsEl) {
                var ce = d.current_equity || 0;
                var pe = d.peak_equity || 0;
                var dd = d.current_drawdown_pct || 0;
                var mdd = d.max_drawdown_pct || 0;
                var bp = d.buying_power || 0;
                var ddCls = Math.abs(dd) < 1 ? 'color:#4ade80' : Math.abs(dd) < 3 ? 'color:#fbbf24' : 'color:#f87171';
                var mddCls = Math.abs(mdd) < 1 ? 'color:#4ade80' : Math.abs(mdd) < 3 ? 'color:#fbbf24' : 'color:#f87171';
                statsEl.innerHTML = 
                    '<span>当前净值 <b style="color:#e1e4e8">$' + ce.toLocaleString('en-US', {minimumFractionDigits:0, maximumFractionDigits:0}) + '</b> USD</span>' +
                    '<span>峰值 <b style="color:#e1e4e8">$' + pe.toLocaleString('en-US', {minimumFractionDigits:0, maximumFractionDigits:0}) + '</b></span>' +
                    '<span style="' + ddCls + '">回撤 ' + dd.toFixed(2) + '%</span>' +
                    '<span style="' + mddCls + '">最大回撤 ' + mdd.toFixed(2) + '%</span>' +
                    '<span>购买力 <b style="color:#e1e4e8">$' + bp.toLocaleString('en-US', {minimumFractionDigits:0, maximumFractionDigits:0}) + '</b></span>';
            }
            
            if (!history.length || typeof Chart === 'undefined') return;

            var ctx = document.getElementById('equityChart');
            if (!ctx) return;
            if (_equityChart) _equityChart.destroy();

            var labels = history.map(function(h){ return (h.date || '').substring(5); });
            var equity = history.map(function(h){ return h.total_assets || h.equity || 0; });
            var drawdowns = history.map(function(h){ return h.drawdown_pct || 0; });
            var ddColors = drawdowns.map(function(d){ return Math.abs(d) < 1 ? '#4ade80' : Math.abs(d) < 3 ? '#fbbf24' : '#f87171'; });

            _equityChart = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: labels,
                    datasets: [
                        { type: 'line', label: '净值', data: equity, borderColor: '#58a6ff', backgroundColor: 'transparent', borderWidth: 2, pointRadius: 0, yAxisID: 'y', tension: 0.3, order: 0 },
                        { type: 'bar', label: '回撤%', data: drawdowns, backgroundColor: ddColors, yAxisID: 'y1', order: 1 }
                    ]
                },
                options: {
                    responsive: true, maintainAspectRatio: false,
                    plugins: { legend: { display: false } },
                    scales: {
                        x: { ticks: { color: '#8b949e', font: { size: 10 } }, grid: { color: '#21262d' } },
                        y: { position: 'left', ticks: { color: '#58a6ff', font: { size: 10 }, callback: function(v){ return '$' + (v/1000).toFixed(0) + 'K'; } }, grid: { color: '#21262d' } },
                        y1: { position: 'right', ticks: { color: '#f87171', font: { size: 10 }, callback: function(v){ return v.toFixed(1) + '%'; } }, grid: { display: false } }
                    }
                }
            });
        } catch (e) { /* silent */ }
    }

    // -- Decision Review (merged: perf + signals + recent trades) --
    async function loadReview() {
        try {
            var d = await apiFetch('/api/analytics/manual-performance');
            var manual = d.manual || {};
            var auto = d.auto || {};
            var recent = d.recent_trades || [];
            
            var html = '<div class="mini-metrics">';
            html += '<div class="mm"><div class="l">手动交易</div><div class="v">' + (manual.count || 0) + '</div></div>';
            html += '<div class="mm"><div class="l">自动交易</div><div class="v">' + (auto.count || 0) + '</div></div>';
            html += '<div class="mm"><div class="l">手动胜率</div><div class="v ' + ((manual.win_rate||0)>=50?'green':'red') + '">' + (manual.win_rate||0).toFixed(0) + '%</div></div>';
            html += '<div class="mm"><div class="l">自动胜率</div><div class="v ' + ((auto.win_rate||0)>=50?'green':'red') + '">' + (auto.win_rate||0).toFixed(0) + '%</div></div>';
            html += '<div class="mm"><div class="l">总盈亏</div><div class="v ' + (((manual.total_pnl||0)+(auto.total_pnl||0))>=0?'green':'red') + '">$' + ((manual.total_pnl||0)+(auto.total_pnl||0)).toFixed(0) + '</div></div>';
            html += '<div class="mm"><div class="l">利润因子</div><div class="v">' + ((manual.profit_factor||0) > 0 ? (manual.profit_factor).toFixed(1) : '--') + '</div></div>';
            html += '</div>';
            
            // Signal IC summary
            try {
                var sigData = await apiFetch('/api/analytics/signal-quality');
                var ic = sigData.ic_summary || {};
                var ic30 = parseFloat(ic.ic_30d || 0);
                var sigCount = ic.total_signals || 0;
                var icLabel = ic30 >= 0.05 ? '✅ 有预测力' : ic30 >= 0.02 ? '⚠️ 弱' : '❌ 无预测力(噪音)';
                html += '<div style="font-size:12px;color:#8b949e;padding:8px 0;border-bottom:1px solid #21262d">';
                html += '<b style="color:#e1e4e8">信号质量:</b> IC=' + ic30.toFixed(3) + ' (' + sigCount + '个信号) — ' + icLabel;
                html += '</div>';
            } catch(e) {}
            
            // Recent trades (all sources)
            if (recent.length) {
                html += '<div style="font-size:12px;color:#8b949e;padding:8px 0 4px">最近交易 (' + recent.length + ' 笔):</div>';
                html += '<div class="tw"><table style="font-size:12px;width:100%"><thead><tr>';
                html += '<th style="text-align:left">标的</th><th>方向</th><th>盈亏</th><th>来源</th><th>时间</th>';
                html += '</tr></thead><tbody>';
                recent.slice(0, 8).forEach(function(t) {
                    var pnlCls = (t.pnl || 0) >= 0 ? 'green' : 'red';
                    var dir = t.direction === 'LONG' ? '<span style="color:#4ade80">▲</span>' : '<span style="color:#f87171">▼</span>';
                    var dateStr = (t.date || '').substring(0, 10);
                    html += '<tr><td style="color:#e1e4e8">' + escHtml((t.symbol||'').replace('US.','')) + '</td>';
                    html += '<td>' + dir + '</td>';
                    html += '<td class="' + pnlCls + '">$' + (t.pnl || 0).toFixed(2) + '</td>';
                    html += '<td>' + _srcLabel(t.source_type) + '</td>';
                    html += '<td style="color:#8b949e">' + dateStr + '</td></tr>';
                });
                html += '</tbody></table></div>';
            } else {
                html += '<div style="text-align:center;color:#484f58;padding:16px;font-size:12px">暂无交易记录</div>';
            }
            
            document.getElementById('reviewContent').innerHTML = html;
        } catch (e) {
            document.getElementById('reviewContent').innerHTML = '<div style="color:#f87171;font-size:12px">加载失败</div>';
        }
    }

    // -- Trade Review (深度复盘) --
    async function loadTradeReview(days, btn) {
        // Update filter button active state
        if (btn) {
            document.querySelectorAll('#tab-tradeReview .tr-filter-btn').forEach(function(b) {
                b.classList.toggle('active', b === btn);
            });
        } else {
            // Default to 7 days on first load
            days = 7;
        }
        try {
            var d = await apiFetch('/api/trade/review?days=' + (days || 0));
            var tradeCount = d.trade_count || 0;
            var tcEl = document.getElementById('trTradeCount');
            if (tcEl) tcEl.textContent = '共 ' + tradeCount + ' 笔交易';

            // ── Card 1: Dimension Heatmap ──
            var dimStats = d.dimension_stats || {};
            var dimNames = ['direction', 'entry', 'exit', 'duration', 'efficiency'];
            var dimLabels = { direction: '方向判断', entry: '入场时机', exit: '离场时机', duration: '持仓时长', efficiency: '资金效率' };
            var hmHtml = '';
            if (tradeCount > 0) {
                dimNames.forEach(function(dim) {
                    var s = dimStats[dim] || { pass: 0, warn: 0, fail: 0, accuracy: 0, avg_score: 0 };
                    var acc = s.accuracy || 0;
                    var barColor = acc >= 70 ? '#4ade80' : acc >= 40 ? '#fbbf24' : '#f87171';
                    hmHtml += '<div class="tr-dim-row">';
                    hmHtml += '<div class="tr-dim-label">' + (dimLabels[dim] || dim) + '</div>';
                    hmHtml += '<div class="tr-dim-bars">';
                    hmHtml += '<span class="tr-dim-pass">✓ ' + (s.pass || 0) + '</span>';
                    hmHtml += '<span class="tr-dim-warn">⚠ ' + (s.warn || 0) + '</span>';
                    hmHtml += '<span class="tr-dim-fail">✗ ' + (s.fail || 0) + '</span>';
                    hmHtml += '</div>';
                    hmHtml += '<div class="tr-dim-bar-track"><div class="tr-dim-bar-fill" style="width:' + acc + '%;background:' + barColor + '"></div></div>';
                    hmHtml += '<div class="tr-dim-pct" style="color:' + barColor + '">' + acc.toFixed(0) + '%</div>';
                    hmHtml += '</div>';
                });
            } else {
                hmHtml = '<div class="tr-empty">暂无交易数据</div>';
            }
            document.getElementById('trHeatmap').innerHTML = hmHtml;

            // ── Card 2: Plan vs Unplanned ──
            var pv = d.plan_vs_unplanned || {};
            var planned = pv.planned || {};
            var unplanned = pv.unplanned || {};
            var pvHtml = '<div class="tr-compare">';
            pvHtml += '<div class="tr-compare-col"><div class="tr-compare-header">📝 计划内</div>';
            pvHtml += '<div class="tr-compare-item"><span>笔数</span><b>' + (planned.count || 0) + '</b></div>';
            pvHtml += '<div class="tr-compare-item"><span>胜率</span><b class="' + ((planned.win_rate || 0) >= 50 ? 'green' : 'red') + '">' + (planned.win_rate || 0).toFixed(0) + '%</b></div>';
            pvHtml += '<div class="tr-compare-item"><span>均盈亏</span><b class="' + ((planned.avg_pnl || 0) >= 0 ? 'green' : 'red') + '">$' + (planned.avg_pnl || 0).toFixed(2) + '</b></div>';
            pvHtml += '<div class="tr-compare-item"><span>利润因子</span><b>' + ((planned.profit_factor || 0) > 0 ? (planned.profit_factor).toFixed(1) : '--') + '</b></div>';
            pvHtml += '</div>';
            pvHtml += '<div class="tr-compare-col"><div class="tr-compare-header" style="color:#fbbf24">⚡ 非计划</div>';
            pvHtml += '<div class="tr-compare-item"><span>笔数</span><b>' + (unplanned.count || 0) + '</b></div>';
            pvHtml += '<div class="tr-compare-item"><span>胜率</span><b class="' + ((unplanned.win_rate || 0) >= 50 ? 'green' : 'red') + '">' + (unplanned.win_rate || 0).toFixed(0) + '%</b></div>';
            pvHtml += '<div class="tr-compare-item"><span>均盈亏</span><b class="' + ((unplanned.avg_pnl || 0) >= 0 ? 'green' : 'red') + '">$' + (unplanned.avg_pnl || 0).toFixed(2) + '</b></div>';
            pvHtml += '<div class="tr-compare-item"><span>利润因子</span><b>' + ((unplanned.profit_factor || 0) > 0 ? (unplanned.profit_factor).toFixed(1) : '--') + '</b></div>';
            pvHtml += '</div></div>';
            document.getElementById('trPlanVs').innerHTML = pvHtml;

            // ── Card 3: Trend Indicators ──
            var trend = d.trend || {};
            var dq = trend.decision_quality || {};
            var pf = trend.profit_factor || {};
            var tt = trend.total_trades || {};
            var trendHtml = '';
            var arrows = { up: '▲', down: '▼', neutral: '─' };
            var arrowColors = { up: '#4ade80', down: '#f87171', neutral: '#8b949e' };

            trendHtml += '<div class="tr-trend-row">';
            trendHtml += '<span class="tr-trend-label">决策质量</span>';
            trendHtml += '<span class="tr-trend-cur">' + (dq.current || 0) + '</span>';
            trendHtml += '<span class="tr-trend-arrow" style="color:' + (arrowColors[dq.direction] || '#8b949e') + '">' + (arrows[dq.direction] || '─') + ' ' + (dq.delta > 0 ? '+' : '') + (dq.delta || 0).toFixed(1) + '</span>';
            trendHtml += '</div>';

            trendHtml += '<div class="tr-trend-row">';
            trendHtml += '<span class="tr-trend-label">利润因子</span>';
            trendHtml += '<span class="tr-trend-cur">' + (pf.current || 0).toFixed(2) + '</span>';
            trendHtml += '<span class="tr-trend-arrow" style="color:' + (arrowColors[pf.direction] || '#8b949e') + '">' + (arrows[pf.direction] || '─') + ' ' + (pf.delta > 0 ? '+' : '') + (pf.delta || 0).toFixed(2) + '</span>';
            trendHtml += '</div>';

            trendHtml += '<div class="tr-trend-row">';
            trendHtml += '<span class="tr-trend-label">交易笔数</span>';
            trendHtml += '<span class="tr-trend-cur">' + (tt.current || 0) + '</span>';
            trendHtml += '<span class="tr-trend-arrow" style="color:#8b949e">前期: ' + (tt.previous || 0) + '</span>';
            trendHtml += '</div>';

            document.getElementById('trTrend').innerHTML = trendHtml;

            // ── Card 4: Weakest Dimension ──
            var weakest = d.weakest_dimension || 'entry';
            var wLabel = dimLabels[weakest] || weakest;
            var wStats = dimStats[weakest] || {};
            var wAcc = wStats.accuracy || 0;
            var wColor = wAcc >= 70 ? '#4ade80' : wAcc >= 40 ? '#fbbf24' : '#f87171';
            var wHtml = '<div class="tr-weakest-callout" style="border-color:' + wColor + '">';
            wHtml += '<div class="tr-weakest-name" style="color:' + wColor + '">' + wLabel + '</div>';
            wHtml += '<div class="tr-weakest-acc">准确率: <b style="color:' + wColor + '">' + wAcc.toFixed(0) + '%</b></div>';
            wHtml += '<div class="tr-weakest-detail">通过: ' + (wStats.pass || 0) + ' | 警告: ' + (wStats.warn || 0) + ' | 失败: ' + (wStats.fail || 0) + '</div>';
            wHtml += '<div class="tr-weakest-hint">💡 建议优先提升该维度的表现</div>';
            wHtml += '</div>';
            document.getElementById('trWeakest').innerHTML = wHtml;

            // ── Card 5: Behavioral Diagnostics ──
            var behEl = document.getElementById('trBehavioral');
            var beh = d.behavioral || {};
            if (beh.available) {
                var sevLabel = function(sev) {
                    if (sev === 'low') return '<span style="color:#4ade80">✅ 低</span>';
                    if (sev === 'medium') return '<span style="color:#fbbf24">⚠️ 中等</span>';
                    if (sev === 'high') return '<span style="color:#f87171">❌ 高</span>';
                    return '<span style="color:#8b949e">—</span>';
                };
                var bHtml = '';
                if (beh.disposition) bHtml += '<div style="display:flex;justify-content:space-between;padding:4px 0;font-size:12px"><span>处置效应</span>' + sevLabel(beh.disposition.severity) + ' <span style="color:#8b949e;font-size:11px">' + escHtml(beh.disposition.detail || beh.disposition.interpretation || '') + '</span></div>';
                if (beh.anchoring) bHtml += '<div style="display:flex;justify-content:space-between;padding:4px 0;font-size:12px"><span>锚定效应</span>' + sevLabel(beh.anchoring.severity) + ' <span style="color:#8b949e;font-size:11px">' + escHtml(beh.anchoring.detail || beh.anchoring.interpretation || '') + '</span></div>';
                if (beh.chasing) bHtml += '<div style="display:flex;justify-content:space-between;padding:4px 0;font-size:12px"><span>追涨行为</span>' + sevLabel(beh.chasing.severity) + ' <span style="color:#8b949e;font-size:11px">' + escHtml(beh.chasing.detail || beh.chasing.interpretation || '') + '</span></div>';
                var ff = beh.five_factor || {};
                if (ff.noise_pnl !== undefined) {
                    var pnlSpan = function(v) { var c = v >= 0 ? '#4ade80' : '#f87171'; return '<span style="color:'+c+'">'+(v>=0?'+$':'-$')+Math.abs(v).toFixed(0)+'</span>'; };
                    bHtml += '<div style="border-top:1px solid #21262d;margin-top:6px;padding-top:6px;font-size:11px;color:#8b949e">五因素归因</div>';
                    bHtml += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:2px 12px;font-size:12px">';
                    bHtml += '<div>噪音交易 ' + pnlSpan(ff.noise_pnl||0) + '</div>';
                    bHtml += '<div>过早离场 ' + pnlSpan(ff.early_exit_pnl||0) + '</div>';
                    bHtml += '<div>过晚离场 ' + pnlSpan(ff.late_exit_pnl||0) + '</div>';
                    bHtml += '<div>过度交易 ' + pnlSpan(ff.overtrading_pnl||0) + '</div>';
                    bHtml += '<div>错失信号 ' + pnlSpan(ff.missed_pnl||0) + '</div>';
                    bHtml += '</div>';
                }
                behEl.innerHTML = bHtml;
            } else {
                behEl.innerHTML = '<div style="color:#8b949e;font-size:12px;padding:12px 0">需≥5笔交易生成行为评分</div>';
            }

            // ── Card 6: Attribution Cards ──
            var attrEl = document.getElementById('trAttribution');
            var cards = d.attribution_cards || [];
            if (cards.length) {
                var aHtml = '';
                for (var ci = 0; ci < cards.length; ci++) {
                    var c = cards[ci];
                    var pc = c.pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
                    var dirIcon = c.direction === 'LONG' ? '📈' : '📉';
                    aHtml += '<div style="border-bottom:1px solid #21262d;padding:8px 0">';
                    aHtml += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">';
                    aHtml += '<span style="font-weight:700">' + dirIcon + ' ' + escHtml(c.symbol) + ' <span style="font-size:11px;color:#8b949e">' + c.direction + '</span></span>';
                    aHtml += '<span class="' + pc + '">' + (c.pnl>=0?'+$':'-$') + Math.abs(c.pnl).toFixed(2) + '</span>';
                    aHtml += '</div>';
                    aHtml += '<div style="font-size:11px;color:#8b949e;margin-bottom:4px">$' + c.entry_price + ' → $' + c.exit_price + ' · ' + c.quantity + '股 · ' + escHtml(c.date) + '</div>';
                    if (c.dimensions && c.dimensions.length) {
                        aHtml += '<div style="display:flex;gap:6px;flex-wrap:wrap">';
                        for (var di = 0; di < c.dimensions.length; di++) {
                            var dim = c.dimensions[di];
                            var dCls = dim.verdict === '✅' ? '#4ade80' : dim.verdict === '⚠️' ? '#fbbf24' : dim.verdict === '❌' ? '#f87171' : '#8b949e';
                            aHtml += '<span style="font-size:11px;padding:1px 6px;border-radius:3px;background:rgba(255,255,255,0.04);color:' + dCls + '">' + dim.verdict + dim.label + '</span>';
                        }
                        aHtml += '<span style="margin-left:auto;font-size:11px;font-weight:600;color:#e1e4e8">' + escHtml(c.grade) + ' ' + c.score + '分</span>';
                        aHtml += '</div>';
                    }
                    aHtml += '</div>';
                }
                attrEl.innerHTML = aHtml;
            } else {
                attrEl.innerHTML = '<div style="color:#8b949e;font-size:12px;padding:12px 0">暂无已平仓归因记录</div>';
            }
        } catch (e) {
            document.getElementById('trHeatmap').innerHTML = '<div class="tr-empty" style="color:#f87171">加载失败</div>';
            document.getElementById('trPlanVs').innerHTML = '';
            document.getElementById('trTrend').innerHTML = '';
            document.getElementById('trWeakest').innerHTML = '';
            document.getElementById('trBehavioral').innerHTML = '';
            document.getElementById('trAttribution').innerHTML = '';
        }
    }

    // ══════════════════════════════════════
    //  Init + Visibility-Aware Polling
    // ══════════════════════════════════════

    var _pollTimer = null;
    var _isPolling = false;

    function loadAll() {
        // WS 10s 内推送过 → 跳过 HTTP poll (safety net only)
        if (window.WSManager && WSManager.isConnected() && (Date.now() - _wsLastPush < 10000)) {
            return;
        }
        loadSnapshot();
    }

    function _getPollInterval() {
        // WS 在线时降低 polling 频率 (60s safety net), 断线恢复 30s
        return (window.WSManager && WSManager.isConnected()) ? 60000 : 30000;
    }

    function startPolling() {
        if (_isPolling) return;
        _isPolling = true;
        loadAll();  // immediate first load
        _pollTimer = setInterval(function() {
            // 动态调整间隔: WS 在线 60s, 离线 30s
            var desired = _getPollInterval();
            if (_pollTimer && _currentInterval !== desired) {
                clearInterval(_pollTimer);
                _currentInterval = desired;
                _pollTimer = setInterval(arguments.callee, desired);
            }
            loadAll();
        }, 30000);
        var _currentInterval = 30000;
    }

    function stopPolling() {
        _isPolling = false;
        if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
    }

    // Page Visibility API — tab 不可见时停止轮询, 节省 Futu RPC
    document.addEventListener('visibilitychange', function() {
        if (document.hidden) {
            stopPolling();
        } else {
            startPolling();
        }
    });

    // ── WebSocket 实时事件 ──
    var _wsLastPush = 0;  // WS 最近推送时间戳 (ms)

    if (window.WSManager) {
        WSManager.on('order_update', function() { loadSnapshot(); });
        WSManager.on('fill', function() { loadSnapshot(); });
        WSManager.on('signal_new', function() { loadSnapshot(); });

        // DataService 缓存刷新推送 — 即时刷新 snapshot
        WSManager.on('positions_update', function() {
            _wsLastPush = Date.now();
            loadSnapshot();
        });
        WSManager.on('account_update', function() {
            _wsLastPush = Date.now();
            loadSnapshot();
        });
        WSManager.on('quote_update', function() {
            _wsLastPush = Date.now();
            loadSnapshot();
        });

        // 退出事件 → toast + 刷新
        WSManager.on('position_update', function(data) {
            if (data && data.action === 'exit') {
                var icon = data.pnl_pct >= 0 ? '✅' : '📤';
                var color = data.pnl_pct >= 0 ? '#4ade80' : '#f87171';
                _showToast(icon + ' ' + data.symbol + ' ' + (data.reason_cn || data.reason) +
                    ' PnL: ' + data.pnl_pct + '%', color);
            }
            loadSnapshot();
        });

        // 接近止损预警 → toast
        WSManager.on('risk_alert', function(data) {
            if (data && data.message) {
                var isCrit = data.level === 'CRITICAL';
                var icon = isCrit ? '🚨' : '⚠️';
                var color = isCrit ? '#f87171' : '#fbbf24';
                _showToast(icon + ' ' + (data.symbol || '') + ' ' + data.message, color);
                console.warn('[WS Risk]', data.level, data.symbol, data.message);
            }
            loadSnapshot();
        });
    }

    // ── Toast 通知 (WS 事件) ──
    function _showToast(msg, color) {
        var t = document.createElement('div');
        t.style.cssText = 'position:fixed;top:16px;right:16px;z-index:9999;padding:10px 18px;' +
            'border-radius:8px;font-size:13px;font-weight:600;color:#fff;max-width:400px;' +
            'box-shadow:0 4px 16px rgba(0,0,0,0.4);transition:opacity 0.5s;opacity:1;' +
            'background:' + (color || '#333');
        t.textContent = msg;
        document.body.appendChild(t);
        setTimeout(function() { t.style.opacity = '0'; }, 4000);
        setTimeout(function() { t.remove(); }, 4500);
    }

    // Initial start
    updateMarketStatus();
    startPolling();

    // Restore persisted sort button active state
    if (window.Prefs && _currentSort !== 'urgency') {
        document.querySelectorAll('.sort-btn').forEach(function(b) {
            b.classList.toggle('active', b.getAttribute('data-sort') === _currentSort);
        });
    }

    // Hash 导航 — 从其他页面点击"分析"跳转时自动展开
    (function() {
        if (window.location.hash === '#analytics') {
            var body = document.getElementById('analyticsBody');
            var arrow = document.getElementById('analyticsArrow');
            if (body && !body.classList.contains('open')) {
                body.classList.add('open');
                if (arrow) arrow.classList.add('open');
            }
            setTimeout(function() {
                var card = body ? body.closest('.card') : null;
                if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }, 300);
        }
    })();


    // ═══════════════════════════════════════════════════════
    //  Risk Command (pills only — card removed)
    // ═══════════════════════════════════════════════════════

    let _riskCmdPaused = false;

    async function loadRiskCommand() {
        try {
            const res = await fetch('/api/risk/command');
            const data = await res.json();
            _riskCmdPaused = data.kill_switch_active || false;
            
            // Update pills
            var pillKillVal = document.getElementById('pillKillVal');
            var pillKillSwitch = document.getElementById('pillKillSwitch');
            if (pillKillVal) {
                pillKillVal.textContent = _riskCmdPaused ? '🔴 制动中' : '🟢 运行';
                if (pillKillSwitch) pillKillSwitch.style.borderColor = _riskCmdPaused ? '#f87171' : '#30363d';
            }
            
            // Show emergency/resume button based on kill switch state
            var pillEmergency = document.getElementById('pillEmergency');
            if (pillEmergency) {
                pillEmergency.style.display = _riskCmdPaused ? '' : 'none';
                // P0-T5: 已制动时显示"恢复交易"而非再次制动(no-op)
                if (_riskCmdPaused) {
                    pillEmergency.textContent = '🟢 恢复交易';
                    pillEmergency.setAttribute('onclick', 'riskCmdTogglePause()');
                    pillEmergency.style.borderColor = 'rgba(74,222,128,0.4)';
                }
            }
        } catch (e) { /* silent */ }
    }

    async function riskCmdTogglePause() {
        var action = _riskCmdPaused ? '/api/risk/resume-trading' : '/api/risk/pause-trading';
        try {
            await fetch(action, { method: 'POST' });
            loadRiskCommand();
        } catch (e) { /* silent */ }
    }

    async function riskCmdEmergencyClose() {
        if (!confirm('⚠️ 确认紧急制动？\n将停止一切交易活动（扫描+信号+下单），60分钟后自动恢复。')) return;
        try {
            await fetch('/api/risk/pause-trading', { method: 'POST' });
            loadRiskCommand();
        } catch (e) { /* silent */ }
    }

    // ═══════════════════════════════════════════════════════
    //  初始化加载 + 轮询
    // ═══════════════════════════════════════════════════════

    loadRiskCommand();

    // 每 30 秒刷新风控pills (对齐 snapshot 频率, 消除 2 分钟盲区)
    setInterval(loadRiskCommand, 30000);


        // ── Expose handlers to window for inline HTML event handlers ──
        Object.assign(window, {
            cancelThesis, cancelProtection, confirmProtection, editThesis, loadChallenge, riskCmdEmergencyClose, riskCmdTogglePause, saveThesis, setStopLoss, sortPositions, switchTab, switchToAnalytics, toggleHeatmap, togglePosExpand, toggleSection
        });

})();
