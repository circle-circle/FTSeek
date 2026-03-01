#!/usr/bin/env python3.8
"""
测试 query_signals.py 脚本的参数解析功能
"""
import subprocess
import sys


def test_help():
    """测试 --help 显示所有参数说明"""
    result = subprocess.run(
        ['python3.8', '/opt/ftseek/FTSeek/scripts/query_signals.py', '--help'],
        capture_output=True,
        text=True
    )
    
    assert result.returncode == 0, f"--help 应该返回 0，实际返回 {result.returncode}"
    
    # 检查所有必需参数是否在帮助信息中
    required_args = [
        '--stock',
        '--from',
        '--to',
        '--status',
        '--strategy',
        '--limit',
        '--format',
        '--output',
        '--daily-report',
        '--weekly-report'
    ]
    
    for arg in required_args:
        assert arg in result.stdout, f"帮助信息中缺少参数: {arg}"
    
    print("✓ test_help 通过")


def test_no_args():
    """测试无参数运行（使用默认值）"""
    result = subprocess.run(
        ['python3.8', '/opt/ftseek/FTSeek/scripts/query_signals.py', '--limit', '5'],
        capture_output=True,
        text=True
    )
    
    # 应该不报错（返回 0 或因为没有数据而返回 0）
    assert result.returncode == 0, f"无参数运行应该不报错，stderr: {result.stderr}"
    
    print("✓ test_no_args 通过")


def test_stock_filter():
    """测试股票代码过滤参数"""
    result = subprocess.run(
        ['python3.8', '/opt/ftseek/FTSeek/scripts/query_signals.py', 
         '--stock', 'HK0700', '--limit', '5'],
        capture_output=True,
        text=True
    )
    
    assert result.returncode == 0, f"股票过滤参数应该有效，stderr: {result.stderr}"
    print("✓ test_stock_filter 通过")


def test_date_range():
    """测试日期范围参数"""
    result = subprocess.run(
        ['python3.8', '/opt/ftseek/FTSeek/scripts/query_signals.py',
         '--from', '2026-02-01', '--to', '2026-02-28', '--limit', '5'],
        capture_output=True,
        text=True
    )
    
    assert result.returncode == 0, f"日期范围参数应该有效，stderr: {result.stderr}"
    print("✓ test_date_range 通过")


def test_status_filter():
    """测试状态过滤参数"""
    for status in ['pending', 'executed', 'ignored']:
        result = subprocess.run(
            ['python3.8', '/opt/ftseek/FTSeek/scripts/query_signals.py',
             '--status', status, '--limit', '5'],
            capture_output=True,
            text=True
        )
        assert result.returncode == 0, f"状态过滤 {status} 应该有效，stderr: {result.stderr}"
    
    print("✓ test_status_filter 通过")


def test_format_options():
    """测试输出格式参数"""
    for fmt in ['table', 'json', 'csv']:
        result = subprocess.run(
            ['python3.8', '/opt/ftseek/FTSeek/scripts/query_signals.py',
             '--format', fmt, '--limit', '5'],
            capture_output=True,
            text=True
        )
        assert result.returncode == 0, f"格式 {fmt} 应该有效，stderr: {result.stderr}"
    
    print("✓ test_format_options 通过")


def test_limit_parameter():
    """测试限制条数参数"""
    result = subprocess.run(
        ['python3.8', '/opt/ftseek/FTSeek/scripts/query_signals.py',
         '--limit', '10'],
        capture_output=True,
        text=True
    )
    
    assert result.returncode == 0, f"limit 参数应该有效，stderr: {result.stderr}"
    print("✓ test_limit_parameter 通过")


def test_report_modes():
    """测试日报/周报模式"""
    # 日报模式（需要 output 参数）
    result = subprocess.run(
        ['python3.8', '/opt/ftseek/FTSeek/scripts/query_signals.py',
         '--daily-report', '--output', '/tmp/daily_test.json'],
        capture_output=True,
        text=True
    )
    # 可能因为没有数据而返回 0，但不应该报错
    assert 'Traceback' not in result.stderr, f"日报模式不应该报错：{result.stderr}"
    
    # 周报模式
    result = subprocess.run(
        ['python3.8', '/opt/ftseek/FTSeek/scripts/query_signals.py',
         '--weekly-report', '--output', '/tmp/weekly_test.json'],
        capture_output=True,
        text=True
    )
    assert 'Traceback' not in result.stderr, f"周报模式不应该报错：{result.stderr}"
    
    print("✓ test_report_modes 通过")


if __name__ == '__main__':
    print("运行 query_signals.py 参数解析测试...\n")
    
    tests = [
        test_help,
        test_no_args,
        test_stock_filter,
        test_date_range,
        test_status_filter,
        test_format_options,
        test_limit_parameter,
        test_report_modes,
    ]
    
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as e:
            print(f"✗ {test.__name__} 失败：{e}")
            failed += 1
        except Exception as e:
            print(f"✗ {test.__name__} 异常：{e}")
            failed += 1
    
    print(f"\n{'='*50}")
    if failed == 0:
        print(f"所有 {len(tests)} 个测试通过！")
        sys.exit(0)
    else:
        print(f"{failed}/{len(tests)} 个测试失败")
        sys.exit(1)
