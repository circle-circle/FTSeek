/**
 * Playbook Dashboard — 今日交易指令视图
 * 加载指令 → 展示卡片 → 执行/跳过 → 反馈收集
 */

let currentInstructions = [];
let pendingExecuteId = null;

// ========== 初始化 ==========
document.addEventListener('DOMContentLoaded', () => {
    loadPlaybook();
});

async function loadPlaybook() {
    try {
        const [todayResp, summaryResp] = await Promise.all([
            fetch('/api/playbook/today').then(r => r.json()),
            fetch('/api/playbook/summary').then(r => r.json()),
        ]);

        if (todayResp.success) {
            currentInstructions = todayResp.data || [];
            renderInstructions(currentInstructions);
        }

        if (summaryResp.success) {
            renderSummary(summaryResp.data);
        }
    } catch (e) {
        console.error('Failed to load playbook:', e);
        document.getElementById('activeInstructions').innerHTML =
            '<div class="pb-empty">加载失败，请刷新重试</div>';
    }
}

// ========== 渲染 ==========

function renderSummary(data) {
    const bar = document.getElementById('summaryBar');
    bar.style.display = 'flex';

    document.getElementById('statTotal').textContent = data.today_instructions_count || 0;
    document.getElementById('statBuy').textContent = data.today_buy_count || 0;
    document.getElementById('statShort').textContent = data.today_short_count || 0;
    document.getElementById('statSkip').textContent = data.today_skip_count || 0;
    document.getElementById('statExecuted').textContent = data.today_executed_count || 0;
    document.getElementById('statWinRate').textContent = (data.win_rate || 0) + '%';

    // Regime badge
    const regime = data.recent_regime;
    const badge = document.getElementById('regimeBadge');
    if (regime) {
        const regimeMap = {
            'RISK_ON': '🟢 RISK_ON',
            'CAUTIOUS': '🟡 CAUTIOUS',
            'RISK_OFF': '🔴 RISK_OFF',
            'NO_TRADE': '⛔ NO_TRADE',
        };
        badge.textContent = regimeMap[regime.regime] || regime.regime;
        badge.className = 'pb-regime-badge pb-regime-' + regime.regime;
    } else {
        badge.textContent = '-- 未评估 --';
    }
}

function renderInstructions(instructions) {
    const activeContainer = document.getElementById('activeInstructions');
    const skippedContainer = document.getElementById('skippedInstructions');
    const skippedSection = document.getElementById('skippedSection');

    const active = instructions.filter(i => i.direction !== 'SKIP');
    const skipped = instructions.filter(i => i.direction === 'SKIP');

    if (active.length === 0 && skipped.length === 0) {
        activeContainer.innerHTML = '<div class="pb-empty">📋 今日暂无交易指令<br><small>盘前 08:00 ET 自动生成</small></div>';
        skippedSection.style.display = 'none';
        return;
    }

    // Render active instructions
    activeContainer.innerHTML = active.map(inst => renderInstructionCard(inst)).join('');

    // Render skipped instructions
    if (skipped.length > 0) {
        skippedSection.style.display = 'block';
        skippedContainer.innerHTML = skipped.map(inst => renderSkipItem(inst)).join('');
    } else {
        skippedSection.style.display = 'none';
    }
}

