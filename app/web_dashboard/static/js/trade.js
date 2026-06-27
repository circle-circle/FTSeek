// ═══════════════════════════════════════════════════════════════
// trade.js — Trade page logic (extracted from trade.html)
// Reads: window.__CONFIG__ = { isLive: bool }
// ═══════════════════════════════════════════════════════════════
(function() {
        // ── 全局错误捕获: 让 JS 错误在 UI 上可见 (方便手机调试, 2026-06-18) ──
        window.addEventListener('error', function(e) {
            console.error('[GlobalError]', e.message, e.filename, e.lineno);
            var el = document.getElementById('result');
            if (el) { el.className = 'result err'; el.textContent = '⚠️ JS错误: ' + e.message; el.style.display = 'block'; }
        });
        window.addEventListener('unhandledrejection', function(e) {
            console.error('[UnhandledRejection]', e.reason);
            var el = document.getElementById('result');
            if (el) { el.className = 'result err'; el.textContent = '⚠️ 异步错误: ' + (e.reason && e.reason.message || e.reason); el.style.display = 'block'; }
        });

        let curDir = 'LONG', curOp = 'OPEN', curQuote = null, curCap = null, pendingOrder = null;
        let _equityCache = 0;            // 账户净值 (仅 snapshot 同步)
        let _curSuggestion = null;       // 最后一次计算出的建议
        let _userOverridden = false;     // 用户是否手动调整了股数
        let _suppressOverride = false;   // 程序填值时抑制override检测
        let _planTargetsCache = null;    // 作战计划指标缓存
        let _planLastFetchTs = 0;
        const _riskCfg = { riskPct: 0.01, maxPct: 0.05 };
        const isLive = (window.__CONFIG__ && window.__CONFIG__.isLive) || false;

        function formatHoldingTime(created_at) {
            if (!created_at) return '--';
            const now = new Date();
            const entry = new Date(created_at);
            if (isNaN(entry.getTime())) return '--';
            const diffMs = now - entry;
            if (diffMs < 0) return '<1分';
            const diffH = Math.floor(diffMs / 3600000);
            const diffD = Math.floor(diffH / 24);
            const remainH = diffH % 24;
            if (diffD > 0) return diffD + '天' + remainH + '时';
            if (diffH > 0) return diffH + '时';
            const diffM = Math.floor(diffMs / 60000);
            return diffM > 0 ? diffM + '分' : '<1分';
        }

        // ═══ 智能仓位建议引擎 (融入下单流程) ═══
        function setEquityCache(v) {
            const eq = Number(v) || 0;
            const changed = (eq > 0 && Math.abs(eq - _equityCache) > 1);
            _equityCache = eq;
            // 净值变动 + 已有建议 → 重算
            if (changed && _curSuggestion) recomputeSuggestion(/*reapply*/ !_userOverridden);
        }

        async function getBattlePlanTargets() {
            const now = Date.now();
            if (_planTargetsCache && (now - _planLastFetchTs < 60000)) return _planTargetsCache;
            try {
                const r = await fetch('/api/battle-plan');
                if (!r.ok) return _planTargetsCache || [];
                const d = await r.json();
                _planTargetsCache = d.targets || [];
                _planLastFetchTs = now;
                return _planTargetsCache;
            } catch (e) {
                return _planTargetsCache || [];
            }
        }

        function findPlanTarget(targets, symbol) {
            const bare = (symbol || '').replace(/^US\./, '').toUpperCase();
            return targets.find(function(t) {
                const ts = String(t.symbol || '').replace(/^US\./, '').toUpperCase();
                return ts === bare;
            });
        }

        function computePosition(opts) {
            const entry = Number(opts.entry) || 0;
            const sl    = Number(opts.sl) || 0;
            const tp    = Number(opts.tp) || 0;
            const equity = Number(opts.equity) || 0;
            const direction = opts.direction || 'LONG';
            const riskPct = Number(opts.riskPct) || 0.01;
            const maxPct  = Number(opts.maxPct)  || 0.05;
            if (!(entry > 0 && sl > 0 && equity > 0)) return null;
            const priceRisk = Math.abs(entry - sl);
            if (priceRisk <= 0) return null;
            const sharesByRisk = Math.floor(equity * riskPct / priceRisk);
            const sharesByPct  = Math.floor(equity * maxPct  / entry);
            const shares = Math.max(0, Math.min(sharesByRisk, sharesByPct));
            if (shares <= 0) return null;
            const cost = shares * entry;
            const riskAmt = shares * priceRisk;
            let rr = null;
            if (tp > 0) {
                const reward = direction === 'SHORT' ? (entry - tp) : (tp - entry);
                if (reward > 0) rr = reward / priceRisk;
            }
            return {
                shares: shares, cost: cost, riskAmt: riskAmt, rr: rr,
                entry: entry, sl: sl, tp: tp,
                direction: direction, riskPct: riskPct, maxPct: maxPct
            };
        }

        // 选股即计算: 输入 / 选择 / URL 参数 → 查plan → 自动填表单 + 显示建议
        async function autoFillFromPlan(symbol, opts) {
            opts = opts || {};
            const symRaw = (symbol || '').trim().toUpperCase();
            if (!symRaw) return;
            const symFull = symRaw.indexOf('.') >= 0 ? symRaw : 'US.' + symRaw;
            const targets = await getBattlePlanTargets();
            const target = findPlanTarget(targets, symFull);

            let suggestion = null;

            if (target && target.entry_zone && target.stop_loss) {
                const ez = target.entry_zone;
                let entry = 0;
                if (Array.isArray(ez)) {
                    if (ez.length === 2) entry = (Number(ez[0]) + Number(ez[1])) / 2;
                    else if (ez.length === 1) entry = Number(ez[0]);
                }
                const sl = Number(target.stop_loss) || 0;
                const tp = Number(target.take_profit) || 0;
                const direction = target.direction || 'LONG';
                suggestion = computePosition({
                    entry: entry, sl: sl, tp: tp, direction: direction,
                    equity: _equityCache, riskPct: _riskCfg.riskPct, maxPct: _riskCfg.maxPct,
                });
                if (suggestion) {
                    suggestion.source = 'battle_plan';
                    suggestion.sourceLabel = '来自作战计划';
                    suggestion.planRationale = target.rationale || '';
                }
            } else {
                // 不在作战计划中: 用当前价 + 默认3%止损
                let price = (curQuote && curQuote.cur_price) || 0;
                if (!(price > 0)) {
                    try {
                        const r = await fetch('/api/quote/' + encodeURIComponent(symFull));
                        if (r.ok) {
                            const q = await r.json();
                            price = Number(q.cur_price) || 0;
                        }
                    } catch (e) { /* 忽略 */ }
                }
                if (price > 0) {
                    const direction = curDir;
                    const entry = price;
                    const sl = direction === 'SHORT' ? entry * 1.03 : entry * 0.97;
                    suggestion = computePosition({
                        entry: entry, sl: sl, tp: 0, direction: direction,
                        equity: _equityCache, riskPct: _riskCfg.riskPct, maxPct: _riskCfg.maxPct,
                    });
                    if (suggestion) {
                        suggestion.source = 'default';
                        suggestion.sourceLabel = '默认3%止损·无作战计划参数';
                    }
                }
            }

            _curSuggestion = suggestion;
            // 默认填充表单 (除非调用者要求不填)
            if (suggestion && opts.fillForm !== false) {
                _userOverridden = false;
                applySuggestion(suggestion);
            }
            renderSuggestion();
        }

        // 将建议填入表单
        function applySuggestion(s) {
            if (!s) return;
            _suppressOverride = true;
            try {
                if (s.direction && curDir !== s.direction) setDir(s.direction);
                const otEl = document.getElementById('orderType');
                if (otEl && s.entry > 0 && s.source === 'battle_plan') {
                    if (otEl.value !== 'LIMIT') {
                        otEl.value = 'LIMIT';
                        document.getElementById('priceGroup').style.display = 'flex';
                    }
                }
                const lpEl = document.getElementById('limitPrice');
                if (lpEl && otEl && otEl.value === 'LIMIT' && s.entry > 0) {
                    lpEl.value = s.entry.toFixed(2);
                }
                const qEl = document.getElementById('quantity');
                if (qEl && s.shares > 0) qEl.value = s.shares;
                updateEstimate();
            } finally {
                setTimeout(function() { _suppressOverride = false; }, 50);
            }
        }

        function renderSuggestion() {
            const el = document.getElementById('aiSuggest');
            if (!el) return;
            const s = _curSuggestion;
            if (!s) { el.style.display = 'none'; return; }
            // 如果账户净值为 0，打印提示并隐藏
            const cost = '$' + s.cost.toLocaleString(undefined, {maximumFractionDigits: 0});
            const riskTxt = '$' + Math.round(s.riskAmt).toLocaleString() + ' (' + (s.riskPct*100).toFixed(1) + '%风险)';
            let rrHtml = '';
            if (s.rr && s.rr > 0) {
                const rrCls = s.rr >= 2 ? 'g' : s.rr >= 1.5 ? 'y' : 'r';
                rrHtml = '<span class="ai-sep">·</span><span class="ai-rr ' + rrCls + '">R:R ' + s.rr.toFixed(2) + ':1</span>';
            }
            const srcStr = '(' + (s.sourceLabel || s.source || '') + ')';
            const labelTxt = (s.source === 'battle_plan' ? 'AI建议:' : '建议:');
            let html = '';
            html += '<span class="ai-icon">💡</span>';
            html += '<span class="ai-label">' + labelTxt + '</span>';
            html += '<span class="ai-val">' + s.shares + '股</span>';
            html += '<span class="ai-sep">·</span>';
            html += '<span class="ai-val">' + cost + '</span>';
            html += '<span class="ai-sep">·</span>';
            html += '<span class="ai-val">' + riskTxt + '</span>';
            html += rrHtml;
            html += '<span class="ai-src">· ' + srcStr + '</span>';
            html += '<a class="ai-link" onclick="toggleRiskCfg()">[调整风险]</a>';
            if (_userOverridden) {
                html += '<a class="ai-link" onclick="restoreSuggestion()">[恢复建议]</a>';
            }
            el.innerHTML = html;
            // 样式切换
            let cls = 'ai-suggest';
            if (s.source === 'default') cls += ' default-src';
            if (_userOverridden) cls += ' muted';
            el.className = cls;
            el.style.display = 'flex';
        }

        function onQuantityInput() {
            // 程序填值不计作override
            if (!_suppressOverride && _curSuggestion) {
                const cur = parseInt(document.getElementById('quantity').value) || 0;
                if (cur !== _curSuggestion.shares) {
                    _userOverridden = true;
                    renderSuggestion();
                }
            }
            updateEstimate();
        }

        function toggleRiskCfg() {
            const el = document.getElementById('riskCfg');
            if (!el) return;
            el.style.display = (el.style.display === 'flex') ? 'none' : 'flex';
        }

        function setRiskPct(pct) {
            _riskCfg.riskPct = pct;
            document.querySelectorAll('#riskCfg [data-rp]').forEach(function(b) {
                b.classList.toggle('active', Math.abs(parseFloat(b.dataset.rp) - pct) < 1e-9);
            });
            recomputeSuggestion(true);
        }
        function setMaxPct(pct) {
            _riskCfg.maxPct = pct;
            document.querySelectorAll('#riskCfg [data-mp]').forEach(function(b) {
                b.classList.toggle('active', Math.abs(parseFloat(b.dataset.mp) - pct) < 1e-9);
            });
            recomputeSuggestion(true);
        }

        function recomputeSuggestion(reapply) {
            if (!_curSuggestion) return;
            const newS = computePosition({
                entry: _curSuggestion.entry, sl: _curSuggestion.sl, tp: _curSuggestion.tp,
                direction: _curSuggestion.direction,
                equity: _equityCache,
                riskPct: _riskCfg.riskPct, maxPct: _riskCfg.maxPct,
            });
            if (!newS) return;
            newS.source = _curSuggestion.source;
            newS.sourceLabel = _curSuggestion.sourceLabel;
            _curSuggestion = newS;
            if (reapply) {
                _userOverridden = false;
                applySuggestion(newS);
            }
            renderSuggestion();
        }

        function restoreSuggestion() {
            if (!_curSuggestion) return;
            _userOverridden = false;
            applySuggestion(_curSuggestion);
            renderSuggestion();
        }

        // ── 盘前/盘后自动检测 (2026-06-12) ──
        function isExtendedHours() {
            // 用 ET 时间判断: 盘前 04:00-09:30, 盘中 09:30-16:00, 盘后 16:00-20:00
            const now = new Date();
            const etStr = now.toLocaleString('en-US', {timeZone: 'America/New_York', hour: 'numeric', minute: 'numeric', hour12: false});
            const parts = etStr.split(':');
            const etMinutes = parseInt(parts[0]) * 60 + parseInt(parts[1] || 0);
            const weekday = now.toLocaleString('en-US', {timeZone: 'America/New_York', weekday: 'short'});
            if (weekday === 'Sat' || weekday === 'Sun') return {extended: false, phase: 'WEEKEND', etMinutes: etMinutes};
            if (etMinutes >= 240 && etMinutes < 570) return {extended: true, phase: 'PRE_MARKET', etMinutes: etMinutes};   // 04:00-09:30
            if (etMinutes >= 570 && etMinutes < 960) return {extended: false, phase: 'REGULAR', etMinutes: etMinutes};     // 09:30-16:00
            if (etMinutes >= 960 && etMinutes < 1200) return {extended: true, phase: 'AFTER_HOURS', etMinutes: etMinutes};  // 16:00-20:00
            return {extended: false, phase: 'CLOSED', etMinutes: etMinutes};  // 20:00-04:00 完全休市
        }
        function getNextSessionText(phase, etMinutes) {
            if (phase === 'WEEKEND') return '周末休市，订单将在周一开盘后排队执行';
            if (phase === 'CLOSED') {
                if (etMinutes < 240) return '盘前 04:00 ET 开始，订单将排队等待';
                return '已收盘，订单将在明日盘前 04:00 ET 排队执行';
            }
            return '';
        }
        function updateExtRthUI() {
            const {extended, phase, etMinutes} = isExtendedHours();
            const group = document.getElementById('extRthGroup');
            const check = document.getElementById('extRthCheck');
            const autoHint = document.getElementById('extRthAutoHint');
            const closedHint = document.getElementById('extRthClosedHint');
            if (extended) {
                group.style.display = 'block';
                check.checked = true;
                autoHint.style.display = 'block';
                autoHint.textContent = `⚡ 已自动开启 — 当前处于${phase === 'PRE_MARKET' ? '盘前' : '盘后'}时段`;
                if (closedHint) closedHint.style.display = 'none';
                // 盘前盘后强制切换为限价单
                const otypeEl = document.getElementById('orderType');
                if (otypeEl && otypeEl.value === 'MARKET') {
                    otypeEl.value = 'LIMIT';
                    document.getElementById('priceGroup').style.display = 'flex';
                }
            } else if (phase === 'CLOSED' || phase === 'WEEKEND') {
                // 完全休市: 自动勾选 + 排队提示
                group.style.display = 'block';
                check.checked = true;       // 自动勾选: 允许盘前/盘后成交
                check.disabled = true;      // 禁止取消: 休市时必须允许 extended hours
                autoHint.style.display = 'none';
                if (closedHint) {
                    closedHint.style.display = 'block';
                    closedHint.textContent = '⏳ ' + getNextSessionText(phase, etMinutes) + ' · 已开启盘前/盘后成交';
                }
                // 休市也强制限价单
                const otypeEl2 = document.getElementById('orderType');
                if (otypeEl2 && otypeEl2.value === 'MARKET') {
                    otypeEl2.value = 'LIMIT';
                    document.getElementById('priceGroup').style.display = 'flex';
                }
            } else {
                group.style.display = 'none';
                check.checked = false;
                check.disabled = false;
                autoHint.style.display = 'none';
                if (closedHint) closedHint.style.display = 'none';
            }
        }
        // 页面加载 + 每分钟刷新
        updateExtRthUI();
        setInterval(updateExtRthUI, 60000);

        // ── 自动止损止盈勾选切换 ──
        (function() {
            const check = document.getElementById('autoSlTpCheck');
            const hint = document.getElementById('autoSlTpHint');
            const off = document.getElementById('autoSlTpOff');
            if (!check) return;
            check.addEventListener('change', function() {
                if (check.checked) {
                    hint.style.display = 'block';
                    off.style.display = 'none';
                } else {
                    hint.style.display = 'none';
                    off.style.display = 'block';
                }
            });
        })();
        // ── 统一 API 响应解析 (兼容 api_ok/api_error 和旧 flat 格式) ──
        function parseApiResult(data) {
            if (!data) return { ok: false, error: '空响应', data: null };
            if (data.ok === true) return { ok: true, data: data.data, raw: data };
            if (data.ok === false) {
                const err = data.error || {};
                return { ok: false, error: err.message || err.code || JSON.stringify(err), data: null, raw: data };
            }
            // 旧 flat 格式兼容
            return { ok: data.success, data: data, error: data.error || '未知错误', raw: data };
        }

        async function takeoverPos(symbol) {
            try {
                const resp = await fetch('/api/positions/takeover', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({symbol: symbol, owner: 'manual'})
                });
                const json = await resp.json();
                const result = parseApiResult(json);
                if (result.ok) {
                    const info = result.data || {};
                    showMsg('ok', '✅ ' + (info.message || symbol + ' 已纳入管理'));
                    if (typeof refreshAll === 'function') refreshAll();
                    else loadPositions();
                } else {
                    showMsg('err', '❌ 失败: ' + (result.error || '未知错误'));
                }
            } catch (e) {
                showMsg('err', '❌ 请求失败: ' + e.message);
            }
        }

        // escHtml 由 /static/js/utils.js 全局提供

        function setDir(d) {
            curDir = d;
            document.querySelectorAll('[data-dir]').forEach(b => b.classList.remove('active'));
            document.querySelector(`[data-dir="${d}"]`).classList.add('active');
            updateEstimate();
        }
        function setOp(o) {
            curOp = o;
            document.querySelectorAll('[data-op]').forEach(b => b.classList.remove('active'));
            document.querySelector(`[data-op="${o}"]`).classList.add('active');
            updateEstimate();
        }

        document.getElementById('orderType').addEventListener('change', function() {
            document.getElementById('priceGroup').style.display = this.value === 'LIMIT' ? 'flex' : 'none';
            updateEstimate();
        });
        document.getElementById('symbol').addEventListener('blur', onSymbolChanged);
        document.getElementById('symbol').addEventListener('input', function() {
            if (this.value.trim().length >= 3) {
                clearTimeout(this._timer);
                this._timer = setTimeout(onSymbolChanged, 500);
            }
        });
        document.getElementById('limitPrice').addEventListener('input', function() {
            // Auto-switch: typing a price → switch to LIMIT
            const otEl = document.getElementById('orderType');
            if (this.value && parseFloat(this.value) > 0 && otEl.value === 'MARKET') {
                otEl.value = 'LIMIT';
                document.getElementById('priceGroup').style.display = 'flex';
            }
            updateEstimate();
        });

        // symbol 变动 → 并发: 报价 + 作战计划 → 自动填表单 + 显示智能建议
        function onSymbolChanged() {
            const sym = document.getElementById('symbol').value.trim().toUpperCase();
            if (!sym) return;
            // 重置override状态 (新 symbol 默认采纳建议)
            _userOverridden = false;
            // 报价与plan并发; plan会设置股数/价格/方向
            fetchQuote();
            autoFillFromPlan(sym);
        }

        // ── Symbol Intelligence ──
        async function loadIntel(sym) {
            const card = document.getElementById('intelCard');
            const content = document.getElementById('intelContent');
            card.style.display = 'block';
            content.innerHTML = '<div style="text-align:center;color:#8b949e;padding:8px">加载系统分析...</div>';
            try {
                const res = await fetch(`/api/trade/symbol-intel/${encodeURIComponent(sym)}`);
                const d = await res.json();
                let html = '';

                // Header: signal direction
                const sig = d.signal || {};
                const sigDir = sig.direction || '';
                const sigCls = sigDir === 'LONG' ? 'long' : sigDir === 'SHORT' ? 'short' : 'neutral';
                const sigLabel = sig.has_signal ? `${sigDir} ▲ 置信${(sig.confidence * 100).toFixed(0)}%` : '无近期信号';
                html += `<div class="intel-header">
                    <span class="intel-title">${escHtml(d.symbol)} 系统判断</span>
                    <span class="intel-signal ${sigCls}">${sigLabel}</span>
                </div>`;

                // Recent trades
                const trades = d.recent_trades || {};
                if (trades.count > 0) {
                    const pnlCls = trades.net_pnl >= 0 ? 'g' : 'r';
                    html += `<div class="intel-row"><span class="intel-label">近 7 天交易</span>
                        <span class="intel-val">${trades.count}笔 胜率${trades.win_rate}% <span class="${pnlCls}">${trades.net_pnl >= 0 ? '+$' : '-$'}${Math.abs(trades.net_pnl).toFixed(0)}</span></span></div>`;
                } else {
                    html += `<div class="intel-row"><span class="intel-label">近 7 天交易</span><span class="intel-val" style="color:#8b949e">无记录</span></div>`;
                }

                // Current position
                const pos = d.position || {};
                if (pos.held) {
                    const pCls = pos.pnl >= 0 ? 'g' : 'r';
                    const slWarn = pos.sl_distance_pct > 0 && pos.sl_distance_pct < 5
                        ? ` <span style="color:${pos.sl_distance_pct < 2 ? '#f87171' : '#fbbf24'}">⚠️ 距止损${pos.sl_distance_pct}%</span>` : '';
                    html += `<div class="intel-row"><span class="intel-label">当前持仓</span>
                        <span class="intel-val">${pos.direction} ${pos.size}股 <span class="${pCls}">${pos.pnl >= 0 ? '+$' : '-$'}${Math.abs(pos.pnl).toFixed(0)} (${pos.pnl_pct >= 0 ? '+' : ''}${pos.pnl_pct.toFixed(1)}%)</span>${slWarn}</span></div>`;
                }

                // Battle plan match
                const plan = d.plan || {};
                if (plan.in_plan) {
                    const planCls = plan.cancelled ? 'cancelled' : '';
                    const statusIcon = plan.cancelled ? '❌' : '✅';
                    const zone = plan.entry_zone ? `$${plan.entry_zone[0]}–$${plan.entry_zone[1]}` : '--';
                    html += `<div class="intel-plan ${planCls}">
                        <div class="plan-dir">${statusIcon} 战前计划: ${plan.direction} 入场区 ${zone}</div>
                        ${plan.rationale ? `<div class="plan-rationale">${escHtml(plan.rationale)}</div>` : ''}
                    </div>`;
                }

                content.innerHTML = html;
            } catch (e) {
                card.style.display = 'none';
            }
        }

        async function fetchQuote() {
            let sym = document.getElementById('symbol').value.trim().toUpperCase();
            if (!sym) return;
            // Auto-prefix: bare ticker → US.TICKER (Futu format)
            if (!sym.includes('.')) {
                sym = 'US.' + sym;
                document.getElementById('symbol').value = sym;
            }
            document.getElementById('quoteBox').style.display = 'block';
            document.getElementById('quotePrice').textContent = '...';
            try {
                const controller = new AbortController();
                const timeout = setTimeout(() => controller.abort(), 8000);

                const [qRes, cRes] = await Promise.all([
                    fetch(`/api/quote/${sym}`, { signal: controller.signal }),
                    fetch(`/api/order-capacity/${sym}`, { signal: controller.signal }),
                ]);
                clearTimeout(timeout);
                const q = await qRes.json();
                const c = await cRes.json();
                curCap = c;

                if (q.cur_price) {
                    curQuote = q;
                    document.getElementById('quotePrice').textContent = `$${q.cur_price.toFixed(2)}`;
                    const upd = q.update_time ? q.update_time.substring(11, 19) : '--';
                    document.getElementById('quoteDetail').innerHTML =
                        `高 $${q.high_price?.toFixed(2)||'--'} | 低 $${q.low_price?.toFixed(2)||'--'} | 昨收 $${q.prev_close?.toFixed(2)||'--'} | ${upd}`;

                    // 填充资金信息
                    if (c.cur_price || c.cash != null) {
                        const fi = document.getElementById('fundInfo');
                        fi.style.display = 'block';
                        const fmtK = v => v >= 1000 ? '$' + (v/1000).toFixed(1) + 'K' : '$' + v.toFixed(0);
                        document.getElementById('fundCash').textContent = fmtK(c.cash || 0);
                        document.getElementById('fundPower').textContent = fmtK(c.power || 0) + (c.max_can_buy ? ` (${c.max_can_buy.toLocaleString()}股)` : '');
                        const held = c.held_qty || 0;
                        if (held !== 0) {
                            document.getElementById('fundHeldRow').style.display = 'flex';
                            const dir = held > 0 ? 'LONG' : 'SHORT';
                            document.getElementById('fundHeld').textContent = `${dir} ${Math.abs(held)}股 (可卖 ${c.can_sell || 0})`;
                        } else {
                            document.getElementById('fundHeldRow').style.display = 'none';
                        }
                        document.getElementById('fundShort').textContent = c.can_short ? `${c.can_short.toLocaleString()}股` + (c.max_short_power ? ` (${fmtK(c.max_short_power)})` : '') : '--';
                    }

                    // Show existing position hint for same symbol
                    const heldEl = document.getElementById('heldHint');
                    const heldTextEl = document.getElementById('heldHintText');
                    if (c.held_qty && c.held_qty !== 0) {
                        const dir = c.held_qty > 0 ? 'LONG (做多)' : 'SHORT (做空)';
                        heldTextEl.textContent = `已有持仓: ${sym} ${dir} ${Math.abs(c.held_qty)} 股`;
                        heldEl.classList.add('show');
                    } else {
                        heldEl.classList.remove('show');
                    }

                    updateEstimate();
                    loadIntel(sym);
                } else {
                    document.getElementById('quotePrice').textContent = '--';
                    document.getElementById('quoteDetail').textContent = q.error || '无数据';
                    document.getElementById('heldHint').classList.remove('show');
                }
            } catch (e) {
                document.getElementById('quotePrice').textContent = '--';
                document.getElementById('quoteDetail').textContent = e.name === 'AbortError' ? '报价超时 (8s)' : `网络异常: ${e.message}`;
                document.getElementById('heldHint').classList.remove('show');
            }
        }

        function setQty(n) { if (n > 0) { document.getElementById('quantity').value = n; updateEstimate(); } }
        function stepQty(delta) {
            const el = document.getElementById('quantity');
            const cur = parseInt(el.value) || 0;
            const nv = Math.max(1, cur + delta);
            el.value = nv;
            updateEstimate();
        }
        function pctQty(pct) {
            if (!curCap) { showMsg('err', '⚠️ 请先输入标的获取报价'); return; }
            const price = curQuote?.cur_price || curCap.cur_price;
            if (!price || price <= 0) { showMsg('err', '⚠️ 无法获取当前价格'); return; }
            let maxShares;
            if (curDir === 'LONG' && curOp === 'OPEN') {
                maxShares = curCap.max_can_buy || Math.floor((curCap.power || 0) / price);
            } else if (curDir === 'SHORT' && curOp === 'OPEN') {
                maxShares = curCap.can_short || Math.floor((curCap.max_short_power || 0) / price);
            } else if (curOp === 'CLOSE') {
                maxShares = Math.abs(curCap.held_qty || 0);
                if (curDir === 'LONG') maxShares = curCap.can_sell || maxShares;
            } else {
                maxShares = curCap.max_can_buy || 100;
            }
            const n = Math.max(1, Math.floor(maxShares * pct));
            document.getElementById('quantity').value = n;
            updateEstimate();
        }

        function updateEstimate() {
            const qty = parseInt(document.getElementById('quantity').value) || 0;
            const btn = document.getElementById('btnSubmit');
            const panel = document.getElementById('estimatePanel');
            const estCost = document.getElementById('estCost');
            const estMargin = document.getElementById('estMargin');
            const estBpPct = document.getElementById('estBpPct');
            const estWarning = document.getElementById('estWarning');

            if (curQuote && curQuote.cur_price && qty > 0) {
                const val = qty * curQuote.cur_price;
                const action = getAction();
                btn.textContent = `预览: ${curOp === 'OPEN' ? '开仓' : '平仓'} ${curDir === 'LONG' ? '做多' : '做空'} $${val.toLocaleString(undefined,{maximumFractionDigits:0})}`;
                btn.className = action === 'BUY' ? 'btn btn-buy' : 'btn btn-sell';

                let marginRate = 0;
                if (curOp === 'OPEN') {
                    marginRate = curDir === 'SHORT' ? 1.0 : 0.5;
                }
                const marginRequired = val * marginRate;
                const fmtCost = '$' + val.toLocaleString(undefined, {maximumFractionDigits: 0});
                const fmtMargin = marginRequired > 0
                    ? '$' + marginRequired.toLocaleString(undefined, {maximumFractionDigits: 0})
                    : (curOp === 'CLOSE' ? '平仓无要求' : '--');

                estCost.textContent = fmtCost;
                estMargin.textContent = fmtMargin;
                panel.classList.add('show');

                if (curCap && curCap.power && curCap.power > 0) {
                    const bpPct = (marginRequired / curCap.power) * 100;
                    const pctCls = bpPct > 25 ? 'r' : bpPct > 15 ? 'y' : 'g';
                    estBpPct.innerHTML = `<span class="est-v ${pctCls}">${bpPct.toFixed(1)}%</span>`;
                    if (bpPct > 25) {
                        estWarning.innerHTML = `<div class="est-danger">⚠️ 保证金占用超过购买力的25% (${bpPct.toFixed(1)}%)，请谨慎下单</div>`;
                    } else if (bpPct > 15) {
                        estWarning.innerHTML = `<div class="est-warn">⚠️ 保证金占用购买力的 ${bpPct.toFixed(1)}%，注意风险</div>`;
                    } else {
                        estWarning.innerHTML = '';
                    }
                } else {
                    estBpPct.textContent = '--';
                    estWarning.innerHTML = '';
                }

                // ── 实时限价偏离提示 (P0 2026-06-15) ──
                // 2026-06-25: 盘前/盘后/休市时 cur_price 为上次收盘价, 不反映盘外报价 → 跳过 (与后端 validator 对齐)
                const otype = document.getElementById('orderType')?.value;
                const lpRaw = document.getElementById('limitPrice')?.value;
                if (otype === 'LIMIT' && lpRaw && parseFloat(lpRaw) > 0 && curQuote.cur_price > 0) {
                    const lp = parseFloat(lpRaw);
                    const cp = curQuote.cur_price;
                    const dev = Math.abs(lp - cp) / cp * 100;
                    if (dev >= 10) {
                        const existing = estWarning.innerHTML || '';
                        estWarning.innerHTML = existing + `<div class="est-danger">⚠️ 限价偏离市价 ${dev.toFixed(1)}% (≥10%)，提交时需确认</div>`;
                    } else if (dev >= 5) {
                        const existing = estWarning.innerHTML || '';
                        estWarning.innerHTML = existing + `<div class="est-warn">⚠️ 限价偏离市价 ${dev.toFixed(1)}% (≥5%)，提交时需确认</div>`;
                    }
                }
            } else {
                btn.textContent = '预览下单';
                btn.className = 'btn btn-buy';
                panel.classList.remove('show');
                estWarning.innerHTML = '';
            }
        }

        function getAction() {
            if (curDir === 'LONG' && curOp === 'OPEN') return 'BUY';
            if (curDir === 'LONG' && curOp === 'CLOSE') return 'SELL';
            if (curDir === 'SHORT' && curOp === 'OPEN') return 'SELL';
            if (curDir === 'SHORT' && curOp === 'CLOSE') return 'BUY';
            return 'BUY';
        }

        // ── 清除止损止盈输入 (修复: clearSlTp 未定义, 2026-06-18) ──
        function clearSlTp() {
            var slEl = document.getElementById('stopLoss');
            var tpEl = document.getElementById('takeProfit');
            if (slEl) slEl.value = '';
            if (tpEl) tpEl.value = '';
            var slTpRow = document.getElementById('slTpRow');
            if (slTpRow) slTpRow.style.display = 'none';
        }

        // ── Preview modal = confirmation ──
        function previewOrder() {
          try {
            let sym = document.getElementById('symbol').value.trim().toUpperCase();
            if (!sym.includes('.')) { sym = 'US.' + sym; document.getElementById('symbol').value = sym; }
            const qty = parseInt(document.getElementById('quantity').value);
            let otype = document.getElementById('orderType').value;
            let price = document.getElementById('limitPrice').value;
            // 自动修正: 有价格输入但类型是 MARKET → 自动切到 LIMIT
            if (price && parseFloat(price) > 0 && otype === 'MARKET') {
                otype = 'LIMIT';
                document.getElementById('orderType').value = 'LIMIT';
                document.getElementById('priceGroup').style.display = 'flex';
            }
            const action = getAction();

            if (!sym || !qty) { showMsg('err', '请填写标的和数量'); return; }

            const est = curQuote ? qty * curQuote.cur_price : 0;
            const {extended, phase, etMinutes} = isExtendedHours();
            const isClosed = (phase === 'CLOSED' || phase === 'WEEKEND');
            const tif = isClosed ? 'GTC' : 'DAY';
            const extRth = document.getElementById('extRthCheck')?.checked || false;
            // 盘前盘后必须限价单
            if (extRth && otype === 'MARKET') { showMsg('err', '盘前/盘后交易仅支持限价单'); return; }
            // 完全休市也必须限价单
            if (isClosed && otype === 'MARKET') { showMsg('err', '非交易时段仅支持限价单 (GTC)'); return; }
            const autoSlTp = document.getElementById('autoSlTpCheck')?.checked !== false;
            // 止损止盈: 优先读用户手动输入，为空时 fallback 作战计划值
            const slInput = parseFloat(document.getElementById('stopLoss')?.value) || 0;
            const tpInput = parseFloat(document.getElementById('takeProfit')?.value) || 0;
            const slPlan = (_curSuggestion && _curSuggestion.sl) || 0;
            const tpPlan = (_curSuggestion && _curSuggestion.tp) || 0;
            const slFinal = slInput > 0 ? slInput : slPlan;
            const tpFinal = tpInput > 0 ? tpInput : tpPlan;
            pendingOrder = { symbol: sym, direction: curDir, action, quantity: qty, order_type: otype, price: price || null, reason: (_curSuggestion && _curSuggestion.planRationale) || '', operation: curOp, time_in_force: tif, fill_outside_rth: extRth, stop_loss: autoSlTp ? slFinal : 0, take_profit: autoSlTp ? tpFinal : 0, auto_sltp: autoSlTp };

            const el = document.getElementById('previewContent');
            let html = '';
            html += '<div class="preview-row"><span class="pk">标的</span><span class="pv">' + sym + '</span></div>';
            html += '<div class="preview-row"><span class="pk">操作</span><span class="pv">' + (curOp==='OPEN'?'开仓':'平仓') + ' ' + (curDir==='LONG'?'做多':'做空') + ' → ' + action + '</span></div>';
            html += '<div class="preview-row"><span class="pk">数量</span><span class="pv">' + qty.toLocaleString() + ' 股</span></div>';
            html += '<div class="preview-row"><span class="pk">类型</span><span class="pv">' + otype + (price ? ' @ $'+price : '') + '</span></div>';
            html += '<div class="preview-row"><span class="pk">有效期</span><span class="pv">' + tif + (isClosed ? ' (自动设为 GTC — 排队至下一交易时段)' : '') + '</span></div>';
            html += '<div class="preview-row"><span class="pk">预估金额</span><span class="pv">' + (est > 0 ? '$'+est.toLocaleString(undefined,{maximumFractionDigits:0}) : '--') + '</span></div>';
            // SL/TP display
            const slVal = pendingOrder.stop_loss || 0;
            const tpVal = pendingOrder.take_profit || 0;
            if (autoSlTp && (slVal > 0 || tpVal > 0)) {
                let sltpStr = '';
                if (slVal > 0) sltpStr += '🛑 止损 $' + slVal.toFixed(2);
                if (slVal > 0 && tpVal > 0) sltpStr += '  |  ';
                if (tpVal > 0) sltpStr += '🎯 止盈 $' + tpVal.toFixed(2);
                sltpStr += '  (成交后自动挂单)';
                html += '<div class="preview-row" style="color:#4ade80"><span class="pk">风控</span><span class="pv">' + sltpStr + '</span></div>';
            } else if (!autoSlTp) {
                html += '<div class="preview-row" style="color:#fbbf24"><span class="pk">风控</span><span class="pv">⚠️ 未设置自动止损止盈 — 需手动管理</span></div>';
            }
            if (extRth) {
                html += '<div class="preview-row" style="color:#fbbf24"><span class="pk">⚡ 盘前/盘后</span><span class="pv">' + (phase === 'PRE_MARKET' ? '盘前时段' : '盘后时段') + ' · 限价单 · 流动性较低</span></div>';
            }
            if (isClosed) {
                html += '<div class="preview-row" style="color:#f87171;background:rgba(248,113,113,0.08);padding:6px 10px;border-radius:6px"><span class="pk">⏳ 排队</span><span class="pv">' + getNextSessionText(phase, etMinutes) + '</span></div>';
            }
            el.innerHTML = html;

            // ── 价格偏离检测 (P0 2026-06-15: SNDK 事故防护) ──
            // 仅盘中实时行情有效; 盘前/盘后/休市时报价不准, 跳过检测
            const confirmBtn = document.getElementById('confirmBtn');
            let priceWarnAcknowledged = false;
            const _skipDevCheck = (phase !== 'REGULAR');
            if (!_skipDevCheck && otype === 'LIMIT' && price && curQuote && curQuote.cur_price > 0) {
                const lp = parseFloat(price);
                const cp = curQuote.cur_price;
                const dev = Math.abs(lp - cp) / cp * 100;
                const isHard = dev >= 10;
                const isWarn = dev >= 5;

                if (isHard || isWarn) {
                    let scenarioMsg = '';
                    if (action === 'BUY') {
                        if (lp > cp) scenarioMsg = `买入限价 $${lp.toFixed(2)} 高于市价 $${cp.toFixed(2)} 达 ${dev.toFixed(1)}%，将以市价立即成交（可能非预期）`;
                        else scenarioMsg = `买入限价 $${lp.toFixed(2)} 低于市价 $${cp.toFixed(2)} 达 ${dev.toFixed(1)}%，订单不会立即成交（请确认是否输入正确）`;
                    } else {
                        if (lp < cp) scenarioMsg = `卖出限价 $${lp.toFixed(2)} 低于市价 $${cp.toFixed(2)} 达 ${dev.toFixed(1)}%，将以市价立即成交（可能非预期）`;
                        else scenarioMsg = `卖出限价 $${lp.toFixed(2)} 高于市价 $${cp.toFixed(2)} 达 ${dev.toFixed(1)}%，订单不会立即成交（请确认是否输入正确）`;
                    }

                    if (isHard) {
                        // ≥10% 强提醒 — 需勾选确认, 但不阻止提交 (2026-06-24: 原 hard block 改为 strong warn)
                        const warnId = 'priceWarnHard_' + Date.now();
                        el.innerHTML += `<div style="background:#dc2626;color:#fff;padding:12px;border-radius:8px;margin:12px 0;font-weight:bold;font-size:14px">
                            🛑 价格偏离过大 (${dev.toFixed(1)}%)<br>
                            <span style="font-weight:normal;font-size:13px">${scenarioMsg}</span><br>
                            <label style="font-weight:normal;font-size:13px;margin-top:8px;display:flex;align-items:center;gap:6px;cursor:pointer">
                                <input type="checkbox" id="${warnId}" style="width:18px;height:18px;cursor:pointer" onchange="
                                    var btn = document.getElementById('confirmBtn');
                                    if (this.checked) {
                                        btn.disabled = false;
                                        btn.style.opacity = '1';
                                        btn.style.cursor = 'pointer';
                                        btn.textContent = '⚠️ 确认提交 (已知晓价格偏离大)';
                                        window._priceWarnAck = true;
                                    } else {
                                        btn.disabled = true;
                                        btn.style.opacity = '0.5';
                                        btn.style.cursor = 'not-allowed';
                                        btn.textContent = '请先确认价格偏离';
                                        window._priceWarnAck = false;
                                    }
                                ">
                                我已知晓价格偏离 ${dev.toFixed(1)}%，确认继续下单
                            </label>
                        </div>`;
                        confirmBtn.disabled = true;
                        confirmBtn.textContent = '请先确认价格偏离';
                        confirmBtn.style.opacity = '0.5';
                        confirmBtn.style.cursor = 'not-allowed';
                        window._priceWarnAck = false;
                    } else {
                        // 5-10% 软警告 — 需勾选确认
                        const warnId = 'priceWarnCheck_' + Date.now();
                        el.innerHTML += `<div style="background:#f59e0b;color:#1a1a1a;padding:12px;border-radius:8px;margin:12px 0;font-weight:bold;font-size:14px">
                            ⚠️ 价格偏离警告 (${dev.toFixed(1)}%)<br>
                            <span style="font-weight:normal;font-size:13px">${scenarioMsg}</span><br>
                            <label style="font-weight:normal;font-size:13px;margin-top:8px;display:flex;align-items:center;gap:6px;cursor:pointer">
                                <input type="checkbox" id="${warnId}" style="width:18px;height:18px;cursor:pointer" onchange="
                                    var btn = document.getElementById('confirmBtn');
                                    if (this.checked) {
                                        btn.disabled = false;
                                        btn.style.opacity = '1';
                                        btn.style.cursor = 'pointer';
                                        btn.textContent = '⚠️ 确认提交 (已知晓价格异常)';
                                        window._priceWarnAck = true;
                                    } else {
                                        btn.disabled = true;
                                        btn.style.opacity = '0.5';
                                        btn.style.cursor = 'not-allowed';
                                        btn.textContent = '请先确认价格异常';
                                        window._priceWarnAck = false;
                                    }
                                ">
                                我已知晓价格偏离 ${dev.toFixed(1)}%，确认继续下单
                            </label>
                        </div>`;
                        confirmBtn.disabled = true;
                        confirmBtn.textContent = '请先确认价格异常';
                        confirmBtn.style.opacity = '0.5';
                        confirmBtn.style.cursor = 'not-allowed';
                        window._priceWarnAck = false;
                    }
                }
            }

            // Preview IS the confirmation — direct confirm button
            if (!confirmBtn.disabled) {
                confirmBtn.className = action === 'BUY' ? 'btn btn-buy' : 'btn btn-sell';
                confirmBtn.textContent = `确认 ${action === 'BUY' ? '买入' : '卖出'} ${qty} 股`;
            }
            document.getElementById('previewModal').classList.add('show');
          } catch(e) {
            showMsg('err', '❌ 预览失败: ' + e.message);
            console.error('[previewOrder]', e);
          }
        }

        function closePreview() { document.getElementById('previewModal').classList.remove('show'); pendingOrder = null; window._priceWarnAck = false; }

        async function executeOrder() {
            if (!pendingOrder) return;
            const order = pendingOrder;
            // 传递价格偏离确认标志
            if (window._priceWarnAck) {
                order.acknowledge_price_warning = true;
            }
            // 幂等 key: 防止双击/网络重试导致重复下单 (P0 2026-06-15)
            order.idempotency_key = 'ord_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8);
            closePreview();
            // ── Mobile fix: 全屏加载遮罩 + 按钮禁用 (防重复点击, 2026-06-18) ──
            const confirmBtn = document.getElementById('confirmBtn');
            if (confirmBtn) { confirmBtn.disabled = true; confirmBtn.textContent = '提交中...'; }
            var _loadingOverlay = document.getElementById('orderLoadingOverlay');
            if (!_loadingOverlay) {
                _loadingOverlay = document.createElement('div');
                _loadingOverlay.id = 'orderLoadingOverlay';
                _loadingOverlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:200;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:12px';
                _loadingOverlay.innerHTML = '<div style="width:40px;height:40px;border:3px solid #30363d;border-top-color:#4ade80;border-radius:50%;animation:_orderSpin 0.8s linear infinite"></div><div style="color:#e1e4e8;font-size:14px;font-weight:600">订单提交中...</div>';
                document.body.appendChild(_loadingOverlay);
            }
            _loadingOverlay.style.display = 'flex';
            showMsg('ok', '提交中...');
            // ── Mobile fix: 30s AbortController 超时 (防无限等待, 2026-06-18) ──
            const controller = new AbortController();
            const timeout = setTimeout(() => controller.abort(), 30000);
            try {
                const res = await fetch('/api/orders', {
                    method: 'POST', headers: {'Content-Type':'application/json'},
                    body: JSON.stringify(order),
                    signal: controller.signal,
                });
                clearTimeout(timeout);
                const d = await res.json();
                const result = parseApiResult(d);
                if (result.ok) {
                    const data = result.data || {};
                    showMsg('ok', `✅ 订单提交成功! ID: ${data.order?.order_id || data.order_id || '--'}`);
                    // 立即将新订单插入本地列表, 不用等下次 snapshot 轮询
                    var newOrder = data.order || data || {};
                    if (newOrder.order_id || newOrder.order_id === 0) {
                        // ManualOrder dataclass 字段映射到 broker 订单格式
                        _brokerOrders.unshift({
                            order_id: newOrder.order_id,
                            code: newOrder.symbol || order.symbol || '',
                            trd_side: newOrder.action || (order.direction === 'SHORT' ? 'SELL' : 'BUY'),
                            qty: newOrder.quantity || order.quantity || 0,
                            price: newOrder.price || order.limit_price || 0,
                            order_status: (newOrder.status || 'submitted').toUpperCase(),
                            order_type: newOrder.order_type || order.order_type || 'MARKET',
                            create_time: newOrder.timestamp || new Date().toISOString().replace('T', ' ').substring(0, 19),
                            filled_qty: 0,
                        });
                        renderUnifiedOrders();
                    }
                    loadOrders(); loadPositions();
                    // 仅在从作战计划跳转过来时才跳回, 否则留在交易页面
                    var params = new URLSearchParams(window.location.search);
                    if (params.get('from') === 'plan') {
                        setTimeout(function() { window.location.href = '/'; }, 2000);
                    }
                } else {
                    const errData = result.raw || {};
                    showMsg('err', `❌ ${errData.errors ? errData.errors.join(', ') : (result.error || '下单失败')}`);
                }
            } catch (e) {
                clearTimeout(timeout);
                showMsg('err', `❌ ${e.name === 'AbortError' ? '请求超时(30s)，请检查网络后重试' : e.message}`);
            } finally {
                if (_loadingOverlay) _loadingOverlay.style.display = 'none';
                if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.textContent = '确认下单'; }
            }
        }

        function showMsg(type, msg) {
            const el = document.getElementById('result');
            el.className = `result ${type}`; el.textContent = msg; el.style.display = 'block';
            if (type === 'ok') setTimeout(() => el.style.display = 'none', 5000);
        }

        let _slMap = {};
        let _diagData = {};
        let _prevPnl = {};  // PnL 趋势: {symbol: previous_unrealized_pnl}

        function computeSignal(pos, slData) {
            const cost = pos.cost_price || 0;
            const cur = pos.cur_price || 0;
            const isLong = pos.direction === 'LONG';
            const reasons = [];
            let color = 'green', action = '安心持有', actionCls = 'hold';
            if (slData) {
                if (slData.danger || slData.sl_distance_pct < 2) {
                    color = 'red'; action = '止损临近，注意风控'; actionCls = 'exit';
                    reasons.push('🔴 距止损仅 ' + slData.sl_distance_pct.toFixed(1) + '%');
                } else if (slData.sl_distance_pct < 5) {
                    if (color !== 'red') { color = 'yellow'; action = '关注止损距离'; actionCls = 'caution'; }
                    reasons.push('🟡 距止损 ' + slData.sl_distance_pct.toFixed(1) + '%，偏近');
                }
            }
            if (cost > 0 && cur > 0 && color === 'green') {
                const pnlPct = isLong ? ((cur - cost) / cost * 100) : ((cost - cur) / cost * 100);
                if (pnlPct > 15) { color = 'yellow'; action = '浮盈可观，考虑移动止损锁利'; actionCls = 'caution'; reasons.push('💰 浮盈 +' + pnlPct.toFixed(1) + '%'); }
                else if (pnlPct < -10) { color = 'yellow'; action = '浮亏较深，关注止损位'; actionCls = 'caution'; reasons.push('📉 浮亏 ' + pnlPct.toFixed(1) + '%'); }
            }
            if (reasons.length === 0) reasons.push('🟢 仓位健康，趋势完好');
            return {color, action, actionCls, reasons};
        }
        function showDiag(sym, el) {
            hideDiag();
            const d = _diagData[sym];
            if (!d) return;
            const p = d.pos, sl = d.slData, sig = d.signal;
            const cost = p.cost_price || 0, cur = p.cur_price || 0;
            const upnl = p.unrealized_pl || 0;
            const qty = d.qty, isLong = d.isLong;
            const pnlPct = cost > 0 && cur > 0 ? (isLong ? ((cur-cost)/cost*100) : ((cost-cur)/cost*100)) : 0;
            let html = '<div class="diag-header"><span class="diag-sym">' + escHtml(sym.replace(/^US\./,'')) + '</span>';
            html += '<span class="diag-dir ' + (isLong?'long':'short') + '">' + (isLong?'LONG':'SHORT') + ' ' + qty + '</span></div>';
            html += '<div style="font-size:13px;margin-bottom:8px"><span class="' + (upnl>=0?'long':'short') + '">' + (upnl>=0?'+$':'-$') + Math.abs(upnl).toFixed(2) + ' (' + (pnlPct>=0?'+':'') + pnlPct.toFixed(1) + '%)</span>';
            if (sl) html += ' | SL <span style="color:' + (sl.danger?'#f87171':'#fbbf24') + '">' + sl.sl_distance_pct.toFixed(1) + '%</span>';
            html += '</div>';
            html += '<div class="diag-action ' + sig.actionCls + '">' + escHtml(sig.action) + '</div>';
            sig.reasons.forEach(r => { html += '<div class="diag-reason">' + escHtml(r) + '</div>'; });
            html += '<div class="diag-btns">';
            html += '<button class="diag-btn" onclick="closePos(\'' + escHtml(sym) + '\',' + qty + ',true)">平半仓</button>';
            html += '<button class="diag-btn danger" onclick="closePos(\'' + escHtml(sym) + '\',' + qty + ',false)">全平</button>';
            html += '<a class="diag-btn" href="/stock/' + encodeURIComponent(sym) + '">详情 →</a>';
            // 查找该标的的活跃挂单, 提供撤单按钮
            var pendingForSym = (_brokerOrders || []).filter(function(o) {
                var st = String(o.order_status || '').toUpperCase();
                return o.code === sym && st.indexOf('FILLED') < 0 && st.indexOf('CANCEL') < 0
                    && st.indexOf('FAIL') < 0 && st.indexOf('REJECT') < 0;
            });
            if (pendingForSym.length > 0) {
                html += '<button class="diag-btn" style="border-color:#f59e0b;color:#fbbf24" onclick="cancelOrder(\'' + escHtml(pendingForSym[0].order_id) + '\');hideDiag()">撤挂单 (' + pendingForSym.length + ')</button>';
            }
            html += '</div>';
            const card = document.getElementById('diagCard');
            card.innerHTML = html;
            const isMobile = window.innerWidth <= 768;
            if (isMobile) {
                card.style.top = '80px';
                card.style.left = '10px';
            } else {
                const rect = el.getBoundingClientRect();
                let top = rect.bottom + 8, left = rect.left - 140;
                if (left < 10) left = 10;
                if (left + 320 > window.innerWidth) left = window.innerWidth - 330;
                if (top + 300 > window.innerHeight) top = rect.top - 310;
                card.style.top = top + 'px'; card.style.left = left + 'px';
            }
            card.classList.add('show');
            document.getElementById('diagOverlay').classList.add('show');
        }
        function hideDiag() {
            document.getElementById('diagCard').classList.remove('show');
            document.getElementById('diagOverlay').classList.remove('show');
        }

        let closeCtx = null;

        function closePos(sym, totalQty, isHalf) {
            const absQty = Math.abs(parseInt(totalQty) || 0);
            if (!absQty) { showResult('⚠️', `${sym} 无持仓`); return; }
            closeCtx = { sym, absQty };
            document.getElementById('closeSym').textContent = sym;
            document.getElementById('closeTotalQty').textContent = absQty + ' 股';
            // 现价 (从持仓数据获取)
            const posData = _diagData[sym];
            const curPrice = posData ? (posData.pos.cur_price || posData.pos.current || 0) : 0;
            document.getElementById('closeCurPrice').textContent = curPrice > 0 ? '$' + curPrice.toFixed(2) : '--';
            const preFillQty = isHalf === true ? Math.max(1, Math.floor(absQty / 2)) : null;
            document.getElementById('closeQtyInput').value = preFillQty !== null ? preFillQty : '';
            document.getElementById('closeQtyInput').max = absQty;
            document.getElementById('closeQtyInput').placeholder = absQty + ' (全部平仓)';
            document.getElementById('closePriceInput').value = '';
            document.getElementById('closeReasonInput').value = '手动平仓';
            document.getElementById('closeError').style.display = 'none';
            document.getElementById('closeConfirmBtn').textContent = '确认平仓';
            document.getElementById('closeModal').classList.add('show');
            setTimeout(() => document.getElementById('closeQtyInput').focus(), 100);

            // 加载入场 thesis
            const thesisRow = document.getElementById('closeThesisRow');
            const thesisEl = document.getElementById('closeThesis');
            thesisRow.style.display = 'none';
            thesisEl.textContent = '';
            fetch(`/api/positions/${encodeURIComponent(sym)}/thesis`)
                .then(r => r.json())
                .then(data => {
                    const t = data.thesis || (data.data && data.data.thesis) || '';
                    const ctx = data.entry_context || (data.data && data.data.entry_context);
                    if (t || ctx) {
                        let html = '';
                        if (t) html += `<div>${escHtml(t)}</div>`;
                        if (ctx && typeof ctx === 'object') {
                            const parts = [];
                            if (ctx.plan_match) parts.push('✅ 计划匹配');
                            if (ctx.in_avoid_list) parts.push('⚠️ 回避列表');
                            if (ctx.signal) parts.push(`信号: ${escHtml(ctx.signal.direction)}(${ctx.signal.strength})`);
                            if (ctx.vix) parts.push(`VIX: ${escHtml(String(ctx.vix))}`);
                            if (ctx.history_7d) parts.push(`7天: ${ctx.history_7d.wins}W/${ctx.history_7d.trades - ctx.history_7d.wins}L`);
                            if (parts.length) html += `<div style="color:#8b949e;margin-top:4px">${escHtml(parts.join(' · '))}</div>`;
                        }
                        if (html) {
                            thesisEl.innerHTML = html;
                            thesisRow.style.display = 'block';
                        }
                    }
                })
                .catch(() => {});
        }

        // 限价平仓 (止损/止盈) — 打开平仓弹窗并预填价格
        function closePosAtPrice(sym, totalQty, presetPrice) {
            closePos(sym, totalQty, false);
            if (presetPrice > 0) {
                document.getElementById('closePriceInput').value = presetPrice.toFixed(2);
                document.getElementById('closeReasonInput').value = '止损平仓';
                document.getElementById('closeConfirmBtn').textContent = `确认限价平仓 @ $${presetPrice.toFixed(2)}`;
            }
        }

        function closeCloseModal() {
            document.getElementById('closeModal').classList.remove('show');
            closeCtx = null;
        }

        async function executeClose() {
            if (!closeCtx) return;
            const { sym, absQty } = closeCtx;
            const errEl = document.getElementById('closeError');
            errEl.style.display = 'none';

            const qVal = document.getElementById('closeQtyInput').value.trim();
            const closeQty = qVal === '' ? 0 : parseInt(qVal);
            if (qVal !== '' && (isNaN(closeQty) || closeQty <= 0 || closeQty > absQty)) {
                errEl.textContent = `数量无效: 请输入 1~${absQty} 的整数`;
                errEl.style.display = 'block';
                return;
            }

            const reason = document.getElementById('closeReasonInput').value.trim();
            if (!reason) {
                errEl.textContent = '请填写理由';
                errEl.style.display = 'block';
                return;
            }

            const btn = document.getElementById('closeConfirmBtn');
            btn.disabled = true;
            btn.textContent = '提交中...';

            try {
                const limitPrice = parseFloat(document.getElementById('closePriceInput').value) || null;
                const body = { symbol: sym, reason, quantity: closeQty };
                if (limitPrice && limitPrice > 0) body.price = limitPrice;
                const res = await fetch('/api/orders/close', {
                    method: 'POST', headers: {'Content-Type':'application/json'},
                    body: JSON.stringify(body),
                });
                const d = await res.json();
                const result = parseApiResult(d);
                closeCloseModal();
                if (result.ok) {
                    const data = result.data || {};
                    const isFull = closeQty === 0 || closeQty >= absQty;
                    const priceLabel = limitPrice ? ` @ $${limitPrice.toFixed(2)}` : '';
                    showResult('✅', `${sym} ${isFull ? '全平' : '部分平仓'}成功${priceLabel}!`);
                    loadPositions(); loadOrders();
                    // 延迟获取归因 (等后端写入完成)

                } else {
                    showResult('❌', result.error || '平仓失败');
                }
            } catch (e) {
                closeCloseModal();
                showResult('❌', e.message);
            } finally {
                btn.disabled = false;
                btn.textContent = '确认平仓';
            }
        }

        function showResult(icon, msg) {
            document.getElementById('resultIcon').textContent = icon;
            document.getElementById('resultMsg').textContent = msg;
            document.getElementById('resultModal').classList.add('show');
        }


        let _pendingCancelId = null;
        function cancelOrder(id) {
            _pendingCancelId = id;
            document.getElementById('confirmText').textContent = `确认撤销订单 ${id}？`;
            document.getElementById('confirmModal').classList.add('show');
        }
        async function _executeCancel() {
            const id = _pendingCancelId;
            document.getElementById('confirmModal').classList.remove('show');
            if (!id) return;
            try {
                const res = await fetch('/api/orders/cancel', {
                    method: 'POST', headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({ order_id: id }),
                });
                const d = await res.json();
                const result = parseApiResult(d);
                if (result.ok) {
                    showMsg('ok', '✅ 已撤销');
                    // 立即更新本地订单状态, 不等 snapshot 轮询
                    for (var i = 0; i < _brokerOrders.length; i++) {
                        if (_brokerOrders[i].order_id === id) {
                            _brokerOrders[i].order_status = 'CANCELLED';
                            break;
                        }
                    }
                    renderUnifiedOrders();
                } else {
                    showMsg('err', `❌ ${result.error || '撤单失败'}`);
                }
                loadOrders();
            } catch (e) { showMsg('err', e.message); }
            _pendingCancelId = null;
        }

        // ══════════════════════════════════════════════════════
        //  Attention Priority Engine (注意力优先)
        // ══════════════════════════════════════════════════════
        function updateAttentionBanner(positions) {
            const banner = document.getElementById('attentionBanner');
            if (!positions.length) { banner.style.display = 'none'; return; }

            let topAlert = null, topScore = 0;
            for (const p of positions) {
                const sym = p.code || p.symbol;
                const uPnl = p.unrealized_pl || p.unrealized_pnl || 0;
                const cur = p.cur_price || p.current || 0;
                const cost = p.cost_price || p.cost || 0;
                const qty = Math.abs(p.qty);
                const isLong = p.direction === 'LONG';
                const slKey = sym + '|' + (isLong ? 'LONG' : 'SHORT');
                const slData = _slMap[slKey];
                const slDist = slData ? slData.sl_distance_pct : 999;
                const prevU = _prevPnl[sym] || 0;
                const pnlChange = prevU ? uPnl - prevU : 0;

                let score = 0, type = '', msg = '', action = '', actionFn = '';

                if (slDist < 2 && slDist > 0) {
                    score = 100; type = 'danger';
                    msg = '<strong>' + escHtml(sym.replace('US.','')) + '</strong> 距止损仅 ' + slDist.toFixed(1) + '%，随时可能触发';
                    action = '立即平仓'; actionFn = "closePos('" + escHtml(sym) + "'," + qty + ")";
                } else if (slDist < 5 && slDist > 0) {
                    score = 60; type = 'warning';
                    msg = '<strong>' + escHtml(sym.replace('US.','')) + '</strong> 距止损 ' + slDist.toFixed(1) + '%，注意风控';
                    action = '查看详情'; actionFn = "showDiag('" + escHtml(sym) + "', this)";
                }
                if (pnlChange < -100 && score < 80) {
                    score = 80; type = 'danger';
                    msg = '<strong>' + escHtml(sym.replace('US.','')) + '</strong> 盈亏快速恶化，15s 内变动 $' + pnlChange.toFixed(0);
                    action = '查看持仓'; actionFn = "showDiag('" + escHtml(sym) + "', this)";
                }
                const pnlPct = cost > 0 && cur > 0 ? ((isLong ? (cur-cost) : (cost-cur)) / cost * 100) : 0;
                if (pnlPct > 10 && score < 50) {
                    score = 50; type = 'opportunity';
                    msg = '<strong>' + escHtml(sym.replace('US.','')) + '</strong> 浮盈 +' + pnlPct.toFixed(1) + '%（$' + uPnl.toFixed(0) + '），考虑锁利';
                    action = '平半仓'; actionFn = "closePos('" + escHtml(sym) + "'," + qty + ",true)";
                }
                if (score > topScore) { topScore = score; topAlert = {type, msg, action, actionFn}; }
            }

            if (topAlert && topScore >= 30) {
                banner.style.display = 'block';
                const icon = topAlert.type === 'danger' ? '🔴' : topAlert.type === 'warning' ? '🟡' : '💰';
                banner.innerHTML = '<div class="attn-banner ' + topAlert.type + '">' +
                    '<span class="attn-icon">' + icon + '</span>' +
                    '<span class="attn-text">' + topAlert.msg + '</span>' +
                    '<span class="attn-action"><button class="attn-btn" onclick="' + topAlert.actionFn + '">' + topAlert.action + '</button></span></div>';
            } else {
                banner.style.display = 'none';
            }
        }

        // ══════════════════════════════════════════════════════
        //  Optimized Polling — 2 endpoints + Visibility API
        // ══════════════════════════════════════════════════════

        async function loadTradeSnapshot() {
            try {
                const res = await fetch('/api/trade/snapshot');
                if (!res.ok) { console.error('snapshot HTTP error:', res.status); return; }
                const d = await res.json();
                console.log('[snapshot]', 'positions:', (d.positions||[]).length, 'orders:', (d.orders||[]).length, 'account:', !!d.account);

                const positions = (d.positions || []).filter(p => p.qty !== 0);
                document.getElementById('posCount').textContent = `(${positions.length})`;

                // ── 更新顶部摘要条 ──
                const acc = d.account || {};
                const totalAssets = acc.total_assets || acc.nav || 0;
                const power = acc.power || acc.buying_power || 0;
                const marginPct = acc.margin_ratio || acc.margin_usage || 0;
                const todayPnl = (d.pnl || {}).today_pl || 0;
                const unrealPnl = positions.reduce((s, p) => s + (p.unrealized_pl || p.unrealized_pnl || 0), 0);
                const displayPnl = todayPnl || unrealPnl;
                const displayPnlPct = totalAssets > 0 ? (displayPnl / totalAssets * 100) : 0;

                document.getElementById('tsAssets').textContent = '$' + (totalAssets >= 1000 ? (totalAssets/1000).toFixed(1) + 'K' : totalAssets.toFixed(0));
                document.getElementById('tsPower').textContent = '$' + (power >= 1000 ? (power/1000).toFixed(1) + 'K' : power.toFixed(0));
                // 同步账户净值到智能建议引擎
                if (totalAssets > 0) setEquityCache(totalAssets);

                // 持仓盈亏
                const posPnlEl = document.getElementById('tsPosPnl');
                const posPnlPctEl = document.getElementById('tsPosPnlPct');
                posPnlEl.textContent = (unrealPnl >= 0 ? '+$' : '-$') + Math.abs(unrealPnl).toFixed(0);
                posPnlEl.className = 'ts-val ' + (unrealPnl >= 0 ? 'green' : 'red');
                posPnlPctEl.textContent = (unrealPnl >= 0 ? '+' : '') + (totalAssets > 0 ? (unrealPnl / totalAssets * 100).toFixed(1) : '0') + '%';
                posPnlPctEl.className = 'ts-sub ' + (unrealPnl >= 0 ? 'green' : 'red');

                // 今日盈亏
                const pnlEl = document.getElementById('tsPnl');
                const pnlPctEl = document.getElementById('tsPnlPct');
                pnlEl.textContent = (displayPnl >= 0 ? '+$' : '-$') + Math.abs(displayPnl).toFixed(0);
                pnlEl.className = 'ts-val ' + (displayPnl >= 0 ? 'green' : 'red');
                pnlPctEl.textContent = (displayPnl >= 0 ? '+' : '') + displayPnlPct.toFixed(1) + '%';
                pnlPctEl.className = 'ts-sub ' + (displayPnl >= 0 ? 'green' : 'red');

                // 次要指标
                const marketVal = (d.account || {}).market_val || 0;
                const cash = (d.account || {}).cash || 0;
                const frozen = (d.account || {}).frozen_cash || 0;
                document.getElementById('tsMarketVal').textContent = '$' + (marketVal >= 1000 ? (marketVal/1000).toFixed(1) + 'K' : marketVal.toFixed(0));
                document.getElementById('tsCash').textContent = '$' + (cash >= 1000 ? (cash/1000).toFixed(1) + 'K' : cash.toFixed(0));
                document.getElementById('tsFrozen').textContent = '$' + frozen.toFixed(0);
                document.getElementById('tsMarginUsage').textContent = marginPct.toFixed(1) + '%';

                // 数据新鲜度
                const dot = document.querySelector('.ts-dot');
                if (dot) { dot.classList.remove('stale'); }
                _failCount = 0;
                // 清除之前的错误提示
                const errRow = document.querySelector('.api-error-row');
                if (errRow) errRow.remove();

                _slMap = {};
                (d.stop_distances || []).forEach(s => {
                    _slMap[(s.symbol||'') + '|' + (s.direction||'')] = s;
                });

                const tbody = document.getElementById('posBody');
                if (!positions.length) {
                    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#8b949e;padding:20px">无持仓</td></tr>';
                } else {
                    const totalMv = positions.reduce((s, x) => s + (x.market_val || x.market_value || Math.abs(x.qty) * (x.cur_price || x.current || 0)), 0);
                    tbody.innerHTML = positions.map(p => {
                        const isLong = p.direction === 'LONG';
                        const cost = p.cost_price || p.cost || 0;
                        const cur = p.cur_price || p.current || 0;
                        const qty = Math.abs(p.qty);
                        const mv = p.market_val || p.market_value || qty * cur;
                        const tPnl = p.today_pl || 0;
                        const uPnl = p.unrealized_pl || p.unrealized_pnl || 0;
                        const uPnlPct = cost > 0 ? ((cur - cost) / cost * 100 * (isLong ? 1 : -1)) : 0;
                        const tPnlPct = cost > 0 ? (tPnl / (qty * cost) * 100) : 0;
                        const concPct = totalMv > 0 ? (mv / totalMv * 100) : 0;
                        const concColor = concPct > 30 ? '#f87171' : concPct > 15 ? '#fbbf24' : '#58a6ff';
                        const sym = p.code || p.symbol;
                        const codeShort = sym.replace('US.', '');
                        const tCls = tPnl >= 0 ? 'pnl-pos' : 'pnl-neg';
                        const uCls = uPnl >= 0 ? 'pnl-pos' : 'pnl-neg';
                        // 做空: 数量和市值显示负数
                        const qtyStr = isLong ? qty + '股' : '-' + qty + '股';
                        const mvStr = isLong
                            ? (mv >= 1000 ? '$'+(mv/1000).toFixed(1)+'K' : '$'+mv.toFixed(0))
                            : (mv >= 1000 ? '-$'+(mv/1000).toFixed(1)+'K' : '-$'+mv.toFixed(0));
                        const mvColor = isLong ? '#e1e4e8' : '#f87171';
                        const qtyColor = isLong ? '#8b949e' : '#f87171';
                        // PnL 趋势
                        const prevU = _prevPnl[sym];
                        const pnlDelta = prevU !== undefined ? uPnl - prevU : 0;
                        const trendIcon = prevU === undefined ? '' : pnlDelta > 5 ? ' <span style="color:#4ade80;font-size:11px" title="盈利改善 +$'+pnlDelta.toFixed(0)+'">↑</span>' : pnlDelta < -5 ? ' <span style="color:#f87171;font-size:11px" title="盈利恶化 $'+pnlDelta.toFixed(0)+'">↓</span>' : ' <span style="color:#8b949e;font-size:11px" title="横盘">→</span>';
                        _prevPnl[sym] = uPnl;
                        // Signal light
                        const slKey = sym + '|' + (isLong ? 'LONG' : 'SHORT');
                        const slData = _slMap[slKey];
                        const sig = computeSignal(p, slData);
                        _diagData[sym] = {pos: p, slData, signal: sig, qty, isLong};
                        // 点击代码跳转交易页面并带入代码+数量
                        const tradeUrl = '/trade?symbol=' + encodeURIComponent(sym) + '&qty=' + qty;
                        return `<tr onclick="showDiag('${escHtml(sym)}', this)" style="cursor:pointer">
                            <td style="white-space:nowrap">
                                <a href="${tradeUrl}" style="color:#58a6ff;text-decoration:none;font-weight:700" onclick="event.stopPropagation()">${escHtml(codeShort)}</a>
                                <span class="sig ${sig.color}" style="width:10px;height:10px;vertical-align:middle;margin-left:6px" title="${sig.action}"></span>
                                ${p.legacy ? `<span title="broker has position but system not managing" onclick="event.stopPropagation();takeoverPos('${sym}')" style="display:inline-block;font-size:9px;padding:1px 5px;margin-left:4px;background:#7c2d12;color:#fed7aa;border:1px solid #c2410c;border-radius:3px;cursor:pointer;font-weight:700">未管理</span>` : ''}
                            </td>
                            <td style="text-align:right;white-space:nowrap">
                                <div style="font-weight:600;color:${mvColor}">${mvStr}</div>
                                <div style="font-size:11px;color:${qtyColor}">${qtyStr}</div>
                            </td>
                            <td style="text-align:right;white-space:nowrap">
                                <div style="font-weight:600">${cur > 0 ? '$'+cur.toFixed(2) : '--'}</div>
                                <div style="font-size:11px;color:#8b949e">/$${cost.toFixed(2)}</div>
                            </td>
                            <td style="text-align:right;white-space:nowrap" class="${tCls}">
                                <div>${tPnl ? (tPnl>=0?'+$':'-$')+Math.abs(tPnl).toFixed(2) : '--'}</div>
                                <div style="font-size:11px">${tPnl ? (tPnl>=0?'+':'')+tPnlPct.toFixed(1)+'%' : ''}</div>
                            </td>
                            <td style="text-align:right;white-space:nowrap" class="${uCls}">
                                <div>${uPnl>=0?'+$':'-$'}${Math.abs(uPnl).toFixed(2)}${trendIcon}</div>
                                <div style="font-size:11px">${(uPnl>=0?'+':'')+uPnlPct.toFixed(1)}%</div>
                            </td>
                            <td style="text-align:center;white-space:nowrap">
                                <div style="font-size:12px;font-weight:600">${concPct.toFixed(1)}%</div>
                                <div style="height:3px;background:#21262d;border-radius:2px;margin-top:3px"><div style="height:100%;width:${Math.min(concPct,100)}%;background:${concColor};border-radius:2px"></div></div>
                            </td>
                        </tr>`;
                    }).join('');
                }

                // ── 注意力优先引擎: 扫描持仓，置顶最紧急事项 ──
                updateAttentionBanner(positions);

                // Orders — store and render unified with audit
                _brokerOrders = d.orders || [];
                _auditOrders = (d.audit && d.audit.orders) ? d.audit.orders : [];
                renderUnifiedOrders();
            } catch (e) {
                console.error('Trade snapshot error:', e);
                _failCount++;
                const dot = document.querySelector('.ts-dot');
                if (dot) dot.classList.add('stale');
                if (_failCount >= _MAX_FAILS_BEFORE_BANNER) {
                    const posBody = document.getElementById('posBody');
                    if (posBody && !posBody.querySelector('.api-error-row')) {
                        posBody.innerHTML = '<tr class="api-error-row"><td colspan="6" style="text-align:center;color:#f87171;padding:20px">⚠️ 数据加载失败 (已重试 ' + _failCount + ' 次) — 请检查网络连接</td></tr>';
                    }
                }
            }
        }

        // ── slow-data removed: decision quality moved to dashboard deep review ──

        // ── Visibility-Aware Polling ──
        let _fastTimer = null, _slowTimer = null;
        let _isPolling = false;
        let _failCount = 0;
        const _MAX_FAILS_BEFORE_BANNER = 3;  // 连续失败3次(45s)后显示错误提示

        function startPolling() {
            if (_isPolling) return;
            _isPolling = true;
            loadTradeSnapshot();
            // WS 在线: 30s safety net, 离线: 15s (交易页需要更快)
            var fastInterval = (window.WSManager && WSManager.isConnected()) ? 30000 : 15000;
            _fastTimer = setInterval(function() {
                // 动态调整: WS 状态变化时切换间隔
                var desired = (window.WSManager && WSManager.isConnected()) ? 30000 : 15000;
                if (desired !== fastInterval) {
                    fastInterval = desired;
                    clearInterval(_fastTimer);
                    _fastTimer = setInterval(arguments.callee, desired);
                }
                // WS 10s 内推送过 → 跳过 HTTP
                if (typeof _tradeWsLastPush !== 'undefined' && _tradeWsLastPush && (Date.now() - _tradeWsLastPush < 10000)) return;
                loadTradeSnapshot();
            }, fastInterval);
        }

        function stopPolling() {
            _isPolling = false;
            if (_fastTimer) { clearInterval(_fastTimer); _fastTimer = null; }
            if (_slowTimer) { clearInterval(_slowTimer); _slowTimer = null; }
        }

        document.addEventListener('visibilitychange', () => {
            if (document.hidden) stopPolling();
            else startPolling();
        });

        const loadPositions = loadTradeSnapshot;
        const loadOrders = loadTradeSnapshot;

        // ── 统一交易记录: 合并 Broker 订单 + 系统操作记录 ──
        var _brokerOrders = [];
        var _auditOrders = [];

        function toggleOrders() {
            var body = document.getElementById('ordersBody');
            var arrow = document.getElementById('ordersArrow');
            if (body.style.display === 'none') {
                body.style.display = 'block';
                arrow.textContent = '▼';
            } else {
                body.style.display = 'none';
                arrow.textContent = '▶';
            }
        }

        function renderUnifiedOrders() {
            var tbody = document.getElementById('ordersBodyTable');
            if (!tbody) return;

            // 统一为同一格式的行数组, 按时间倒序
            var rows = [];

            // Broker 订单
            var brokerList = _brokerOrders || [];
            for (var i = 0; i < brokerList.length; i++) {
                var o = brokerList[i];
                var stStr = String(o.order_status || '').toUpperCase();
                var isFilled = stStr.indexOf('FILLED') >= 0 || stStr === 'DEALT';
                var isCancel = stStr.indexOf('CANCEL') >= 0 || stStr.indexOf('FAIL') >= 0
                    || stStr.indexOf('REJECT') >= 0 || stStr.indexOf('DELETED') >= 0
                    || stStr.indexOf('DISABLED') >= 0 || stStr.indexOf('EXPIRED') >= 0;
                var canCancel = !isFilled && !isCancel && stStr.length > 0;
                var stColor = isFilled ? '#4ade80' : isCancel ? '#f87171' : '#fbbf24';
                var sideStr = String(o.trd_side || '').toUpperCase();
                var sideLabel = sideStr === 'BUY' || sideStr === 'B'
                    ? '<span style="color:#4ade80;font-weight:600">买入</span>'
                    : '<span style="color:#f87171;font-weight:600">卖出</span>';
                var priceStr = o.price > 0 ? '$' + o.price.toFixed(2) : '市价';
                if (o.dealt_avg_price > 0 && isFilled) priceStr = '$' + o.dealt_avg_price.toFixed(2);
                var timeStr = escHtml((o.create_time || '').substring(11, 19) || '--');
                // 时长: 已终结订单显示从提交到成交/取消的耗时
                var durStr = '';
                if ((isFilled || isCancel) && o.create_time && o.update_time) {
                    try {
                        var t0 = new Date(o.create_time.replace(' ', 'T'));
                        var t1 = new Date(o.update_time.replace(' ', 'T'));
                        var sec = Math.round((t1 - t0) / 1000);
                        if (sec >= 0 && sec < 86400) {
                            durStr = sec < 60 ? sec + 's' : Math.floor(sec/60) + 'm' + (sec%60) + 's';
                        }
                    } catch(_e) {}
                }
                var note = '';
                if (o.remark) note = escHtml(String(o.remark).substring(0, 30));
                var oid = String(o.order_id || '');
                var cancelBtn = canCancel && oid
                    ? '<button class="btn-cancel" style="font-size:11px;padding:4px 10px;min-height:auto" onclick="event.stopPropagation();cancelOrder(\'' + escHtml(oid) + '\')">撤单</button>'
                    : (isFilled ? '<span style="font-size:11px;color:#4ade80">已成交</span>' : '');
                var sortKey = o.create_time || '';
                var qtyDisplay = o.filled_qty || o.dealt_qty || o.qty || 0;
                rows.push({
                    sortKey: sortKey,
                    html: '<tr>' +
                        '<td style="font-size:11px;color:#8b949e;white-space:nowrap">' + timeStr +
                            (durStr ? ' <span style="font-size:10px;color:#58a6ff">' + durStr + '</span>' : '') +
                        '</td>' +
                        '<td><b>' + escHtml(o.code || '') + '</b></td>' +
                        '<td>' + sideLabel + '</td>' +
                        '<td style="text-align:right">' + qtyDisplay + '</td>' +
                        '<td style="text-align:right;white-space:nowrap">' + priceStr + '</td>' +
                        '<td style="color:' + stColor + ';font-weight:600;font-size:12px">' + escHtml(stStr || o.order_status || '--') + '</td>' +
                        '<td style="font-size:11px;color:#8b949e">' + note + '</td>' +
                        '<td style="white-space:nowrap">' + cancelBtn + '</td>' +
                        '</tr>'
                });
            }

            // 系统操作记录 (audit)
            var auditList = _auditOrders || [];
            // 仅显示最新交易日
            var latestDate = '';
            for (var j = 0; j < auditList.length; j++) {
                var d = (auditList[j].timestamp || '').substring(0, 10);
                if (d > latestDate) latestDate = d;
            }
            for (var k = 0; k < auditList.length; k++) {
                var a = auditList[k];
                if (latestDate && (a.timestamp || '').substring(0, 10) !== latestDate) continue;
                var aTime = escHtml((a.timestamp || '').substring(11, 19) || '--');
                var aSc = (a.status === 'submitted' || a.status === 'filled') ? '#4ade80' : a.status === 'rejected' ? '#f87171' : '#fbbf24';
                var aAction = escHtml(a.action || '--');
                var aNote = escHtml((a.reason || '').substring(0, 40));
                if (a.error_msg) aNote += (aNote ? ' ' : '') + '<span style="color:#f87171">' + escHtml(a.error_msg.substring(0, 25)) + '</span>';
                var aSym = escHtml((a.symbol || '').replace('US.', ''));
                rows.push({
                    sortKey: a.timestamp || '',
                    html: '<tr style="opacity:0.75">' +
                        '<td style="font-size:11px;color:#8b949e;white-space:nowrap">' + aTime + '</td>' +
                        '<td><b>' + aSym + '</b> <span style="font-size:9px;color:#8b949e">系统</span></td>' +
                        '<td>' + aAction + '</td>' +
                        '<td style="text-align:right">' + escHtml(String(a.quantity || '')) + '</td>' +
                        '<td style="text-align:right;font-size:11px;color:#8b949e">' + escHtml(a.order_type || '') + '</td>' +
                        '<td style="color:' + aSc + ';font-weight:600;font-size:12px">' + escHtml(a.status) + '</td>' +
                        '<td style="font-size:11px;color:#8b949e">' + aNote + '</td>' +
                        '<td></td>' +
                        '</tr>'
                });
            }

            // 按时间倒序排列, 最多显示最新 30 条 (防止无限累加)
            rows.sort(function(a, b) { return b.sortKey > a.sortKey ? 1 : b.sortKey < a.sortKey ? -1 : 0; });
            var MAX_ROWS = 30;
            var totalRows = rows.length;
            if (rows.length > MAX_ROWS) rows = rows.slice(0, MAX_ROWS);

            // Badge
            var badge = document.getElementById('ordersBadge');
            if (badge) {
                var activeCnt = brokerList.filter(function(o) {
                    return !o.order_status.includes('FILLED') && !o.order_status.includes('CANCEL') && !o.order_status.includes('FAILED');
                }).length;
                var filledCnt = brokerList.filter(function(o) { return o.order_status.includes('FILLED'); }).length;
                var parts = [];
                if (activeCnt > 0) parts.push(activeCnt + ' 活跃');
                if (filledCnt > 0) parts.push(filledCnt + ' 成交');
                if (auditList.length > 0) parts.push(auditList.length + ' 系统');
                if (totalRows > MAX_ROWS) parts.push('显示最新 ' + MAX_ROWS + '/' + totalRows);
                badge.textContent = rows.length ? '(' + parts.join(' · ') + ')' : '';
            }

            if (!rows.length) {
                tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#8b949e">暂无记录</td></tr>';
            } else {
                tbody.innerHTML = rows.map(function(r) { return r.html; }).join('');
            }
        }

        // loadAudit: audit data now comes from snapshot
        // loadManualPerf: removed

        // ── WebSocket 实时事件消费 ──
        if (window.WSManager) {
            // 订单状态变更 → 立即刷新持仓+订单
            WSManager.on('order_update', function(data) {
                if (data.status === 'submitted') {
                    showMsg('ok', '📡 ' + (data.symbol || '') + ' ' + (data.side || '') + ' ' + (data.qty || '') + '股 订单已提交');
                } else if (data.status === 'error') {
                    showMsg('err', '📡 订单失败: ' + (data.symbol || '') + ' ' + (data.error || ''));
                } else if (data.status === 'filled') {
                    showMsg('ok', '📡 ' + (data.symbol || '') + ' 订单已成交');
                }
                loadTradeSnapshot();
            });
            // 成交通知 → 刷新 + 提示
            WSManager.on('fill', function(data) {
                showMsg('ok', '✅ 成交: ' + (data.symbol || '') + ' ' + (data.side || '') + ' ' + (data.qty || '') + '股 @ $' + (data.price || '?'));
                loadTradeSnapshot();
            });
            // 持仓变动 → 刷新
            WSManager.on('position_update', function(data) {
                loadTradeSnapshot();
            });

            // DataService 缓存刷新推送 — 即时刷新交易页
            var _tradeWsLastPush = 0;
            WSManager.on('positions_update', function() {
                _tradeWsLastPush = Date.now();
                loadTradeSnapshot();
            });
            WSManager.on('account_update', function() {
                _tradeWsLastPush = Date.now();
                loadTradeSnapshot();
            });
        }

        // Init
        startPolling();

        // URL 参数预填 (从作战计划/股详页跳转过来)
        (function() {
            const params = new URLSearchParams(window.location.search);
            const sym = params.get('symbol');
            const dir = params.get('direction');
            const qty = params.get('qty');
            const orderType = params.get('order_type');
            const price = params.get('price');
            const op = params.get('op');
            // 仓位参数 (Task 13/14: 从作战计划跳转过来时携带)
            const entryParam = params.get('entry');
            const slParam    = params.get('sl');
            const tpParam    = params.get('tp');
            const reasonParam = params.get('reason');  // ADR-007: 作战计划入场理由

            if (sym) {
                document.getElementById('symbol').value = sym;
            }
            if (dir) setDir(dir.toUpperCase());
            if (op)  setOp(op.toUpperCase());
            if (orderType) {
                const otEl = document.getElementById('orderType');
                if (otEl) { otEl.value = orderType.toUpperCase(); otEl.dispatchEvent(new Event('change')); }
            }
            if (price) {
                const priceEl = document.getElementById('limitPrice');
                if (priceEl) priceEl.value = parseFloat(price);
            }
            if (qty) {
                const q = parseInt(qty);
                if (q > 0) setQty(q);
            }

            // 如果携带了 entry/sl 参数 → 直接根据参数计算并显示建议行
            // (这是从 battle_plan 跳转过来的 "ready to submit" 状态)
            // P1-U1: 只要有 sl 参数就展开 SL/TP 字段 (不要求 entry)
            if (sym && slParam) {
                const entry = parseFloat(entryParam);
                const sl    = parseFloat(slParam);
                const tp    = parseFloat(tpParam) || 0;
                if (entry > 0 && sl > 0) {
                    // 预填止损/止盈到表单 (2026-06-16)
                    const slEl = document.getElementById('stopLoss');
                    const tpEl = document.getElementById('takeProfit');
                    const slTpRow = document.getElementById('slTpRow');
                    if (slEl) slEl.value = sl.toFixed(2);
                    if (tpEl && tp > 0) tpEl.value = tp.toFixed(2);
                    if (slTpRow) slTpRow.style.display = 'flex';

                    // 延迟到 _equityCache 就绪后计算 (snapshot 加载完)
                    const tryCompute = function() {
                        const direction = (dir || curDir).toUpperCase();
                        const s = computePosition({
                            entry: entry, sl: sl, tp: tp, direction: direction,
                            equity: _equityCache,
                            riskPct: _riskCfg.riskPct, maxPct: _riskCfg.maxPct,
                        });
                        if (s) {
                            s.source = 'battle_plan';
                            s.sourceLabel = '来自作战计划';
                            if (reasonParam) s.planRationale = reasonParam;
                            _curSuggestion = s;
                            // URL 明确携带 qty: 如果 URL qty 与计算不一致说明用户手动调过
                            const urlQty = qty ? parseInt(qty) : 0;
                            _userOverridden = (urlQty > 0 && urlQty !== s.shares);
                            renderSuggestion();
                            return true;
                        }
                        return false;
                    };
                    if (!tryCompute()) {
                        // equity 尚未到位，轮询几次 (最多 6s)
                        let tries = 0;
                        const iv = setInterval(function() {
                            tries += 1;
                            if (tryCompute() || tries >= 12) clearInterval(iv);
                        }, 500);
                    }
                }
            }

            // 触发报价与作战计划动作
            if (sym) {
                fetchQuote();
                // 仅携带 symbol (未携带 entry+sl) → 从作战计划调取参数
                if (!(entryParam && slParam)) {
                    autoFillFromPlan(sym, { fillForm: !qty /* URL已定股数不覆盖 */ });
                }
            }
        })();

        // ── Expose handlers to window for inline HTML event handlers ──
        Object.assign(window, {
            cancelOrder, clearSlTp, closeCloseModal, closePos, closePosAtPrice, closePreview, _executeCancel, executeClose, executeOrder, fetchQuote, hideDiag, loadOrders, loadPositions, onQuantityInput, pctQty, previewOrder, restoreSuggestion, setDir, setMaxPct, setOp, setQty, setRiskPct, showDiag, stepQty, takeoverPos, toggleOrders, toggleRiskCfg
        });

})();
