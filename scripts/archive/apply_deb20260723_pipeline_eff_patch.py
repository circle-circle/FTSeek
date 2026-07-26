#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DEB-20260723-da-pipeline-efficiency — 方案 F + A 一键补丁 (三轮辩论共识, 2026-07-23)
================================================================================================
Qoder 在 Auto 权限模式下对 /root/.hermes 只可读不可写, 故将已共识变更 (F 先落先验 → A)
打包为本脚本: 锚点逐一断言 + 改动前备份 + ast 语法校验 + 幂等跳过 + 失败回滚。
沿用 apply_deb20260723_hermes_patch.py 的同一机制。

用法:
  # 干跑 (默认): 只验证锚点与展示计划, 不写任何文件
  python3 scripts/apply_deb20260723_pipeline_eff_patch.py

  # 实际执行 (自动备份)
  python3 scripts/apply_deb20260723_pipeline_eff_patch.py --apply

  # 对镜像副本演练
  python3 scripts/apply_deb20260723_pipeline_eff_patch.py --root data/_tmp_hermes_mirror --apply

  # 回滚: 恢复备份
  python3 scripts/apply_deb20260723_pipeline_eff_patch.py --rollback

变更清单 (全部零 live 交易行为变更; 时间/成本优化):
  F  llm_synth.py : _call_llm 把 API usage(prompt/completion tokens) 注入结果 _usage 字段
                    (只读元数据, 补齐成本观测地基, 前置于 A)
  A  llm_synth.py : _run_challenger 的三次采样从串行改并行 (ThreadPoolExecutor);
                    额外并发 1 次 temp=0 对照采样, 落 _temp0_sample + _debate.challenge_direction_temp0
                    硬断言: ① temp=0.4 补采按提交序收集 (禁用 as_completed 选 live), live=第1个提交调用;
                            ② 补采/对照异常→跳过, samples 长度可 <3 但不影响 live;
                    收益: 消除三次采样引入的 +28min 回到 ~67min 基线; temp0 对照供 07-30 裁决
                          区分"纯采样噪声(降 temp 可解)" vs "证据矛盾(应降置信度)"
