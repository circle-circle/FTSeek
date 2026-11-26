#!/usr/bin/env python3
"""pre-commit 助手: 检测 staged Python 文件中的真·undefined name (运行时 NameError).

背景: 2026-07-16 全仓扫描发现 13 处 undefined-name 潜伏 bug (多被 try/except 静默吞掉),
  py_compile 语法检查抓不到. 本脚本用 pyflakes 补这个洞.

只拦截真问题, 忽略字符串前向注解 (如 def f(x: "np.ndarray") — pyflakes 会报但运行时无害).

用法: check_undefined_names.py <file1.py> <file2.py> ...
退出码: 0=通过, 1=发现真 undefined name, 2=检查工具不可用或执行失败.
"""
import sys
import io

try:
    from pyflakes import api as pyflakes_api
    from pyflakes import reporter as pyflakes_reporter
    from pyflakes.messages import UndefinedName
except Exception as exc:
    # 检查工具不可用是门禁故障，不能静默放行；安装 requirements.lock 后重试。
    print(f"❌ check_undefined_names: pyflakes 不可用 ({type(exc).__name__}: {exc}); "
          "请先 pip install -r requirements.lock", file=sys.stderr)
    sys.exit(2)


def _line_col_in_string_annotation(source_lines, lineno, col, name):
    """判断 lineno:col 处的 name 是否位于字符串字面量内 (前向注解)。

    简化判据: 该行中 name 的所有出现是否都被引号包裹。
    pyflakes 对字符串注解 (PEP 563 deferred) 里的未定义名也会报 F821,
    但这些在运行时不求值, 属误报。
    """
    if lineno < 1 or lineno > len(source_lines):
        return False
    line = source_lines[lineno - 1]
    # 该行是否存在被引号包裹的 name (如 "np.ndarray" / 'ConfigContainer')
    quoted = (f'"{name}' in line) or (f"'{name}" in line)
    # 且不存在裸用 (name 后紧跟 . 或 ( 且前面不是引号) — 保守: 只要有裸用就当真 bug
    bare = False
    idx = 0
    while True:
        idx = line.find(name, idx)
        if idx == -1:
            break
        prev = line[idx - 1] if idx > 0 else ""
        if prev not in ("'", '"'):
            bare = True
            break
        idx += len(name)
    return quoted and not bare


class _Collector:
    def __init__(self):
        self.hits = []  # (filename, lineno, col, name)

    def unexpectedError(self, filename, msg):
        pass

    def syntaxError(self, filename, msg, lineno, offset, text):
        # 语法错误交给 py_compile 处理, 这里不重复报
        pass

    def flake(self, message):
        if isinstance(message, UndefinedName):
            name = message.message_args[0] if message.message_args else "?"
            self.hits.append((message.filename, message.lineno, message.col, name))


def main(argv):
    files = [f for f in argv[1:] if f.endswith(".py")]
    if not files:
        return 0

    collector = _Collector()
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                src = fh.read()
        except OSError as e:
            print(f"⚠️  check_undefined_names: 读不了 {f} ({e}), 跳过", file=sys.stderr)
            continue
        pyflakes_api.check(src, f, reporter=collector)

    # 过滤掉字符串注解误报
    real = []
    src_cache = {}
    for filename, lineno, col, name in collector.hits:
        if filename not in src_cache:
            try:
                with open(filename, "r", encoding="utf-8") as fh:
                    src_cache[filename] = fh.read().splitlines()
            except OSError:
                src_cache[filename] = []
        if _line_col_in_string_annotation(src_cache[filename], lineno, col, name):
            continue
        real.append((filename, lineno, name))

    if real:
        print("╔══════════════════════════════════════════════╗")
        print("║  ❌ GOVERNANCE: 发现 undefined name (运行时 NameError)  ║")
        print("╚══════════════════════════════════════════════╝")
        print("")
        for filename, lineno, name in real:
            print(f"  {filename}:{lineno}: undefined name '{name}'")
        print("")
        print("这类 bug 常被 try/except 静默吞掉, 但会导致功能失效.")
        print("修复缺失的 import/变量后重新提交, 或 git commit --no-verify 跳过 (不推荐).")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
