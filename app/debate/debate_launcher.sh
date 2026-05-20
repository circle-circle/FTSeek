#!/bin/bash
# ============================================================
# FTSeek-US 多 Agent 辩论启动器
# 
# 用法:
#   ./app/debate/debate_launcher.sh premarket    # 盘前辩论
#   ./app/debate/debate_launcher.sh postmarket    # 盘后归因
#
# 功能:
#   1. 启动 Claude Code (deepseek-v4-pro) 后台分析进程
#   2. 输出 Hermes Agent 的分析指令（Hermes 自己读代码做分析）
#   3. 两个 Agent 并行工作，最终由 Hermes 交叉综合
# ============================================================

set -e

MODE="${1:-premarket}"
FTSEEK_DIR="/opt/ftseek/FTSeek-US"
DATE=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR="/tmp/ftseek_debate/${DATE}"
mkdir -p "$OUTPUT_DIR"

echo "=== FTSeek-US 多Agent辩论启动 ==="
echo "模式: $MODE"
echo "输出目录: $OUTPUT_DIR"
echo ""

# ============================================================
# 1. 启动 Claude Code 后台进程
# ============================================================

if [ "$MODE" = "premarket" ]; then
    CLAUDE_PROMPT="你是 FTSeek-US 量化交易系统的红队审计员。请深度分析以下文件并输出 JSON 格式的审计报告:

1. 读取 config/unified_config.yaml，检查:
   - 配置矛盾（同一参数多处定义不同值）
   - 风险阈值设置是否合理
   - 缺失的关键配置
   
2. 读取 app/core/scheduler_core.py 和 app/risk/engine.py，检查:
   - 信号生成逻辑的漏洞
   - 风控状态机的边界条件
   - 并发安全问题
   
3. 读取 data/ 目录下最新的交易日志，检查:
   - 今日持仓集中度
   - 未平仓风险
   - 信号命中率

输出格式 (JSON):
{
    \"risk_findings\": [{\"category\": \"...\", \"severity\": \"P0/P1/P2\", \"detail\": \"...\", \"location\": \"file:line\"}],
    \"config_issues\": [{\"param\": \"...\", \"values\": [...], \"recommendation\": \"...\"}],
    \"code_concerns\": [{\"file\": \"...\", \"issue\": \"...\"}],
    \"market_readiness\": \"READY/CAUTION/HALT\",
    \"summary\": \"一句话总结\"
}

只输出 JSON，不要其他文字。"

elif [ "$MODE" = "postmarket" ]; then
    CLAUDE_PROMPT="你是 FTSeek-US 量化交易系统的架构审计员。请深度分析今日交易数据并输出 JSON 格式的归因报告:

1. 读取 data/trades.db (用 sqlite3)，查询:
   - 今日所有交易 (SELECT * FROM trades WHERE date = today)
   - 盈亏统计
   - 止损/止盈触发情况
   
2. 读取 app/risk/position_exit_monitor.py 和 app/risk/engine.py，检查:
   - 退出逻辑是否按配置执行
   - 是否有状态机异常
   - 熔断是否被正确触发
   
3. 对比 DB 持仓与 Futu API 持仓 (检查是否有不一致)
   用命令: python -c \"from app.core.position_recovery import ...;\" 或直接查 DB

输出格式 (JSON):
{
    \"trade_summary\": {\"total\": N, \"wins\": N, \"losses\": N, \"win_rate\": 0.0, \"total_pnl\": 0.0},
    \"attribution\": [{\"symbol\": \"...\", \"pnl\": 0.0, \"reason\": \"信号质量/执行/市场\"}],
    \"systemic_issues\": [{\"issue\": \"...\", \"severity\": \"P0/P1/P2\"}],
    \"data_consistency\": \"OK/MISMATCH\",
    \"next_day_actions\": [\"action1\", \"action2\"],
    \"summary\": \"一句话总结\"
}

只输出 JSON，不要其他文字。"
fi

echo "启动 Claude Code (deepseek-v4-pro) 后台进程..."

cd "$FTSEEK_DIR"

# 关键: 使用 claude-runner 非 root 用户 + --dangerously-skip-permissions 完全跳过审批
# root 用户使用 --dangerously-skip-permissions 会被 Claude Code 安全策略拒绝
# 先把 prompt 写入临时文件，避免复杂的引号转义问题
PROMPT_FILE="/tmp/ftseek_debate_prompt_${DATE}.txt"
echo "$CLAUDE_PROMPT" > "$PROMPT_FILE"
chmod 644 "$PROMPT_FILE"