"""
from __future__ import annotations

import argparse
import ast
import shutil
import sys
from pathlib import Path

SKILL_DEFAULT = Path("/root/.hermes/skills/deep-stock-analysis")
BAK_SUFFIX = ".bak-DEB20260723-effpatch"

# ── 替换规则: (相对路径, 标记(幂等), old, new) ─────────────────────────────
RULES = [
    # F: _call_llm 注入 usage (紧跟已存在的 _model_reported 注入之后)
    ("scripts/llm_synth.py", "_usage",
     '        if isinstance(out, dict):\n'
     '            out.setdefault("_model_reported", data.get("model"))\n'
     "        return out",
     '        if isinstance(out, dict):\n'
     '            out.setdefault("_model_reported", data.get("model"))\n'
     "            # [DEB-20260723 方案F] token usage 落库: 补齐成本观测地基, 只读零行为变更;\n"
     "            #   方案B 裁决三次采样成本时须真实 token 数, 非 56s 时间估计.\n"
     '            out.setdefault("_usage", data.get("usage"))\n'
     "        return out"),
    # A: 三次采样并行化 (串行 for 循环 → ThreadPoolExecutor, 按提交序收集)
    ("scripts/llm_synth.py", "方案A 并行采样",
     "        if result and \"_error\" not in result:\n"
     "            # [DEB-20260723 H5 用户批准] 采样稳定性 shadow: 同输入再采样 2 次, 方向序列落库测翻转率;\n"
     "            #   live 仍用第 1 次结果, 行为零变更. 一周后翻转率高再议 3 取众数切换 (另批).\n"
     '            _samples = [str(result.get("my_direction", "?")).upper()]\n'
     "            for _i in range(2):\n"
     "                _r2 = _call_llm(challenger_system, challenger_user,\n"
     "                                temperature=0.4, max_tokens=2000, model=CHALLENGER_MODEL)\n"
     '                if _r2 and "_error" not in _r2:\n'
     '                    _samples.append(str(_r2.get("my_direction", "?")).upper())\n'
     '            result["_direction_samples"] = _samples\n'
     "            return result",
     "        if result and \"_error\" not in result:\n"
     "            # [DEB-20260723 H5 用户批准] 采样稳定性 shadow: 同输入再采样 2 次, 方向序列落库测翻转率;\n"
     "            #   live 仍用第 1 次结果, 行为零变更. 一周后翻转率高再议 3 取众数切换 (另批).\n"
     "            # [DEB-20260723 方案A 并行采样] 2 次 temp=0.4 补采 + 1 次 temp=0 对照, 与主采并发发出\n"
     "            #   (I/O 密集, GIL 无碍), 消除 +28min; 墙钟时间不增.\n"
     "            #   硬断言: temp=0.4 补采按提交序收集 (禁用 as_completed 选 live) → live=第1个提交调用,\n"
     "            #           与串行逐位对齐; 补采异常/None → 跳过, samples 长度可 <3, live 第1采不受影响.\n"
     "            # [DEB-20260723 temp0 对照, 用户批准] 额外 1 次 temp=0 采样单独落 _temp0_sample;\n"
     "            #   07-30 裁决判据: temp=0.4 翻转但 temp=0 不翻→纯采样噪声(降 temp 可解);\n"
     "            #   temp=0 仍翻→证据矛盾(应降置信度而非降 temp, MoE 架构 temp=0 亦非确定性).\n"
     "            import concurrent.futures as _cf\n"
     '            _samples = [str(result.get("my_direction", "?")).upper()]\n'
     "            with _cf.ThreadPoolExecutor(max_workers=3) as _ex:\n"
     "                _futs = [_ex.submit(_call_llm, challenger_system, challenger_user,\n"
     "                                    temperature=0.4, max_tokens=2000, model=CHALLENGER_MODEL)\n"
     "                         for _i in range(2)]\n"
     "                _fut_t0 = _ex.submit(_call_llm, challenger_system, challenger_user,\n"
     "                                     temperature=0.0, max_tokens=2000, model=CHALLENGER_MODEL)\n"
     "                for _f in _futs:  # 提交序遍历, 非 as_completed → 顺序确定\n"
     "                    try:\n"
     "                        _r2 = _f.result()\n"
     "                    except Exception:\n"
     "                        _r2 = None\n"
     '                    if _r2 and "_error" not in _r2:\n'
     '                        _samples.append(str(_r2.get("my_direction", "?")).upper())\n'
     "                try:\n"
     "                    _rt0 = _fut_t0.result()\n"
     "                except Exception:\n"
     "                    _rt0 = None\n"
     '                if _rt0 and "_error" not in _rt0:\n'
     '                    result["_temp0_sample"] = str(_rt0.get("my_direction", "?")).upper()\n'
     '            result["_direction_samples"] = _samples\n'
     "            return result"),
    # A-c: _debate dict 增 temp0 对照采样 (紧跟 challenge_direction_samples)
    ("scripts/llm_synth.py", "challenge_direction_temp0",
     '        "challenge_direction_samples": challenge.get("_direction_samples"),\n'
     "    }",
     '        "challenge_direction_samples": challenge.get("_direction_samples"),\n'
     '        "challenge_direction_temp0": challenge.get("_temp0_sample"),\n'
     "    }"),
]

PY_FILES = ("scripts/llm_synth.py",)


def _apply_rules(root: Path, apply: bool) -> bool:
    ok = True
    for rel, marker, old, new in RULES:
        path = root / rel
        tag = f"{rel} :: {marker}"
        if not path.exists():
            print(f"[FAIL] {tag}: 文件不存在 {path}")
            ok = False
            continue
        text = path.read_text(encoding="utf-8")
        if marker in text:
            print(f"[skip] {tag}: 标记已存在 (幂等跳过)")
            continue
        cnt = text.count(old)
        if cnt != 1:
            print(f"[FAIL] {tag}: 锚点出现 {cnt} 次 (期望 1) — 文件可能已被改动, 需人工核对")
            ok = False
            continue
        if apply:
            bak = path.with_name(path.name + BAK_SUFFIX)
            if not bak.exists():
                shutil.copy2(path, bak)
            path.write_text(text.replace(old, new, 1), encoding="utf-8")
            print(f"[done] {tag}: 已替换 (备份 {bak.name})")
        else:
            print(f"[plan] {tag}: 锚点命中, 待 --apply")
    if apply:
        for rel in PY_FILES:
            p = root / rel
            try:
                ast.parse(p.read_text(encoding="utf-8"))
                print(f"[ok]   {rel}: ast 语法校验通过")
            except SyntaxError as e:
                print(f"[FAIL] {rel}: 语法错误 {e} — 请立即 --rollback")
                ok = False
    return ok


def _rollback(root: Path) -> None:
    for rel in PY_FILES:
        p = root / rel
        bak = p.with_name(p.name + BAK_SUFFIX)
        if bak.exists():
            shutil.copy2(bak, p)
            print(f"[rollback] {rel}: 已恢复 {bak.name}")
        else:
            print(f"[skip] {rel}: 无备份")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(SKILL_DEFAULT), help="skill 根目录 (演练时指镜像)")
    ap.add_argument("--apply", action="store_true", help="实际写入 (默认干跑)")
    ap.add_argument("--rollback", action="store_true", help="从备份恢复后退出")
    args = ap.parse_args()
    root = Path(args.root)

    if args.rollback:
        _rollback(root)
        return 0

    print(f"目标 root: {root}   模式: {'APPLY' if args.apply else 'DRY-RUN'}")
    ok = _apply_rules(root, apply=args.apply)
    print("结果:", "全部通过" if ok else "存在失败项 (见上)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
