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
        // B1 (2026-09-08 REVIEW-20260908): 手动下单闸门只读快照 (/api/trade/gates)。
        // 声明在顶部以避免 TDZ —— updateEstimate 早于轮询段执行也要能安全读。
        let _gates = null;
        let _gatesTimer = null;
        // 服务端下发的 runtime/capabilities — 前端只消费, 不自行猜测能否交易。
        let _runtime = null;
        const _riskCfg = { riskPct: 0.01, maxPct: 0.05 };
        const isLive = (window.__CONFIG__ && window.__CONFIG__.isLive) || false;

        const _RUNTIME_LABELS = {
            broker: {connected:'券商已连接', degraded:'券商数据降级', disconnected:'券商不可用', unknown:'券商未确认'},
            quotes: {live:'报价实时', degraded:'报价降级', unknown:'报价逐单校验'},
            positions: {live:'持仓实时', degraded:'持仓为本地兜底', unknown:'持仓未确认'},
            orders: {live:'订单实时', degraded:'订单为本地审计', unknown:'订单未确认'},
            market: {regular:'盘中', pre:'盘前', after:'盘后', closed:'休市', unknown:'时段未知'}
        };

        function _runtimeCan(action) {
            return !!(_runtime && _runtime.capabilities && _runtime.capabilities[action] === true);
        }

        function _runtimeLabel(kind, value) {
            return (_RUNTIME_LABELS[kind] && _RUNTIME_LABELS[kind][value]) || value || '未知';
        }

        function _formatEtTime(iso) {
            if (!iso) return '';
            const d = new Date(iso);
            if (isNaN(d.getTime())) return '';
            try {
                return d.toLocaleTimeString('en-US', {
                    timeZone: 'America/New_York', hour12: false,
                    hour: '2-digit', minute: '2-digit', second: '2-digit'
                }) + ' ET';
            } catch (e) { return ''; }
        }

        function _runtimeGuard(action, modal) {
            if (_runtimeCan(action)) return true;
            const name = {place:'开仓', reduce:'平仓/减仓', cancel:'撤单', modify_stop:'修改止损'}[action] || action;
            let msg = '交易状态确认中，请稍候';
            if (_runtime) {
                const reason = (_runtime.reasons || [])[0];
                msg = `只读：${name}当前不可用${reason ? ' — ' + reason : ''}`;
            }
            if (modal) showResult('🔒', msg); else showMsg('err', msg);
            return false;
        }

        function _setRuntimeButton(id, allowed, reason, opts) {
            const btn = document.getElementById(id);
            if (!btn) return;
            opts = opts || {};
            // 允许态不要覆盖价格偏离勾选/提交中的临时禁用; 禁止态永远优先。
            if (allowed && (btn.dataset.busy === '1' || opts.enableWhenAllowed === false)) return;
            btn.disabled = !allowed;
            btn.title = allowed ? '' : (reason || '当前运行状态不允许此动作');
        }

        function applyRuntimeState(rt) {
            _runtime = rt || null;
            const banner = document.getElementById('runtimeBanner');
            const caps = (_runtime && _runtime.capabilities) || {};
            _setRuntimeButton('btnSubmit', caps.place === true, (_runtime && (_runtime.reasons || [])[0]) || '');
            _setRuntimeButton('confirmBtn', caps.place === true, (_runtime && (_runtime.reasons || [])[0]) || '', {enableWhenAllowed:false});
            _setRuntimeButton('closeConfirmBtn', caps.reduce === true, (_runtime && (_runtime.reasons || [])[0]) || '', {enableWhenAllowed:false});
            if (!banner || !_runtime) return;

            const broker = _runtime.broker || 'unknown';
            const tone = (broker === 'disconnected' || broker === 'unknown') ? 'danger'
                : (broker === 'degraded' || _runtime.positions === 'degraded'
                    || _runtime.orders === 'degraded' || _runtime.quotes === 'degraded'
                    || caps.place === false) ? 'warn' : 'ok';
            const parts = [
                _runtimeLabel('quotes', _runtime.quotes),
                _runtimeLabel('positions', _runtime.positions),
                _runtimeLabel('orders', _runtime.orders),
                _runtimeLabel('market', _runtime.market)
            ];
            const timeLabel = _formatEtTime(_runtime.as_of);
            const checkedAt = _formatEtTime(_runtime.runtime_as_of);
            const reason = (_runtime.reasons || [])[0] || '';
            const readOnly = broker === 'disconnected' || broker === 'unknown';
            banner.className = 'runtime-banner ' + tone;
            banner.title = (_runtime.reasons || []).join('\n');
            banner.innerHTML = '<span class="runtime-dot"></span>'
                + '<b>' + (readOnly ? '只读模式 · ' : '') + escHtml(_runtimeLabel('broker', broker)) + '</b>'
                + '<span class="runtime-sep">·</span>'
                + parts.map(function(p) { return '<span>' + escHtml(p) + '</span>'; }).join('<span class="runtime-sep">·</span>')
                + (timeLabel ? '<span class="runtime-sep">·</span><span>数据 ' + escHtml(timeLabel) + '</span>'
                    : (checkedAt ? '<span class="runtime-sep">·</span><span>校验 ' + escHtml(checkedAt) + '</span>' : ''))
                + (reason ? '<span class="runtime-sep">·</span><span class="runtime-reason">' + escHtml(reason) + '</span>' : '');
            // 横幅与摘要条的新鲜度必须同源；断线时不能再显示绿色“实时”。
            if (readOnly) {
                const dot = document.querySelector('.ts-dot');
                if (dot) { dot.classList.add('stale'); dot.classList.remove('degraded'); }
                const freshEl = document.getElementById('tsFreshText');
                if (freshEl) {
                    freshEl.textContent = '离线';
                    freshEl.style.color = '#f87171';
                    freshEl.title = '券商连接不可用；仅展示最近缓存/本地台账';
                }
            }
        }

        function showRuntimeUnavailable(reason) {
            applyRuntimeState({
                broker:'unknown', account:'unknown', quotes:'unknown', positions:'unknown',
                orders:'unknown', market:'unknown', as_of:null,
                runtime_as_of:new Date().toISOString(),
                capabilities:{place:false, reduce:false, cancel:false, modify_stop:false},
                reasons:[reason || '交易状态读取失败；已切换只读']
            });
        }

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
                autoHint.textContent = `已自动开启 — 当前处于${phase === 'PRE_MARKET' ? '盘前' : '盘后'}时段`;
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
                    closedHint.textContent = getNextSessionText(phase, etMinutes) + ' · 已开启盘前/盘后成交';
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
            if (!_runtimeGuard('modify_stop', false)) return;
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
            // B10 (批3): 换标的即作废上一只票的报价/购买力快照。
            //   不作废 ⇒ 新报价到达前点"预览下单", 预估金额与限价偏离检查会拿
            //   **上一只票的价格**当基准 (与港股页 A1 同族缺陷)。宁可短暂显示 "--"
            //   (updateEstimate 会退回"请先获取报价"), 也不给一个错得很像样的数字。
            curQuote = null;
            curCap = null;
            // 报价与plan并发; plan会设置股数/价格/方向
            fetchQuote();
            autoFillFromPlan(sym);
        }

        // ── B10 (2026-09-08 REVIEW-20260908 批3): 最近标的 chips (localStorage) ──
        // 港股页早有这排 chips (#recent-chips), 美股页此前零 localStorage (实测 grep 无命中),
        // 只能靠 URL 参数带代码 —— 连续复盘/加仓同一批票时每次都要重打代码。
        // **只存代码本身** (5 枚, 去重, 最新在前): 不存价格/股数/方向, 免得把过期数字带回表单。
        // 点击走 onSymbolChanged() —— 与"手输代码 + 失焦"完全同一条路径, 不另开分支。
        const RECENT_SYMBOLS_KEY = 'us_recent_symbols';
        function _recentSymbols() {
            try {
                const arr = JSON.parse(localStorage.getItem(RECENT_SYMBOLS_KEY) || '[]');
                return Array.isArray(arr) ? arr.filter(x => typeof x === 'string' && x.length > 0) : [];
            } catch (e) {
                return [];   // localStorage 被禁用 / JSON 损坏 ⇒ chips 不显示, 绝不影响下单主流程
            }
        }
        function saveRecentSymbol(sym) {
            if (!sym) return;
            try {
                const arr = [sym].concat(_recentSymbols().filter(x => x !== sym)).slice(0, 5);
                localStorage.setItem(RECENT_SYMBOLS_KEY, JSON.stringify(arr));
            } catch (e) { return; }
            renderRecentChips();
        }
        function renderRecentChips() {
            const box = document.getElementById('recentChips');
            if (!box) return;
            // data-sym + 事件委托 (下方 bindRecentChips), 不用内联 onclick 字符串拼接:
            // 标的字符串来自服务端, 走 escHtml 后放进属性值即可, 不进 JS 字面量。
            box.innerHTML = _recentSymbols().map(s =>
                `<button type="button" class="rec-chip" data-sym="${escHtml(s)}" title="点击填入 ${escHtml(s)}">${escHtml(s.replace(/^US\./, ''))}</button>`
            ).join('');
        }
        function pickRecentSymbol(sym) {
            const el = document.getElementById('symbol');
            if (!el || !sym) return;
            el.value = sym;
            onSymbolChanged();
        }
        function bindRecentChips() {
            const box = document.getElementById('recentChips');
            if (!box) return;
            box.addEventListener('click', function (ev) {
                const btn = ev.target.closest ? ev.target.closest('.rec-chip') : null;
                if (btn && btn.dataset.sym) pickRecentSymbol(btn.dataset.sym);
            });
            renderRecentChips();
        }

        // ── B11 (2026-09-08 REVIEW-20260908 §H, 用户裁「理由非必填」): 平仓理由一键 chips ──
        // 平仓是**最紧急动作**, 此前理由为空直接拦 ("请填写理由") = 在最该快的路径上加摩擦,
        // 且与港股页口径相反 (港股 2026-07-22 已裁「去掉理由必填」)。改法 = 保留痕、去掉打字:
        // chips 一点即填, 也**可以什么都不填** —— 留空由后端按 "手动平仓" 记账 (routes/trade.py),
        // 归因链不断。审计口径: 全仓零消费方按理由**文本**分支 (消费方都读 EXTERNAL_CLOSE/MANUAL
        // 这类枚举), 故 chips 词表只影响留痕可读性, 不影响任何判据。
        // 只给 4 枚实义 chips: 建议里的第 5 枚「其它」与下面的自由文本框完全重复 = 纯噪声。
        const CLOSE_REASONS = ['止损触发', '止盈达成', '计划变更', '风控降敞口'];
        function renderCloseReasonChips(active) {
            const box = document.getElementById('closeReasonChips');
            if (!box) return;
            box.innerHTML = CLOSE_REASONS.map(r =>
                `<button type="button" class="rec-chip${r === active ? ' on' : ''}" data-reason="${escHtml(r)}">${escHtml(r)}</button>`
            ).join('');
        }
        function bindCloseReasonChips() {
            const box = document.getElementById('closeReasonChips');
            if (!box) return;
            box.addEventListener('click', function (ev) {
                const btn = ev.target.closest ? ev.target.closest('.rec-chip') : null;
                if (!btn || !btn.dataset.reason) return;
                const inp = document.getElementById('closeReasonInput');
                if (inp) inp.value = btn.dataset.reason;   // 单一真源仍是 input ⇒ 提交路径逐字不变
                renderCloseReasonChips(btn.dataset.reason);
            });
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
                // B10 (批3): 两道标的校验 (报价响应带 symbol — manual_order_service.py:255 —
                //   但此前前端从不比对)。
                //   ① 在途响应过期: 请求发出后输入框已换成别的标的 ⇒ 整份丢弃, 既不写
                //      curQuote/curCap 也不渲染 —— 否则**后到的旧响应**会盖掉新标的的报价
                //      (5s 轮询与手输代码并发时真实可发生)。
                //   ② 回包标的与请求不符 ⇒ 同样丢弃, 但要说出来 (三态口径: 报错 ≠ 空)。
                const nowSym = document.getElementById('symbol').value.trim().toUpperCase();
                if (nowSym !== sym) return;
                if (q.symbol && String(q.symbol).toUpperCase() !== sym) {
                    curQuote = null;
                    curCap = null;
                    document.getElementById('quotePrice').textContent = '--';
                    document.getElementById('quoteDetail').textContent =
                        `⚠️ 报价标的不一致（请求 ${sym} / 回包 ${q.symbol}）— 已丢弃，请重新查询`;
                    return;
                }
                curCap = c;

                if (q.cur_price) {
                    curQuote = q;
                    // B10: 只在**报价成功**时记忆 (打错的代码不会污染 chips)
                    saveRecentSymbol(sym);
                    // 价格标签: 按时段显示盘前/盘后/夜盘
                    const {phase: qPhase} = isExtendedHours();
                    let priceLabel = '';
                    let priceColor = '#e1e4e8';
                    if (qPhase === 'PRE_MARKET' && q.pre_price) {
                        priceLabel = `盘前 $${q.pre_price.toFixed(2)}`;
                        priceColor = '#fbbf24';
                    } else if (qPhase === 'AFTER_HOURS' && q.after_price) {
                        priceLabel = `盘后 $${q.after_price.toFixed(2)}`;
                        priceColor = '#fbbf24';
                    } else if (qPhase === 'CLOSED' || qPhase === 'WEEKEND') {
                        if (q.overnight_price) { priceLabel = `夜盘 $${q.overnight_price.toFixed(2)}`; priceColor = '#a78bfa'; }
                        else if (q.after_price) { priceLabel = `盘后 $${q.after_price.toFixed(2)}`; priceColor = '#fbbf24'; }
                        else if (q.pre_price) { priceLabel = `盘前 $${q.pre_price.toFixed(2)}`; priceColor = '#fbbf24'; }
                    }
                    document.getElementById('quotePrice').innerHTML = `$${q.cur_price.toFixed(2)}` + (priceLabel ? ` <span style="font-size:12px;color:${priceColor};margin-left:8px">${priceLabel}</span>` : '');
                    const upd = q.update_time ? q.update_time.substring(11, 19) : '--';
                    // 买卖盘
                    const bidAsk = (q.bid_price && q.ask_price) ? ` | 买 ${q.bid_price.toFixed(2)} × ${q.bid_vol||0} / 卖 ${q.ask_price.toFixed(2)} × ${q.ask_vol||0}` : '';
                    document.getElementById('quoteDetail').innerHTML =
                        `高 $${q.high_price?.toFixed(2)||'--'} | 低 $${q.low_price?.toFixed(2)||'--'} | 昨收 $${q.prev_close?.toFixed(2)||'--'} | ${upd}${bidAsk}`;

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

        // ── 盘外时段价格基准 (2026-09-08 REVIEW B2) ──
        // 盘外 cur_price 是上次常规收盘价, 拿它算偏离既会假警报也会假安心;
        // 报价响应已带时段价 (manual_order_service.py:263-265), 与 quoteBox 展示同源选择。
        function _extPriceBasis(q, phase) {
            if (!q) return {price: 0, label: ''};
            if (phase === 'PRE_MARKET' && q.pre_price) return {price: q.pre_price, label: '盘前价'};
            if (phase === 'AFTER_HOURS' && q.after_price) return {price: q.after_price, label: '盘后价'};
            if (q.overnight_price) return {price: q.overnight_price, label: '夜盘价'};
            if (q.after_price) return {price: q.after_price, label: '盘后价'};
            if (q.pre_price) return {price: q.pre_price, label: '盘前价'};
            return {price: q.cur_price || 0, label: '上次收盘价'};
        }

        // ── B8 (2026-09-08 REVIEW-20260908 批3): 限价偏离警告的单一实现 ──
        // 原盘中 ≥10% / ≥5% 两分支各写一份 ~60 行内联 HTML + 内联 onchange 字符串,
        // 差异只有颜色与文案; 且阈值在前端硬编码, 与后端 validator 用的服务常量各写一份。
        // 现: 文案查表 + 样式走 .price-warn class + 勾选逻辑走 addEventListener。
        // **行为逐条保持**: 仍不阻止提交 (2026-06-24 起硬档也只是 strong warn),
        // 仍靠 window._priceWarnAck → order.acknowledge_price_warning 与后端软拦截对齐
        // (validator.py:229 无此标志即拒), 故这个全局标志是契约的一部分, 不得顺手删掉。
        const _PRICE_WARN_TEXT = {
            hard: {icon: '🛑', title: '价格偏离过大', ack: '⚠️ 确认提交 (已知晓价格偏离大)', gate: '请先确认价格偏离'},
            soft: {icon: '⚠️', title: '价格偏离警告', ack: '⚠️ 确认提交 (已知晓价格异常)', gate: '请先确认价格异常'},
        };

        // 场景化文案与后端 validator.py:203-224 的四条逐字同构 (买卖 × 高于/低于市价)。
        function _priceDevScenario(action, lp, cp, dev) {
            const isBuy = action === 'BUY';
            const rel = isBuy ? (lp > cp ? '高于' : '低于') : (lp < cp ? '低于' : '高于');
            const immediate = isBuy ? (lp > cp) : (lp < cp);
            const tail = immediate ? '将以市价立即成交（可能非预期）' : '订单不会立即成交（请确认是否输入正确）';
            return `${isBuy ? '买入' : '卖出'}限价 $${lp.toFixed(2)} ${rel}市价 $${cp.toFixed(2)} 达 ${dev.toFixed(1)}%，${tail}`;
        }

        function _renderPriceWarn(kind, dev, scenarioMsg) {
            const t = _PRICE_WARN_TEXT[kind] || _PRICE_WARN_TEXT.soft;
            const boxId = 'priceWarn_' + kind + '_' + Date.now();
            return {
                boxId: boxId,
                ackText: t.ack,
                gateText: t.gate,
                html: `<div class="price-warn gated ${kind}">
                    ${t.icon} ${t.title} (${dev.toFixed(1)}%)
                    <span class="pw-scenario">${escHtml(scenarioMsg)}</span>
                    <label><input type="checkbox" id="${boxId}">我已知晓价格偏离 ${dev.toFixed(1)}%，确认继续下单</label>
                </div>`,
            };
        }

        // 必须在 warn HTML 已写入 el.innerHTML 之后调用 (innerHTML += 会重建子树)。
        function _bindPriceWarn(w, confirmBtn) {
            window._priceWarnAck = false;
            if (!confirmBtn) return;
            confirmBtn.disabled = true;
            confirmBtn.textContent = w.gateText;
            confirmBtn.style.opacity = '0.5';
            confirmBtn.style.cursor = 'not-allowed';
            const box = document.getElementById(w.boxId);
            if (!box) return;
            box.addEventListener('change', function () {
                const on = this.checked;
                window._priceWarnAck = on;
                confirmBtn.disabled = !on;
                // 价格偏离确认不能覆盖服务端下发的只读态；执行时仍有 guard 兜底。
                if (!confirmBtn.disabled && !_runtimeCan('place')) confirmBtn.disabled = true;
                const allowed = !confirmBtn.disabled;
                confirmBtn.style.opacity = allowed ? '1' : '0.5';
                confirmBtn.style.cursor = allowed ? 'pointer' : 'not-allowed';
                confirmBtn.textContent = allowed ? w.ackText : w.gateText;
            });
        }

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
                // 2026-06-25 原注释称盘外"跳过 (与后端 validator 对齐)", 但代码从未跳过 ——
                //   实际是拿陈旧收盘价算偏离, 既造假警报也造假安心。
                // 2026-09-08 (REVIEW B2): 改为按时段选基准价并标注基准; 盘中语义与阈值完全不变。
                const otype = document.getElementById('orderType')?.value;
                const lpRaw = document.getElementById('limitPrice')?.value;
                if (otype === 'LIMIT' && lpRaw && parseFloat(lpRaw) > 0) {
                    const {phase: dPhase} = isExtendedHours();
                    const basis = dPhase === 'REGULAR'
                        ? {price: curQuote.cur_price || 0, label: '市价'}
                        : _extPriceBasis(curQuote, dPhase);
                    const lp = parseFloat(lpRaw);
                    const hardPct = (_gates && _gates.price_deviation_hard_pct) || 10;
                    const warnPct = (_gates && _gates.price_deviation_warn_pct) || 5;
                    if (basis.price > 0) {
                        const dev = Math.abs(lp - basis.price) / basis.price * 100;
                        const tail = dPhase === 'REGULAR'
                            ? '，提交时需确认'
                            : `（对比${basis.label}；盘外后端不拦截，请自行确认）`;
                        if (dev >= hardPct) {
                            const existing = estWarning.innerHTML || '';
                            estWarning.innerHTML = existing + `<div class="est-danger">⚠️ 限价偏离${basis.label} ${dev.toFixed(1)}% (≥${hardPct}%)${tail}</div>`;
                        } else if (dev >= warnPct) {
                            const existing = estWarning.innerHTML || '';
                            estWarning.innerHTML = existing + `<div class="est-warn">⚠️ 限价偏离${basis.label} ${dev.toFixed(1)}% (≥${warnPct}%)${tail}</div>`;
                        }
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
        // B1 (2026-09-08 REVIEW-20260908): 本单相关的闸门比对 — 只呈现, 后端仍是唯一裁决方。
        // 此前这些判据只在拒单后可见 (实测 83 条手动单 13 条被拒, 2026-06-30 连撞 PDT 3 次)。
        // 不含 PDT: 2026-08-02 用户裁决关闭 (富途证券(香港)账户不适用 FINRA PDT), 2026-09-08 用户复述。
        function orderGateLines(estCost) {
            if (!_gates) return '';
            const lines = [];
            const row = (color, text) => lines.push(
                '<div class="preview-row" style="color:' + color + '"><span class="pk">闸门</span><span class="pv">'
                + escHtml(text) + '</span></div>');
            (_gates.blocks_now || []).forEach(function(b) { row('#f87171', b); });
            (_gates.warnings_now || []).forEach(function(w) { row('#fbbf24', w); });
            const money = function(v) { return '$' + Number(v).toLocaleString(undefined, {maximumFractionDigits: 0}); };
            const cap = _gates.max_order_value_usd;
            if (cap && estCost > 0) {
                if (estCost > cap) row('#f87171', '订单金额 ' + money(estCost) + ' 超过单笔上限 ' + money(cap));
                else if (estCost > cap * 0.8) row('#fbbf24', '订单金额已达单笔上限的 ' + (estCost / cap * 100).toFixed(0) + '%（上限 ' + money(cap) + '）');
            }
            const budget = _gates.manual_budget_usd;
            if (budget != null && curOp === 'OPEN' && estCost > budget) {
                row('#f87171', '超出手动预算余量 ' + money(budget) + '（全局 80% 上限）');
            }
            const n = _gates.today_manual_orders, mx = _gates.max_daily_manual_orders;
            if (n != null && mx != null && n >= mx) row('#f87171', '今日手动下单已达 ' + n + ' 次（上限 ' + mx + '）');
            return lines.join('');
        }

        function previewOrder() {
          if (!_runtimeGuard('place', false)) return;
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

            // B10 (批3): 报价响应带 symbol (manual_order_service.py:255) 但此前从不比对。
            //   比对不上 ⇒ 当作"无报价"处理 (est 显示 --, 偏离检查跳过) 并**明说跳过**,
            //   而不是拿别的票的价格算出一个看起来很权威的预估金额。
            const quoteSym = curQuote && curQuote.symbol ? String(curQuote.symbol).toUpperCase() : '';
            const quoteMatches = !!curQuote && (!quoteSym || quoteSym === sym);
            const est = quoteMatches ? qty * curQuote.cur_price : 0;
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
            // B3 (2026-09-08 REVIEW): 休市自动 GTC + 卖空 = 券商必拒
            // (实测 2026-08-20 US.BABA: "撤单前有效订单不支持股票卖空。股票卖空请使用当日有效订单。")
            if (isClosed && action === 'SELL' && curDir === 'SHORT' && curOp === 'OPEN') {
                html += '<div class="preview-row" style="color:#f87171;background:rgba(248,113,113,0.08);padding:6px 10px;border-radius:6px"><span class="pk">🚫 卖空 + GTC</span><span class="pv">券商不接受"撤单前有效"的卖空单 — 请等盘中时段提交</span></div>';
            }
            // B10 (批3): 报价属于别的标的 ⇒ 预估金额与偏离检查按"无报价"处理, 并把这件事
            //   **说出来** (静默跳过会让人以为检查过了)。只提示不阻断: 后端 validator 仍会
            //   用自己取的现价做偏离软拦截, 前端不该多设一道闸门。
            if (curQuote && !quoteMatches) {
                html += '<div class="preview-row" style="color:#fbbf24"><span class="pk">⚠️ 报价标的</span><span class="pv">当前报价属于 ' + escHtml(quoteSym) + '，与输入的 ' + escHtml(sym) + ' 不一致 — 预估金额与限价偏离检查已跳过（提交时后端仍会校验）</span></div>';
            }
            el.innerHTML = html;
            // B1: 闸门信息随预览一起呈现 (提交前可见, 不再靠一次失败往返得知)
            el.innerHTML += orderGateLines(est);

            // ── 价格偏离检测 (P0 2026-06-15: SNDK 事故防护) ──
            // 仅盘中实时行情有效; 盘前/盘后/休市时报价不准, 跳过检测
            const confirmBtn = document.getElementById('confirmBtn');
            confirmBtn.disabled = !_runtimeCan('place');
            confirmBtn.style.opacity = confirmBtn.disabled ? '0.5' : '1';
            confirmBtn.style.cursor = confirmBtn.disabled ? 'not-allowed' : 'pointer';
            const _skipDevCheck = (phase !== 'REGULAR');
            // B8: 阈值改读 /api/trade/gates —— 后端 validator 用的就是这组服务常量
            // (PRICE_DEVIATION_WARN_PCT=5.0 / HARD_PCT=10.0), 前端再硬编码一份 5/10
            // 等于把同一个判据写两处。gates 未加载或字段缺失 ⇒ fail-open 回落 5/10
            // (与既有常量同值, 今日行为不变); 绝不让"取不到阈值"变成"不做偏离检查"。
            const warnPct = (_gates && _gates.price_deviation_warn_pct) || 5;
            const hardPct = (_gates && _gates.price_deviation_hard_pct) || 10;
            if (!_skipDevCheck && otype === 'LIMIT' && price && quoteMatches && curQuote.cur_price > 0) {
                const lp = parseFloat(price);
                const cp = curQuote.cur_price;
                const dev = Math.abs(lp - cp) / cp * 100;
                if (dev >= warnPct) {
                    const w = _renderPriceWarn(dev >= hardPct ? 'hard' : 'soft', dev,
                        _priceDevScenario(action, lp, cp, dev));
                    el.innerHTML += w.html;
                    _bindPriceWarn(w, confirmBtn);
                }
            }
            else if (_skipDevCheck && otype === 'LIMIT' && price && quoteMatches) {
                // B2 (2026-09-08 REVIEW-20260908): 盘外原本前后端**同时**跳过偏离检测 = 零防护
                //   (前端 _skipDevCheck / 后端 validator.py:189-196), 而 SNDK 事故正是盘后场景。
                //   报价已带 pre_price/after_price/overnight_price ⇒ 用时段对应价做基准。
                //   纯提示: 后端盘外同样不拦截, 故此处**不加勾选门槛、不 disable 确认键** (不改闸门)。
                const basis = _extPriceBasis(curQuote, phase);
                const lp = parseFloat(price);
                if (basis.price > 0 && lp > 0) {
                    const dev = Math.abs(lp - basis.price) / basis.price * 100;
                    if (dev >= warnPct) {
                        const strong = dev >= hardPct;
                        el.innerHTML += `<div class="price-warn ext ${strong ? 'hard' : 'soft'}">
                            ${strong ? '🛑' : '⚠️'} 限价 $${lp.toFixed(2)} 偏离${basis.label} $${basis.price.toFixed(2)} 达 ${dev.toFixed(1)}%
                            <div class="pw-note">盘外时段流动性低、价差可扩大 3-5 倍，且后端此时不做偏离拦截 — 请自行确认价格无误。</div>
                        </div>`;
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
            if (!_runtimeGuard('place', false)) return;
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
            if (confirmBtn) { confirmBtn.dataset.busy = '1'; confirmBtn.disabled = true; confirmBtn.textContent = '提交中...'; }
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
                if (confirmBtn) { delete confirmBtn.dataset.busy; confirmBtn.disabled = !_runtimeCan('place'); confirmBtn.textContent = '确认下单'; }
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
        let _lastSnapshotAt = 0;  // B5: 上一轮渲染时间戳 → 横幅"变动"文案的真实比较窗口

        // B5 (2026-09-08 REVIEW-20260908 批2): 原文案硬编码"15s 内变动 $X", 而实际比较
        // 窗口 = 上一轮渲染至今 (盘中 5s) ⇒ 用户对"恶化速度"的判断被系统性放大 3 倍。
        // 改用实测间隔; 拿不到 (首轮 / 标签页休眠过久) 才退回轮询间隔。
        function _snapshotWindowSecs() {
            if (_lastSnapshotAt) {
                const s = Math.round((Date.now() - _lastSnapshotAt) / 1000);
                if (s > 0 && s < 600) return s;
            }
            return Math.round(_getQuoteInterval() / 1000);
        }

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
            if (_runtimeCan('reduce')) {
                html += '<button class="diag-btn" onclick="closePos(\'' + escHtml(sym) + '\',' + qty + ',true)">平半仓</button>';
                html += '<button class="diag-btn danger" onclick="closePos(\'' + escHtml(sym) + '\',' + qty + ',false)">全平</button>';
            } else {
                html += '<span class="diag-btn" style="opacity:.55;cursor:not-allowed" title="券商/持仓状态未确认">平仓锁定</span>';
            }
            html += '<a class="diag-btn" href="/stock/' + encodeURIComponent(sym) + '">详情 →</a>';
            // 查找该标的的活跃挂单, 提供撤单按钮
            var pendingForSym = (_brokerOrders || []).filter(function(o) {
                var st = String(o.order_status || '').toUpperCase();
                return o.code === sym && st.indexOf('FILLED') < 0 && st.indexOf('CANCEL') < 0
                    && st.indexOf('FAIL') < 0 && st.indexOf('REJECT') < 0;
            });
            if (pendingForSym.length > 0 && _runtimeCan('cancel')) {
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
            if (!_runtimeGuard('reduce', true)) return;
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
            // B11: 不再预填文本 (预填 = 用户要么照抄要么改字, 都是摩擦); 留空即按「手动平仓」记账
            document.getElementById('closeReasonInput').value = '';
            renderCloseReasonChips('');
            document.getElementById('closeError').style.display = 'none';
            const closeBtn = document.getElementById('closeConfirmBtn');
            closeBtn.disabled = !_runtimeCan('reduce');
            closeBtn.title = closeBtn.disabled ? '当前运行状态不允许平仓' : '';
            closeBtn.textContent = '确认平仓';
            document.getElementById('closeModal').classList.add('show');
            setTimeout(() => document.getElementById('closeQtyInput').focus(), 100);
            hydrateCloseSessionHint(sym);

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
                // B11: 从 SL/TP 入口进来 ⇒ 预选「止损触发」(仍可改、可清空, 不阻断)
                document.getElementById('closeReasonInput').value = '止损触发';
                renderCloseReasonChips('止损触发');
                document.getElementById('closeConfirmBtn').textContent = `确认限价平仓 @ $${presetPrice.toFixed(2)}`;
            }
        }

        // ── B3 (2026-09-08 REVIEW-20260908): 平仓弹窗的时段提示 ──
        // 开仓侧早有守卫 (盘外禁市价 + 自动 GTC 提示), 平仓侧却完全没有: 未填限价即市价,
        // 且 executeClose 自动置 fill_outside_rth=true, executor.py:427 直接发 MARKET
        // ⇒ 盘外大概率被券商拒, 用户只看到富途原文而没有下一步。此处只提示 + 一键填时段价,
        // 不改提交语义 (是否放行仍由券商/后端裁决)。
        async function hydrateCloseSessionHint(sym) {
            const box = document.getElementById('closeSessionHint');
            if (!box) return;
            const info = isExtendedHours();
            if (info.phase === 'REGULAR') { box.style.display = 'none'; box.innerHTML = ''; return; }
            const phaseTxt = {PRE_MARKET: '盘前', AFTER_HOURS: '盘后', CLOSED: '休市', WEEKEND: '周末休市'}[info.phase] || '非盘中';
            box.style.display = 'block';
            box.innerHTML = '<div style="color:#fbbf24">⏳ 当前为' + phaseTxt + '时段：美股盘外不接受市价单，'
                + '留空限价直接提交大概率被券商拒。建议填入限价。</div>';
            try {
                const res = await fetch(`/api/quote/${encodeURIComponent(sym)}`);
                const q = await res.json();
                const basis = _extPriceBasis(q, info.phase);
                if (basis.price > 0) {
                    box.innerHTML += '<button type="button" class="btn btn-ghost btn-sm" style="margin-top:8px" '
                        + `onclick="fillClosePrice(${basis.price})">填入${escHtml(basis.label)} $${basis.price.toFixed(2)}</button>`;
                }
            } catch (e) { /* 提示已渲染, 取价失败不阻断平仓 */ }
        }

        function fillClosePrice(p) {
            const el = document.getElementById('closePriceInput');
            if (!el) return;
            el.value = Number(p).toFixed(2);
            el.focus();
        }

        function closeCloseModal() {
            document.getElementById('closeModal').classList.remove('show');
            closeCtx = null;
        }

        async function executeClose() {
            if (!closeCtx) return;
            if (!_runtimeGuard('reduce', true)) return;
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

            // B11 (用户裁「理由非必填」): 原实现理由为空直接 return 拦截 —— 已删除。
            //   留空 ⇒ 送空串, 由后端 routes/trade.py 落 "手动平仓" (留痕不断、归因不失真)。
            //   这是**去阻断**, 不是加阻断: 数量校验等既有防线逐字未动。
            const reason = document.getElementById('closeReasonInput').value.trim();

            const btn = document.getElementById('closeConfirmBtn');
            btn.dataset.busy = '1';
            btn.disabled = true;
            btn.textContent = '提交中...';

            try {
                const limitPrice = parseFloat(document.getElementById('closePriceInput').value) || null;
                const {extended: extClose, phase: phaseClose} = isExtendedHours();
                const isClosedClose = (phaseClose === 'CLOSED' || phaseClose === 'WEEKEND');
                const body = { symbol: sym, reason, quantity: closeQty };
                if (limitPrice && limitPrice > 0) body.price = limitPrice;
                body.fill_outside_rth = extClose || isClosedClose;
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
                delete btn.dataset.busy;
                btn.disabled = !_runtimeCan('reduce');
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
            if (!_runtimeGuard('cancel', false)) return;
            _pendingCancelId = id;
            document.getElementById('confirmText').textContent = `确认撤销订单 ${id}？`;
            document.getElementById('confirmModal').classList.add('show');
        }
        async function _executeCancel() {
            if (!_runtimeGuard('cancel', false)) return;
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
            const winSecs = _snapshotWindowSecs();
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
                    msg = '<strong>' + escHtml(sym.replace('US.','')) + '</strong> 盈亏快速恶化，近 ' + winSecs + 's 变动 $' + pnlChange.toFixed(0);
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
                const closeAction = String(topAlert.actionFn || '').indexOf('closePos') === 0;
                const actionHtml = (closeAction && !_runtimeCan('reduce'))
                    ? '<span class="attn-action" style="font-size:12px;opacity:.7">只读</span>'
                    : '<span class="attn-action"><button class="attn-btn" onclick="' + topAlert.actionFn + '">' + topAlert.action + '</button></span>';
                banner.innerHTML = '<div class="attn-banner ' + topAlert.type + '">' +
                    '<span class="attn-icon">' + icon + '</span>' +
                    '<span class="attn-text">' + topAlert.msg + '</span>' +
                    actionHtml + '</div>';
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
                if (!res.ok) {
                    console.error('snapshot HTTP error:', res.status);
                    showRuntimeUnavailable('状态接口 HTTP ' + res.status + '；已切换只读');
                    return;
                }
                const d = await res.json();
                // B7 (批3): 原每轮 console.log('[snapshot]', …) 已删 —— 盘中 5s 一轮 =
                // 每分钟 12 行浏览器控制台噪声, 且与后端 [trade/snapshot] 日志同内容双份
                // (后端那条同批降为 debug)。异常路径的 console.error 全部保留。

                if (d.runtime) applyRuntimeState(d.runtime);
                else showRuntimeUnavailable('后端未下发运行时状态；已切换只读');

                const positions = (d.positions || []).filter(p => p.qty !== 0);
                document.getElementById('posCount').textContent = `(${positions.length})`;

                // Phase A (2026-07-12): 数据新鲜度 — 任一持仓现价非实时即视为降级
                const brokerOffline = !!(_runtime && (_runtime.broker === 'disconnected' || _runtime.broker === 'unknown'));
                const degraded = !!d.stale || positions.some(p => p.stale) || brokerOffline;

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
                // Phase A: 购买力若走汇率估算路径, 追加 "≈" 标记并提示 (不伪装成精确值)
                const _powerEl = document.getElementById('tsPower');
                _powerEl.textContent = '$' + (power >= 1000 ? (power/1000).toFixed(1) + 'K' : power.toFixed(0)) + (acc.fx_estimated ? '≈' : '');
                _powerEl.title = acc.fx_estimated ? '购买力为汇率估算值 (券商未返回官方 USD 字段)' : '';
                // 同步账户净值到智能建议引擎
                if (totalAssets > 0) setEquityCache(totalAssets);

                // 持仓盈亏 (Phase A: 降级时聚合浮盈不可信 → 显示 "--")
                const posPnlEl = document.getElementById('tsPosPnl');
                const posPnlPctEl = document.getElementById('tsPosPnlPct');
                if (degraded) {
                    posPnlEl.textContent = '--'; posPnlEl.className = 'ts-val yellow';
                    posPnlPctEl.textContent = ''; posPnlPctEl.className = 'ts-sub';
                } else {
                    posPnlEl.textContent = (unrealPnl >= 0 ? '+$' : '-$') + Math.abs(unrealPnl).toFixed(0);
                    posPnlEl.className = 'ts-val ' + (unrealPnl >= 0 ? 'green' : 'red');
                    posPnlPctEl.textContent = (unrealPnl >= 0 ? '+' : '') + (totalAssets > 0 ? (unrealPnl / totalAssets * 100).toFixed(1) : '0') + '%';
                    posPnlPctEl.className = 'ts-sub ' + (unrealPnl >= 0 ? 'green' : 'red');
                }

                // 今日盈亏 (Phase A: 降级时显示 "--")
                const pnlEl = document.getElementById('tsPnl');
                const pnlPctEl = document.getElementById('tsPnlPct');
                if (degraded) {
                    pnlEl.textContent = '--'; pnlEl.className = 'ts-val yellow';
                    pnlPctEl.textContent = ''; pnlPctEl.className = 'ts-sub';
                } else {
                    pnlEl.textContent = (displayPnl >= 0 ? '+$' : '-$') + Math.abs(displayPnl).toFixed(0);
                    pnlEl.className = 'ts-val ' + (displayPnl >= 0 ? 'green' : 'red');
                    pnlPctEl.textContent = (displayPnl >= 0 ? '+' : '') + displayPnlPct.toFixed(1) + '%';
                    pnlPctEl.className = 'ts-sub ' + (displayPnl >= 0 ? 'green' : 'red');
                }

                // 次要指标
                const marketVal = (d.account || {}).market_val || 0;
                const cash = (d.account || {}).cash || 0;
                const frozen = (d.account || {}).frozen_cash || 0;
                document.getElementById('tsMarketVal').textContent = '$' + (marketVal >= 1000 ? (marketVal/1000).toFixed(1) + 'K' : marketVal.toFixed(0));
                document.getElementById('tsCash').textContent = '$' + (cash >= 1000 ? (cash/1000).toFixed(1) + 'K' : cash.toFixed(0));
                document.getElementById('tsFrozen').textContent = '$' + frozen.toFixed(0);
                document.getElementById('tsMarginUsage').textContent = marginPct.toFixed(1) + '%';

                // 数据新鲜度 (Phase A: 三态 — 绿=实时券商 / 黄=降级(成本价) / 灰=离线)
                const dot = document.querySelector('.ts-dot');
                if (dot) {
                    dot.classList.toggle('stale', brokerOffline);
                    dot.classList.toggle('degraded', !brokerOffline && degraded);
                }
                const freshEl = document.getElementById('tsFreshText');
                if (freshEl) {
                    freshEl.textContent = brokerOffline ? '离线' : (degraded ? '降级·成本价' : '实时');
                    freshEl.style.color = brokerOffline ? '#f87171' : (degraded ? '#d29922' : '#8b949e');
                    freshEl.title = brokerOffline
                        ? '券商连接不可用；仅展示最近缓存/本地台账'
                        : (degraded ? '券商实时数据不可用, 现价用成本价占位, 浮盈/今日盈亏暂不可知' : ('数据时间 ' + (d.as_of || '')));
                }
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
                    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:#8b949e;padding:20px">无持仓</td></tr>';
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
                        // Phase A: 现价非实时 → 浮盈/今日盈亏显示 "--"; 价格来源加徽标
                        const stale = !!p.stale;
                        const pbBadge = (p.price_basis && p.price_basis !== 'live')
                            ? '<span class="pb-badge" title="现价非实时行情">' + ({cost:'成本', delayed:'延迟', none:'无价'}[p.price_basis] || '非实时') + '</span>'
                            : '';
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
                        // PnL 趋势 — B4 (2026-09-08 REVIEW-20260908 批2): 原实现每轮每行都渲染
                        // ↑/↓/→, 其中"→ 横盘"零信息量, 盘中 5s 一轮导致整列持续闪动 = 稳定噪声。
                        // 现只在 |Δ| 超阈值时渲染, 中性态不占像素; 真实比较窗口写进 title。
                        const prevU = _prevPnl[sym];
                        const pnlDelta = prevU !== undefined ? uPnl - prevU : 0;
                        const trendWin = '（近 ' + _snapshotWindowSecs() + 's）';
                        const trendIcon = prevU === undefined ? ''
                            : pnlDelta > 5 ? ' <span style="color:#4ade80;font-size:11px" title="盈利改善 +$'+pnlDelta.toFixed(0)+trendWin+'">↑</span>'
                            : pnlDelta < -5 ? ' <span style="color:#f87171;font-size:11px" title="盈利恶化 $'+pnlDelta.toFixed(0)+trendWin+'">↓</span>'
                            : '';
                        _prevPnl[sym] = uPnl;
                        // Signal light
                        const slKey = sym + '|' + (isLong ? 'LONG' : 'SHORT');
                        const slData = _slMap[slKey];
                        const sig = computeSignal(p, slData);
                        _diagData[sym] = {pos: p, slData, signal: sig, qty, isLong};
                        // B6 (2026-09-08 REVIEW-20260908 批2): 最关键的风控信息此前只体现为代码旁
                        // 一个 10px 色点, 要看数字必须点开弹层 —— 而注意力横幅的最高优先级恰恰是
                        // "距止损 < 2%"。现升为独立列 (数据本就在 snapshot.stop_distances), 色点与
                        // 弹层保留为动作入口。ATR 估算值显式标 "~", 不伪装成系统真实 SL。
                        const protMeta = {
                            broker_attached: {label:'券商单', color:'#4ade80', title:'本地记录显示券商端已挂止损单（未逐单向券商核验）'},
                            db_only: {label:'仅DB', color:'#fbbf24', title:'止损只存在本地 DB，券商端未确认挂单'},
                            none: {label:'无保护', color:'#f87171', title:'系统未找到该持仓的止损保护'},
                            unknown: {label:'未知', color:'#8b949e', title:'保护状态查询失败，不能据此判断已保护'}
                        }[p.protection_state] || {label:'未知', color:'#8b949e', title:'保护状态未知'};
                        const slCell = slData
                            ? `<div style="font-size:12px;font-weight:700;color:${slData.danger ? '#f87171' : (slData.sl_distance_pct < 5 ? '#fbbf24' : '#8b949e')}">${slData.sl_distance_pct.toFixed(1)}%${slData.atr_estimated ? '<span title="止损位为 ATR 估算 (非系统实际 SL)">~</span>' : ''}</div>`
                              + `<div style="font-size:11px;color:#8b949e">$${(slData.stop_loss || 0).toFixed(2)} <span style="color:${protMeta.color};font-weight:600" title="${protMeta.title}">${protMeta.label}</span></div>`
                            : `<div style="color:#8b949e">--</div><div style="font-size:11px;color:${protMeta.color}" title="${protMeta.title}">${protMeta.label}</div>`;
                        // 点击代码跳转交易页面并带入代码+数量
                        const tradeUrl = '/trade?symbol=' + encodeURIComponent(sym) + '&qty=' + qty;
                        return `<tr onclick="showDiag('${escHtml(sym)}', this)" style="cursor:pointer">
                            <td data-label="代码" style="white-space:nowrap">
                                <a href="${tradeUrl}" style="color:#58a6ff;text-decoration:none;font-weight:700" onclick="event.stopPropagation()">${escHtml(codeShort)}</a>
                                <span class="sig ${sig.color}" style="width:10px;height:10px;vertical-align:middle;margin-left:6px" title="${sig.action}"></span>
                                ${p.legacy ? `<span title="broker has position but system not managing" onclick="event.stopPropagation();takeoverPos('${sym}')" style="display:inline-block;font-size:9px;padding:1px 5px;margin-left:4px;background:#7c2d12;color:#fed7aa;border:1px solid #c2410c;border-radius:3px;cursor:pointer;font-weight:700">未管理</span>` : ''}
                            </td>
                            <td data-label="市值/数量" style="text-align:right;white-space:nowrap">
                                <div style="font-weight:600;color:${mvColor}">${mvStr}</div>
                                <div style="font-size:11px;color:${qtyColor}">${qtyStr}</div>
                            </td>
                            <td data-label="现价/成本" style="text-align:right;white-space:nowrap">
                                <div style="font-weight:600">${cur > 0 ? '$'+cur.toFixed(2) : '--'}${pbBadge}</div>
                                <div style="font-size:11px;color:#8b949e">/$${cost.toFixed(2)}</div>
                            </td>
                            <td data-label="今日盈亏" style="text-align:right;white-space:nowrap" class="${stale ? '' : tCls}">
                                <div>${stale ? '<span style="color:#8b949e">--</span>' : (tPnl ? (tPnl>=0?'+$':'-$')+Math.abs(tPnl).toFixed(2) : '--')}</div>
                                <div style="font-size:11px">${stale ? '' : (tPnl ? (tPnl>=0?'+':'')+tPnlPct.toFixed(1)+'%' : '')}</div>
                            </td>
                            <td data-label="持仓盈亏" style="text-align:right;white-space:nowrap" class="${stale ? '' : uCls}">
                                <div>${stale ? '<span style="color:#8b949e">--</span>' : (uPnl>=0?'+$':'-$')+Math.abs(uPnl).toFixed(2)+trendIcon}</div>
                                <div style="font-size:11px">${stale ? '' : (uPnl>=0?'+':'')+uPnlPct.toFixed(1)+'%'}</div>
                            </td>
                            <td data-label="距止损/SL" style="text-align:right;white-space:nowrap">${slCell}</td>
                            <td data-label="占比" style="text-align:center;white-space:nowrap">
                                <div style="font-size:12px;font-weight:600">${concPct.toFixed(1)}%</div>
                                <div style="height:3px;background:#21262d;border-radius:2px;margin-top:3px"><div style="height:100%;width:${Math.min(concPct,100)}%;background:${concColor};border-radius:2px"></div></div>
                            </td>
                        </tr>`;
                    }).join('');
                }

                // ── 注意力优先引擎: 扫描持仓，置顶最紧急事项 ──
                updateAttentionBanner(positions);
                _lastSnapshotAt = Date.now();   // B5: 必须在本轮渲染完成之后打点

                // Orders — store and render unified with audit
                _brokerOrders = d.orders || [];
                _auditOrders = (d.audit && d.audit.orders) ? d.audit.orders : [];
                renderUnifiedOrders();
            } catch (e) {
                console.error('Trade snapshot error:', e);
                showRuntimeUnavailable('交易状态读取失败；已切换只读');
                _failCount++;
                const dot = document.querySelector('.ts-dot');
                if (dot) { dot.classList.add('stale'); dot.classList.remove('degraded'); }
                const freshErr = document.getElementById('tsFreshText');
                if (freshErr) { freshErr.textContent = '离线'; freshErr.style.color = '#8b949e'; freshErr.title = '数据加载失败'; }
                if (_failCount >= _MAX_FAILS_BEFORE_BANNER) {
                    const posBody = document.getElementById('posBody');
                    if (posBody && !posBody.querySelector('.api-error-row')) {
                        posBody.innerHTML = '<tr class="api-error-row"><td colspan="7" style="text-align:center;color:#f87171;padding:20px">⚠️ 数据加载失败 (已重试 ' + _failCount + ' 次) — 请检查网络连接</td></tr>';
                    }
                }
            }
        }

        // ── slow-data removed: decision quality moved to dashboard deep review ──

        // ── Visibility-Aware Polling ──
        let _fastTimer = null, _slowTimer = null;
        let _isPolling = false;
        let _failCount = 0;
        const _MAX_FAILS_BEFORE_BANNER = 3;
        let _tradeWsLastQuote = 0;  // quote_update WS 推送时间戳

        function _getMarketPhase() {
            // 复用 isExtendedHours() 判断盘中/盘外
            var info = isExtendedHours();
            return info.phase;
        }

        function _getQuoteInterval() {
            // 盘中(REGULAR): 5s; 盘前盘后/休市: 15s; WS在线时放宽到 safety-net
            var phase = _getMarketPhase();
            if (phase === 'REGULAR') return 5000;
            return 15000;
        }

        // ══════════════════════════════════════════════════════
        //  B1 (2026-09-08 REVIEW-20260908): 手动下单闸门只读呈现
        //  闸门此前只在拒单后可见 (实测 83 条手动单 13 条被拒, 其中 kill switch /
        //  资本水位 / 回撤熔断 / 手动预算 / 单笔上限 / 当日单数 六类前端零呈现)。
        //  纪律: 只呈现不裁决 —— 后端 validator 仍是唯一权威; 读取失败一律静默降级,
        //  绝不因为前端拿不到数据就宣称"被阻断"。不含 PDT (2026-08-02 已裁决关闭)。
        // ══════════════════════════════════════════════════════
        async function loadGates() {
            try {
                const res = await fetch('/api/trade/gates');
                if (!res.ok) return;          // 端点不存在 (旧后端) → 保持静默
                _gates = await res.json();
                renderGates();
                updateEstimate();             // 阈值改由后端下发, 到位后重算一次提示
            } catch (e) { /* 降级: 不呈现 ≠ 阻断 */ }
        }

        function renderGates() {
            const row = document.getElementById('gatesRow');
            if (!row || !_gates) return;
            const chips = [];
            const push = (cls, html) => chips.push('<span class="gate-chip ' + cls + '">' + html + '</span>');
            const blocks = _gates.blocks_now || [];
            const warns = _gates.warnings_now || [];
            // 正常态只留一枚暗色 chip, 避免常驻噪声; 有阻断/警告才逐条展开
            if (blocks.length) blocks.forEach(b => push('block', escHtml(b)));
            else if (warns.length) warns.forEach(w => push('warn', escHtml(w)));
            else push('ok', '闸门 ✓ 无全局阻断');
            const n = _gates.today_manual_orders, mx = _gates.max_daily_manual_orders;
            if (n != null && mx != null) {
                push(n >= mx ? 'block' : (n >= mx * 0.8 ? 'warn' : ''), `今日手动单 <b>${n}/${mx}</b>`);
            }
            row.innerHTML = '<span class="gates-title">闸门</span>' + chips.join('');
        }

        function startGatesTimer() {
            if (_gatesTimer) return;
            _gatesTimer = setInterval(loadGates, 60000);   // 闸门变化慢, 60s 足够
        }

        function startPolling() {
            if (_isPolling) return;
            _isPolling = true;
            loadTradeSnapshot();
            loadGates();
            startGatesTimer();
            var fastInterval = _getQuoteInterval();
            // B10 (批3): 原实现用 arguments.callee 重挂定时器 —— 严格模式下直接抛
            // TypeError, 是本文件将来加 'use strict' 的地雷; 改具名函数表达式, 语义不变。
            var fastTick = function fastTick() {
                // 动态调整: 盘中/盘外间隔不同
                var desired = _getQuoteInterval();
                if (desired !== fastInterval) {
                    fastInterval = desired;
                    clearInterval(_fastTimer);
                    _fastTimer = setInterval(fastTick, desired);
                }
                // WS 5s 内推送过 quote_update → 跳过 HTTP 轮询
                if (_tradeWsLastQuote && (Date.now() - _tradeWsLastQuote < 5000)) return;
                loadTradeSnapshot();
                // 刷新输入框中的标的报价
                var _curSym = document.getElementById('symbol')?.value?.trim();
                if (_curSym) fetchQuote();
            };
            _fastTimer = setInterval(fastTick, fastInterval);
        }

        function stopPolling() {
            _isPolling = false;
            if (_fastTimer) { clearInterval(_fastTimer); _fastTimer = null; }
            if (_slowTimer) { clearInterval(_slowTimer); _slowTimer = null; }
            if (_gatesTimer) { clearInterval(_gatesTimer); _gatesTimer = null; }
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
                var cancelBtn = canCancel && oid && _runtimeCan('cancel')
                    ? '<button class="btn-cancel" style="font-size:11px;padding:4px 10px;min-height:auto" onclick="event.stopPropagation();cancelOrder(\'' + escHtml(oid) + '\')">撤单</button>'
                    : (isFilled ? '<span style="font-size:11px;color:#4ade80">已成交</span>'
                        : (canCancel ? '<span style="font-size:11px;color:#8b949e">撤单锁定</span>' : ''));
                var sortKey = o.create_time || '';
                var qtyDisplay = o.filled_qty || o.dealt_qty || o.qty || 0;
                rows.push({
                    sortKey: sortKey,
                    html: '<tr>' +
                        '<td data-label="时间" style="font-size:11px;color:#8b949e;white-space:nowrap">' + timeStr +
                            (durStr ? ' <span style="font-size:10px;color:#58a6ff">' + durStr + '</span>' : '') +
                        '</td>' +
                        '<td data-label="标的"><b>' + escHtml(o.code || '') + '</b></td>' +
                        '<td data-label="动作">' + sideLabel + '</td>' +
                        '<td data-label="数量" style="text-align:right">' + qtyDisplay + '</td>' +
                        '<td data-label="价格" style="text-align:right;white-space:nowrap">' + priceStr + '</td>' +
                        '<td data-label="状态" style="color:' + stColor + ';font-weight:600;font-size:12px">' + escHtml(stStr || o.order_status || '--') + '</td>' +
                        '<td data-label="备注" style="font-size:11px;color:#8b949e">' + note + '</td>' +
                        '<td data-label="" style="white-space:nowrap">' + cancelBtn + '</td>' +
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
                // B7 附带 (批3): 统一交易记录表的第 5 列表头是"价格", 但 audit 行此前
                //   往该列塞的是 order_type ⇒ 表头说价格、内容写 MARKET/LIMIT (实测
                //   manual_order_audit.price 对 LIMIT 单有值、MARKET 单为 NULL)。
                //   现按表头的口径显示真价格, 订单类型降为灰色小字后缀 (信息不丢)。
                var aPrice = (a.price != null && Number(a.price) > 0)
                    ? '$' + Number(a.price).toFixed(2) : '--';
                var aType = escHtml(a.order_type || '');
                rows.push({
                    sortKey: a.timestamp || '',
                    html: '<tr style="opacity:0.75">' +
                        '<td data-label="时间" style="font-size:11px;color:#8b949e;white-space:nowrap">' + aTime + '</td>' +
                        '<td data-label="标的"><b>' + aSym + '</b> <span style="font-size:9px;color:#8b949e">系统</span></td>' +
                        '<td data-label="动作">' + aAction + '</td>' +
                        '<td data-label="数量" style="text-align:right">' + escHtml(String(a.quantity || '')) + '</td>' +
                        '<td data-label="价格" style="text-align:right;white-space:nowrap">' + aPrice +
                            (aType ? ' <span style="font-size:10px;color:#8b949e">' + aType + '</span>' : '') + '</td>' +
                        '<td data-label="状态" style="color:' + aSc + ';font-weight:600;font-size:12px">' + escHtml(a.status) + '</td>' +
                        '<td data-label="备注" style="font-size:11px;color:#8b949e">' + aNote + '</td>' +
                        '<td data-label=""></td>' +
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
                if (_runtime && _runtime.orders !== 'live') parts.push(_runtimeLabel('orders', _runtime.orders));
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
            // 报价推送 → 立即刷新交易页报价 (不再等轮询)
            WSManager.on('quote_update', function() {
                _tradeWsLastPush = Date.now();
                _tradeWsLastQuote = Date.now();
                var _curSym = document.getElementById('symbol')?.value?.trim();
                if (_curSym) fetchQuote();
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

        // ── RFC-078 批B: 券商真值层上下文条 (本地只读, **不进**快照轮询) ──────────
        // 本页「交易记录」= 回合台账 + 审计, 而回合台账曾断链 3 个月 (2026-06-19~09-08,
        // `_record_manual_close` 委派签名漂移 ⇒ 券商已平仓、账本零记录)。这一条让读者知道
        // 真值层看到多少笔、台账覆盖多少、缺口在哪。刻意不做第二张成交表 (今日成交下表已有,
        // 再列一遍只会稀释信噪比)。三态由后端判定, 前端不得把三者渲染成同一句"无成交"。
        function _usMoney(v) {
            return (v === null || v === undefined || isNaN(v)) ? '--'
                : '$' + Number(v).toLocaleString(undefined, {maximumFractionDigits: 2});
        }

        // 批 D (RFC-078 §8): 回合(券商真值派生) — **只加一行**, 不做第二张成交/回合表。
        // 本页已有「交易记录」= trade_records 决策台账 + 审计; 派生回合是券商成交的另一种
        // 粒度, 并列两张表 = 同一笔钱在同页出现两次 (信噪比反向退化)。两层必须**自陈**不是
        // 同一层, 否则会互相冒充 (决策台账有 exit_reason 但断链过; 派生层不断链但无理由)。
        function _usRtLine(d) {
            const rt = d.round_trips || {};
            if (!rt.state) return '';
            if (rt.state === 'OK') {
                const rs = rt.summary || {};
                // 与成交层那个"已实现"是**两种配对口径**, 不等时必须当场解释 (同页两个数
                // 而不解释 = 让读者自己挑一个信): 本行=全量台账配对后按出场日归集;
                // 成交层=仅用本窗口成交配对 (跨窗持仓的进场腿在窗口外 ⇒ 被整段排除 ⇒ 偏低)。
                const fz = (d.fifo || {}).realized_gross_complete_basis_only;
                const rtz = rs.pnl_gross_complete_basis_only;
                const diffNote = (d.state === 'OK' && fz != null && rtz != null
                    && Math.abs(fz - rtz) > 0.005)
                    ? '<br><span style="color:#fbbf24">⚠ 与上面「FIFO 已实现(仅用本窗口成交配对)」'
                      + _usMoney(fz) + ' <b>不相等</b>: 本行是全量台账配对后按出场日归集, '
                      + '跨窗持仓的进场腿落在窗口外会被误标基准不完整而整段排除 ⇒ 窗口口径偏低。'
                      + '本窗口以<b>本行</b>为准, 两者都不含手续费。</span>'
                    : '';
                // 胜率过闸门才给: 计数是事实、比率是推断 (小样本/低覆盖率的比率会误导)
                const win = (rs.win_rate_state === 'OK')
                    ? '胜 ' + (rs.wins || 0) + '/' + (rs.n_scored || 0) + ' (' + rs.win_rate_pct + '%)'
                    : '<b style="color:#fbbf24">不给胜率</b> (' + (rs.win_rate_state === 'SMALL_SAMPLE'
                        ? '计分仅 ' + (rs.n_scored || 0) + ' 笔 < 5, 小样本比率无信息量'
                        : '计分覆盖率 ' + Math.round((rs.scored_coverage || 0) * 100)
                          + '% < 70%, 排除掉的往往正是亏损行') + ')';
                return '<br>回合(<span style="color:#fbbf24">券商真值派生 FIFO · 全量台账配对 · 毛'
                    + '</span>): <b>'
                    + (rs.n || 0) + '</b> 笔 / ' + (rs.symbols || 0) + ' 标的 · ' + win
                    + ' · 已实现(毛, 完整基准, 按出场日归集) <b>' + _usMoney(rtz) + '</b>'
                    + ' · 中位持有 ' + (rs.median_holding_hours == null ? '--' : rs.median_holding_hours) + 'h'
                    + diffNote
                    + '<br><span style="color:#8b949e">⚠ 派生回合只有**执行事实**: 决策理由未捕获 '
                    + (rs.exit_reason_not_captured || 0) + '/' + (rs.n || 0) + ' 笔'
                    + ' (不做时间邻近推断 = 不把猜测写成事实)'
                    + ' · 与下表「交易记录」(trade_records 决策台账)**不是同一层**, 两者不得互相冒充'
                    + ' · 未闭环批次(= 在仓, **不算回合**) ' + (rt.open_lots_n || 0) + ' 批'
                    + ((rt.open_lots_symbols || []).length
                        ? ': ' + escHtml(rt.open_lots_symbols.join(', ')) : '')
                    + '</span>';
            }
            if (rt.state === 'NO_ROUNDS_IN_WINDOW') {
                return '<br><span style="color:#8b949e">回合(派生): 本窗口无**了结**回合 — 台账累计 '
                    + (rt.ledger_rows || 0) + ' 笔 ⇒ 是真没了结, 不是没派生; 未闭环批次 '
                    + (rt.open_lots_n || 0) + ' 批 = 仍在仓</span>';
            }
            if (rt.state === 'NO_DATA') {
                return '<br><span style="color:#8b949e">回合(派生): 台账一行回合都没有 = 还没派生过, '
                    + '这**不是**"零回合" — 点「回补」会一并派生</span>';
            }
            return '<br><span style="color:#f87171">回合(派生): ' + escHtml(rt.state)
                + ' — 该段未出数, 不是零回合</span>';
        }

        async function loadUsLedger() {
            const el = document.getElementById('ledgerStrip');
            if (!el) return;
            try {
                const r = await fetch('/api/us/ledger?days=30');
                const j = await r.json();
                if (!j.ok) {
                    el.innerHTML = '券商真值层: <span style="color:#f87171">读取失败 '
                        + escHtml((j.error && j.error.message) || ('HTTP ' + r.status))
                        + ' — 这**不是**"无成交"</span> '
                        + '<button class="btn btn-ghost btn-sm" onclick="loadUsLedger()">重试</button>';
                    return;
                }
                renderUsLedger(j.data || {}, el);
            } catch (e) {
                el.innerHTML = '券商真值层: <span style="color:#f87171">请求异常 '
                    + escHtml(String(e)) + '</span>';
            }
        }

        function renderUsLedger(d, el) {
            const cov = ((d.coverage || {}).deals || {}).US || {};
            const snap = ((d.coverage || {}).snapshots || {}).US || {};
            const w = d.window || [];
            const head = '券商真值层 (' + escHtml(d.window_tz || 'ET') + ' 本地日 '
                + escHtml(w[0] || '?') + '~' + escHtml(w[1] || '?') + ')';
            const tail = ' · 台账累计 <b>' + (cov.rows || 0) + '</b> 笔 ('
                + escHtml(cov.first || '--') + '~' + escHtml(cov.last || '--') + ')'
                + ' · 持仓快照 ' + (snap.days || 0) + ' 天'
                + ' <button class="btn btn-ghost btn-sm" onclick="loadUsLedger()">刷新</button>'
                + ' <button class="btn btn-ghost btn-sm" id="usLedgerSync" onclick="syncUsLedger()">回补</button>';
            if (d.state === 'NO_DATA') {
                el.innerHTML = head + ': <span style="color:#fbbf24">台账为空 = 还没回补过, '
                    + '这**不是**"本期零成交"</span>' + _usRtLine(d) + tail;
                return;
            }
            if (d.state !== 'OK') {
                el.innerHTML = head + ': <span style="color:#fbbf24">本期无成交 '
                    + '(台账有数据 ⇒ 是真没交易, 不是没同步)</span>' + _usRtLine(d) + tail;
                return;
            }
            const s = d.summary || {}, f = d.fifo || {};
            let html = head + ': <b>' + (s.n || 0) + '</b> 笔成交 / ' + (s.symbols || 0)
                + ' 标的 · 开 ' + _usMoney(s.open_amount) + ' · 平 ' + _usMoney(s.close_amount)
                + ' · 净现金流 <b>' + _usMoney(s.net_cash) + '</b>'
                + ' · FIFO 已实现(<span style="color:#fbbf24">毛</span>, 仅用本窗口成交配对) <b>'
                + _usMoney(f.realized_gross_complete_basis_only) + '</b>'
                + ' <span style="color:#fbbf24">不含手续费(' + (s.fee_rows_null || 0) + '/'
                + (s.n || 0) + ' 笔 fee=NULL), 不得当净额用</span>';
            if ((f.incomplete_basis_symbols || []).length) {
                html += '<br>⚠ 成本基准不完整 (其盈亏**未计入**合计, 不编造基准): '
                    + escHtml(f.incomplete_basis_symbols.join(', '));
            }
            if (s.unknown_side_rows) {
                html += '<br>⚠ 未见过的方向 ' + s.unknown_side_rows + ' 笔 (未计入开/平, 不猜方向)';
            }
            html += _usRtLine(d);
            html += '<br><span style="color:#8b949e">台账↔券商对账 (孤儿在仓行 / 未入账仓) 见周一实盘周报'
                + ' — 下表是回合台账+审计, 断链期 (2026-06-19~09-08) 的平仓不在下表</span>' + tail;
            el.innerHTML = html;
        }

        async function syncUsLedger() {
            const el = document.getElementById('ledgerStrip');
            const btn = document.getElementById('usLedgerSync');
            if (btn) { btn.disabled = true; btn.textContent = '回补中...'; }
            let note;
            try {
                const r = await fetch('/api/us/ledger/sync?days=30', {method: 'POST'});
                const j = await r.json();
                if (!j.ok) {
                    note = '⚠ 回补失败: ' + ((j.error && j.error.message) || ('HTTP ' + r.status))
                        + ' — 台账**未更新**, 这不等于零成交';
                } else {
                    const res = j.data || {}, dl = res.deals || {};
                    const bad = dl.unavailable_legs || [];
                    note = '回补 overall=' + (res.overall || '?') + ' · 新增 ' + (dl.inserted || 0)
                        + ' / 覆写 ' + (dl.updated || 0) + ' 笔'
                        + (bad.length ? ' · ⚠ 不可达腿 ' + bad.join(',') + ' (该腿未更新, 不是零成交)' : '');
                }
            } catch (e) {
                note = '⚠ 回补请求异常: ' + String(e);
            }
            await loadUsLedger();   // 重绘正文 (按钮一并复原)
            const el2 = document.getElementById('ledgerStrip');
            if (el2) el2.insertAdjacentHTML('beforeend',
                '<br><span style="color:#8b949e">' + escHtml(note) + '</span>');
        }

        // ── Expose handlers to window for inline HTML event handlers ──
        loadUsLedger();      // 台账是本地只读, 只加载一次 (不进 30s 轮询, 省富途频率额度)
        bindRecentChips();   // B10: 最近标的 chips (localStorage 不可用时静默无 chips)
        bindCloseReasonChips();   // B11: 平仓理由 chips (事件委托, 弹窗开合不影响监听)
        Object.assign(window, {
            cancelOrder, clearSlTp, closeCloseModal, closePos, closePosAtPrice, closePreview, _executeCancel, executeClose, executeOrder, fetchQuote, fillClosePrice, hideDiag, loadOrders, loadPositions, loadUsLedger, onQuantityInput, pctQty, previewOrder, restoreSuggestion, setDir, setMaxPct, setOp, setQty, setRiskPct, showDiag, stepQty, syncUsLedger, takeoverPos, toggleOrders, toggleRiskCfg
        });

})();
