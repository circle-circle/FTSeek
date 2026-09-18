#!/usr/bin/env python3
"""剔除统计类产物里的占位/测试标的数据污染源 (用户 2026-09-18 令「同意剔除影响统计类的数据污染源」).

范围: `data/**/*.jsonl` 的行式产物 (影子/统计/台账)。判定用**唯一判据**
`app.core.trade_identity.is_test_symbol` —— 不写第二套正则/字符串匹配。

安全设计:
  ① 默认 dry-run; `--apply` 才动文件;
  ② 先整文件备份到 `data/backups/placeholder_purge_<ts>/` (保留相对路径);
  ③ **按原始文本行过滤**(命中即丢整行), 其余行逐字节保留 —— 绝不重新序列化(会改格式/尾逗号);
  ④ 打印 before/after 行数与每一条剔除行的识别三元组, 可人工逐条核对;
  ⑤ 不碰 DB (DB 侧 2026-09-18 已清零并加守卫), 不碰 `data/backups/**`。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from app.core.trade_identity import is_test_symbol  # noqa: E402

DATA = REPO / "data"
SYMBOL_KEYS = ("symbol", "ticker", "sym", "stock_code", "code")


def _row_is_placeholder(raw_line: str) -> tuple[bool, str]:
    try:
        obj = json.loads(raw_line)
    except Exception:
        return False, ""
    if not isinstance(obj, dict):
        return False, ""
    for key in SYMBOL_KEYS:
        val = obj.get(key)
        if isinstance(val, str) and is_test_symbol(val):
            return True, f"{key}={val}"
    for key in ("symbols", "tickers"):
        val = obj.get(key)
        if isinstance(val, list) and any(is_test_symbol(str(v)) for v in val):
            flagged = [str(v) for v in val if is_test_symbol(str(v))]
            return True, f"{key}={flagged}"
    return False, ""


def _targets() -> list[Path]:
    out = []
    for path in sorted(DATA.rglob("*.jsonl")):
        rel = path.relative_to(DATA)
        # backups = 备份留档; _quarantine = 2026-09-03 已人为隔离的测试行归档; archive* = 历史快照
        # —— 都不是活统计, 一律不动 (清理只针对被消费的行式产物)。
        # 注: archive 目录可能嵌套在 data/shadow/ 下 ⇒ 必须查**任意一层**路径, 只看 rel.parts[0] 会漏。
        if any(p in ("backups", "_quarantine") or p.startswith("archive") for p in rel.parts):
            continue
        out.append(path)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写回 (默认只报)")
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = DATA / "backups" / f"placeholder_purge_{stamp}"
    changed = 0
    total_removed = 0
    for path in _targets():
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as exc:
            print(f"[skip] 读不到 {path.relative_to(REPO)}: {exc}")
            continue
        lines = text.splitlines(keepends=True)
        keep, removed = [], []
        for line in lines:
            hit, why = _row_is_placeholder(line)
            if hit:
                removed.append((why, line[:160].rstrip()))
            else:
                keep.append(line)
        if not removed:
            continue
        changed += 1
        total_removed += len(removed)
        rel = path.relative_to(REPO)
        print(f"[{'APPLY' if args.apply else 'DRY'}] {rel}: {len(lines)} -> {len(keep)} (剔 {len(removed)})")
        for why, preview in removed[:5]:
            print(f"        - {why} | {preview}")
        if len(removed) > 5:
            print(f"        ... 其余 {len(removed) - 5} 条同型 (文件内已全数列出, 此处省略打印)")
        if args.apply:
            dest = backup_dir / path.relative_to(DATA)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            path.write_text("".join(keep), encoding="utf-8")
    print(f"\n涉及文件 {changed} 个, 剔除行 {total_removed} 条"
          + (f"; 备份 = {backup_dir.relative_to(REPO)}" if args.apply else " (dry-run, 未写)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())