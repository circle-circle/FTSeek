#!/bin/bash
# FTSeek-US 管理脚本

ACTION=$1
FTSEEK_US_DIR="/opt/ftseek/FTSeek-US"
# P1-4 修复 2026-04-26: 使用项目 venv python，避免系统 python 依赖版本漂移
PYTHON="$FTSEEK_US_DIR/venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    echo "❌ venv python 不存在: $PYTHON"
    echo "请先执行: cd $FTSEEK_US_DIR && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi
MAIN_PY="$FTSEEK_US_DIR/main.py"

case "$ACTION" in
    start)
        echo "启动 FTSeek-US..."
        cd $FTSEEK_US_DIR
        nohup $PYTHON $MAIN_PY --start > logs/ftseek_us.log 2>&1 &
        echo "✅ FTSeek-US 已启动 (PID: $!)"
        echo "日志：tail -f logs/ftseek_us.log"
        ;;
    
    stop)
        echo "停止 FTSeek-US..."
        # [DEB-fullchain #25 2026-08-16] 原 pkill -f "ftseek_us" 会匹配任何含该串的
        #   命令行 (本脚本自身/执行脚本的 shell/tail 日志/其他 agent 会话) → 自杀式误杀。
        #   精确匹配主进程命令行: venv python main.py --start
        # DEB-20260903 #12: 模式含项目路径 — 原 "main.py --start" 可误杀同机
        #   其他含同名参数的 Python 项目进程
        pkill -f "$MAIN_PY --start"
        echo "✅ FTSeek-US 已停止"
        ;;
    
    restart)
        $0 stop
        # DEB-20260904 #139: 等待旧进程真正退出 — 原固定 sleep 2,
        # 优雅关闭 (持仓对账/DB flush) 可能 >2s, 竞争导致重启失败或双实例
        for _i in $(seq 1 30); do
            pgrep -f "$MAIN_PY --start" > /dev/null || break
            sleep 1
        done
        if pgrep -f "$MAIN_PY --start" > /dev/null; then
            echo "❌ 旧进程 30s 内未退出, 放弃启动 (避免双实例), 请人工检查"
            exit 1
        fi
        $0 start
        ;;
    
    status)
        # DEB-20260903 #12: 同 stop, 模式含项目路径防误报
        if pgrep -f "$MAIN_PY --start" > /dev/null; then
            PID=$(pgrep -f "$MAIN_PY --start")
            echo "✅ FTSeek-US 运行中 (PID: $PID)"
            echo ""
            echo "最近日志:"
            tail -5 logs/ftseek_us.log
        else
            echo "❌ FTSeek-US 未运行"
        fi
        ;;
    
    test)
        echo "测试连接..."
        $PYTHON $MAIN_PY --test
        ;;
    
    logs)
        tail -f $FTSEEK_US_DIR/logs/ftseek_us.log
        ;;
    
    *)
        echo "用法：$0 {start|stop|restart|status|test|logs}"
        exit 1
        ;;
esac
