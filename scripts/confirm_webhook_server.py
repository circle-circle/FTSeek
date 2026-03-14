#!/usr/bin/env python3
"""
交易确认 Webhook 服务器

接收钉钉按钮回调，执行交易确认

使用方式:
    python3 scripts/confirm_webhook_server.py --port 8888
"""
import sys
import os
import logging
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config
from scripts.trade_executor_manual import ManualTradeExecutor

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger("ftseek.webhook")


class ConfirmHandler(BaseHTTPRequestHandler):
    """处理确认请求"""
    
    def do_GET(self):
        """处理 GET 请求"""
        parsed = urlparse(self.path)
        
        if parsed.path == '/confirm':
            params = parse_qs(parsed.query)
            signal_id = params.get('signal_id', [None])[0]
            action = params.get('action', [None])[0]
            mode = params.get('mode', ['paper'])[0]
            
            if not signal_id or not action:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'error': 'Missing signal_id or action'
                }).encode())
                return
            
            # 执行交易
            logger.info(f"收到确认请求：{signal_id} {action} {mode}")
            
            executor = ManualTradeExecutor()
            success = executor.execute(signal_id, action)
            
            # 返回结果
            self.send_response(200 if success else 400)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                'success': success,
                'signal_id': signal_id,
                'action': action,
                'mode': mode
            }).encode())
        
        elif parsed.path == '/health':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok'}).encode())
        
        else:
            self.send_response(404)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'Not found'}).encode())
    
    def log_message(self, format, *args):
        logger.info(f"{self.address_string()} - {format % args}")


def run_server(port=8888):
    """启动服务器"""
    server = HTTPServer(('0.0.0.0', port), ConfirmHandler)
    logger.info(f"Webhook 服务器已启动，监听端口 {port}")
    logger.info(f"确认 URL: http://<服务器 IP>:{port}/confirm?signal_id=xxx&action=BUY")
    logger.info(f"健康检查：http://localhost:{port}/health")
    server.serve_forever()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='交易确认 Webhook 服务器')
    parser.add_argument('--port', type=int, default=8888, help='监听端口')
    args = parser.parse_args()
    
    run_server(args.port)