function renderInstructionCard(inst) {
    const conf = inst.confidence || 0;
    const confClass = conf >= 70 ? 'pb-conf-high' : conf >= 50 ? 'pb-conf-mid' : 'pb-conf-low';
    const conditions = parseJSON(inst.conditions_json) || [];
    const risks = parseJSON(inst.risks_json) || [];
    const executed = inst.executed == 1;

    let statusBadge = '';
    if (executed) {
        statusBadge = `<span class="pb-executed-badge">✅ 已执行 @ $${fmt(inst.execution_price)}</span>`;
    }
    if (inst.outcome === 'win') statusBadge += ' <span class="pb-executed-badge" style="background:#16a34a55">Win +$' + fmt(inst.pnl) + '</span>';
    if (inst.outcome === 'loss') statusBadge += ' <span class="pb-executed-badge" style="background:#ef444455">Loss -$' + fmt(Math.abs(inst.pnl)) + '</span>';

    const rr = inst.risk_reward ? inst.risk_reward.toFixed(1) + ':1' : '-';

    return `
    <div class="pb-card" id="card-${inst.id}">
        <div class="pb-card-header">
            <div>
                <span class="pb-symbol">${inst.symbol}</span>
                ${statusBadge}
            </div>
            <div style="display:flex;align-items:center;gap:8px;">
                <span class="pb-direction pb-dir-${inst.direction}">${inst.direction}</span>
                <div class="pb-confidence">
                    <div class="pb-conf-bar"><div class="pb-conf-fill ${confClass}" style="width:${conf}%"></div></div>
                    <span style="font-size:12px;color:#8b949e;">${conf}%</span>
                </div>
            </div>
        </div>

        <div class="pb-params">
            <div class="pb-param"><div class="pb-param-label">入场价</div><div class="pb-param-value">$${fmt(inst.entry_price)}</div></div>
            <div class="pb-param"><div class="pb-param-label">止损</div><div class="pb-param-value" style="color:#f87171">$${fmt(inst.stop_loss)}</div></div>
            <div class="pb-param"><div class="pb-param-label">止盈</div><div class="pb-param-value" style="color:#4ade80">$${fmt(inst.take_profit)}</div></div>
            <div class="pb-param"><div class="pb-param-label">股数</div><div class="pb-param-value">${inst.position_size || '-'}</div></div>
            <div class="pb-param"><div class="pb-param-label">风险</div><div class="pb-param-value">$${fmt(inst.risk_amount)}</div></div>
            <div class="pb-param"><div class="pb-param-label">R:R</div><div class="pb-param-value">${rr}</div></div>
        </div>

        ${conditions.length ? `<div class="pb-conditions">${conditions.map(c => `<span class="pb-tag">${c}</span>`).join('')}</div>` : ''}

        ${inst.thesis ? `<div class="pb-thesis">${inst.thesis}</div>` : ''}

        ${risks.length ? `<div class="pb-risks">⚠️ ${risks.join(' | ')}</div>` : ''}

        ${!executed ? `
        <div class="pb-actions">
            <button class="pb-btn pb-btn-execute" onclick="showConfirmModal(${inst.id})">▶ 执行${inst.direction === 'BUY' ? '买入' : '做空'}</button>
            <button class="pb-btn pb-btn-skip" onclick="showSkipOptions(${inst.id})">👎 跳过</button>
        </div>
        <div class="pb-skip-section" id="skip-${inst.id}">
            <div class="pb-skip-options">
                <div class="pb-skip-option" onclick="skipInstruction(${inst.id}, 'risk_too_high')">○ 风险太大</div>
                <div class="pb-skip-option" onclick="skipInstruction(${inst.id}, 'unfamiliar_sector')">○ 不了解该行业</div>
                <div class="pb-skip-option" onclick="skipInstruction(${inst.id}, 'similar_position')">○ 已有类似持仓</div>
                <div class="pb-skip-option" onclick="skipInstruction(${inst.id}, 'insufficient_capital')">○ 资金不足</div>
                <div class="pb-skip-custom">
                    <input type="text" placeholder="自定义原因..." id="custom-skip-${inst.id}"
                           onkeydown="if(event.key==='Enter')skipInstruction(${inst.id}, '', this.value)">
                    <button class="pb-btn pb-btn-skip" style="margin-top:4px;font-size:11px;" onclick="skipInstruction(${inst.id}, '', document.getElementById('custom-skip-${inst.id}').value)">提交</button>
                </div>
            </div>
        </div>
        ` : ''}

        <div class="pb-result-msg" id="result-${inst.id}"></div>
    </div>`;
}

function renderSkipItem(inst) {
    const conditions = parseJSON(inst.conditions_json) || [];
    return `
    <div class="pb-skip-item">
        ${inst.symbol} <span class="pb-dir-SKIP" style="padding:1px 6px;border-radius:3px;font-size:10px;">SKIP</span>
        <span class="pb-skip-reason">${inst.skip_reason || 'N/A'}</span>
        ${conditions.length ? ` — ${conditions.map(c => `<span class="pb-tag" style="font-size:10px">${c}</span>`).join(' ')}` : ''}
    </div>`;
}