su - claude-runner -c "cd $FTSEEK_DIR && timeout 600 claude -p \"\$(cat $PROMPT_FILE)\" --dangerously-skip-permissions" \
    > "$OUTPUT_DIR/claude_output.txt" 2>&1 &

CLAUDE_PID=$!
echo "Claude Code PID: $CLAUDE_PID"
echo "等待 Claude Code 完成 (最多 10 分钟)..."

# 等待 Claude 完成，同时输出进度
WAIT_COUNT=0
while kill -0 $CLAUDE_PID 2>/dev/null; do
    sleep 10
    WAIT_COUNT=$((WAIT_COUNT + 10))
    if [ $WAIT_COUNT -ge 600 ]; then
        echo "⚠️ Claude Code 超时 (10分钟)，终止进程"
        kill $CLAUDE_PID 2>/dev/null || true
        break
    fi
    # 每 30s 输出一次进度
    if [ $((WAIT_COUNT % 30)) -eq 0 ]; then
        echo "  ... 等待中 (${WAIT_COUNT}s), CPU: $(ps -p $CLAUDE_PID -o %cpu= 2>/dev/null || echo 'N/A')"
    fi
done

wait $CLAUDE_PID 2>/dev/null
CLAUDE_EXIT=$?

echo "Claude Code 完成 (退出码: $CLAUDE_EXIT)"
echo ""

# ============================================================
# 2. 提取 Claude 结果
# ============================================================

echo "=== Claude Code 输出 ==="
if [ -f "$OUTPUT_DIR/claude_output.txt" ]; then
    # 使用独立的 Python 脚本提取 JSON，避免 bash 引号/反引号冲突
    OUTPUT_PATH="$OUTPUT_DIR/claude_output.txt" python3 << 'PYEOF'
import json, sys, os

output_path = os.environ.get("OUTPUT_PATH", "/tmp/ftseek_debate/claude_output.txt")
with open(output_path) as f:
    text = f.read()

# 清理可能的 markdown 包装
if "```json" in text:
    text = text.split("```json", 1)[1]
    if "```" in text:
        text = text.rsplit("```", 1)[0]
elif text.strip().startswith("```"):
    text = text.strip()[3:]
    if text.endswith("```"):
        text = text[:-3]

text = text.strip()
try:
    data = json.loads(text)
    print(json.dumps(data, indent=2, ensure_ascii=False))
except Exception:
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end+1])
            print(json.dumps(data, indent=2, ensure_ascii=False))
        except Exception:
            print(text[:2000])
    else:
        print(text[:2000])
PYEOF
else
    echo "Claude 输出文件不存在"
fi

echo ""
echo "=== Claude 原始输出保存于: $OUTPUT_DIR/claude_output.txt ==="
echo ""

# ============================================================
# 3. 输出 Hermes Agent 分析指令
# ============================================================

cat << 'HERMES_EOF'
=== Hermes Agent 分析指令 ===

Claude Code (deepseek-v4-pro) 已完成独立分析。现在请你作为第二个独立 Agent，
使用你的工具链对同一段代码/配置进行独立分析，然后与 Claude 的结果交叉验证。

请执行以下分析:

盘前模式:
1. 读 config/unified_config.yaml 的配置摘要
2. 用 search_files 搜索关键配置的引用位置
3. 读 data/ 目录下的持仓和交易日志
4. 用 terminal 运行: sqlite3 data/trades.db 查持仓
5. 独立生成盘前分析报告

盘后模式:
1. 读 data/ 下的交易记录
2. 用 terminal 运行 sqlite3 查询今日交易统计
3. 读 risk/ 目录下的退出监控代码
4. 检查日志中的 ERROR/CRITICAL
5. 独立生成归因分析报告

然后交叉综合:
1. 对比你的发现与 Claude 的发现
2. 标注共识项和分歧项
3. 输出最终辩论报告
HERMES_EOF

echo ""
echo "=== 辩论启动完成 ==="
echo "Claude 结果: $OUTPUT_DIR/claude_output.txt"
echo "Hermes Agent 现在执行独立分析，然后综合..."