// ========== 交互 ==========

function showConfirmModal(instructionId) {
    const inst = currentInstructions.find(i => i.id === instructionId);
    if (!inst) return;

    pendingExecuteId = instructionId;

    const slPct = inst.entry_price ? (((inst.entry_price - inst.stop_loss) / inst.entry_price) * 100).toFixed(1) : '?';
    const tpPct = inst.entry_price ? (((inst.take_profit - inst.entry_price) / inst.entry_price) * 100).toFixed(1) : '?';
    const totalValue = (inst.entry_price || 0) * (inst.position_size || 0);

    document.getElementById('confirmTitle').textContent = `确认执行 ${inst.symbol} ${inst.direction}`;
    document.getElementById('confirmDetails').innerHTML = `
        <div class="pb-confirm-detail">入场 $${fmt(inst.entry_price)} | 止损 $${fmt(inst.stop_loss)} (-${slPct}%) | 止盈 $${fmt(inst.take_profit)} (+${tpPct}%)</div>
        <div class="pb-confirm-detail">${inst.position_size} 股 = $${fmt(totalValue)}</div>
        <div class="pb-confirm-detail">风险 $${fmt(inst.risk_amount)} | R:R ${inst.risk_reward ? inst.risk_reward.toFixed(1) : '-'}:1</div>
        ${inst.thesis ? `<div class="pb-confirm-detail" style="color:#8b949e">${inst.thesis}</div>` : ''}
        <div class="pb-confirm-detail" style="color:#f0883e;margin-top:8px">⚠️ 默认以模拟模式执行 (simulate)</div>
    `;
    document.getElementById('confirmModal').style.display = 'flex';
}

function closeConfirmModal() {
    document.getElementById('confirmModal').style.display = 'none';
    pendingExecuteId = null;
}

async function confirmExecute() {
    if (!pendingExecuteId) return;
    const id = pendingExecuteId;
    closeConfirmModal();

    const btn = document.querySelector(`#card-${id} .pb-btn-execute`);
    if (btn) { btn.disabled = true; btn.textContent = '执行中...'; }

    try {
        const resp = await fetch('/api/playbook/execute', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                instruction_id: id,
                acknowledge_risk: true,
            }),
        });
        const result = await resp.json();

        showResult(id, result.success, result.message || (result.success ? '执行成功' : '执行失败'));
        if (result.success) {
            loadPlaybook(); // Refresh
        } else if (btn) {
            btn.disabled = false;
            btn.textContent = '▶ 执行';
        }
    } catch (e) {
        showResult(id, false, '网络错误: ' + e.message);
        if (btn) { btn.disabled = false; btn.textContent = '▶ 执行'; }
    }
}

function showSkipOptions(id) {
    const el = document.getElementById(`skip-${id}`);
    if (el) el.style.display = el.style.display === 'block' ? 'none' : 'block';
}

async function skipInstruction(id, reason, customReason) {
    try {
        const resp = await fetch('/api/playbook/skip', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                instruction_id: id,
                reason: reason,
                custom_reason: customReason || '',
            }),
        });
        const result = await resp.json();
        showResult(id, result.success, result.message);
        if (result.success) loadPlaybook();
    } catch (e) {
        showResult(id, false, '网络错误: ' + e.message);
    }
}

// ========== 工具函数 ==========

function showResult(id, success, message) {
    const el = document.getElementById(`result-${id}`) || document.getElementById('resultMsg');
    if (!el) return;
    el.style.display = 'block';
    el.className = `pb-result-msg ${success ? 'pb-result-success' : 'pb-result-error'}`;
    el.textContent = message;
    if (success) setTimeout(() => { el.style.display = 'none'; }, 3000);
}

function fmt(n) {
    if (n == null || isNaN(n)) return '-';
    return Number(n).toFixed(2);
}

function parseJSON(val) {
    if (!val) return [];
    if (Array.isArray(val)) return val;
    try { return JSON.parse(val); } catch { return []; }
}
