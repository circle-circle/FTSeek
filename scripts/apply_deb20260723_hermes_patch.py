#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DEB-20260723-da-ue-prompt-optimization — hermes 侧 + crontab 移交项一键补丁 (用户批准, 2026-07-23)
================================================================================================
Qoder 在 Auto 权限模式下对 /root/.hermes 与 crontab 只可读不可写, 故将全部已批准变更
(H0-H5.2) 打包为本脚本: 锚点逐一断言 + 改动前备份 + ast 语法校验 + 幂等跳过 + 失败回滚。

用法:
  # 干跑 (默认): 只验证锚点与展示计划, 不写任何文件
  python3 scripts/apply_deb20260723_hermes_patch.py

  # 实际执行 (自动备份, 含 crontab 追加)
  python3 scripts/apply_deb20260723_hermes_patch.py --apply

  # 对镜像副本演练 (Qoder 已用此方式验证全部锚点)
  python3 scripts/apply_deb20260723_hermes_patch.py --root data/_tmp_hermes_mirror --no-cron --apply

  # 回滚: 恢复备份
  python3 scripts/apply_deb20260723_hermes_patch.py --rollback

变更清单 (全部 record-only / 加法式, 零 live 交易行为变更):
  H0   crontab 追加 UE score (05:40 CST 周二-六)
  H1   pipeline.py  : bridge_data 增 skill_version (解析 SKILL.md frontmatter)
  H2   SKILL.md     : version 0.58→0.61 + changelog 前置 v0.61 条目
  H3   pipeline.py  : cand 增 confidence_semantic (WARN 前的语义置信度双写, 弃权=0.50)
  H4   llm_synth.py : _call_llm 注入 _model_reported; 挑战者 prompt 增 skip_reason 必填;
                      _debate 增 challenger_model_reported / challenger_skip_reason
  H5.2 llm_synth.py : _run_challenger 同输入再采样 2 次, 方向序列落库 (live 仍用第 1 次)
"""
from __future__ import annotations

import argparse
import ast
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_DEFAULT = Path("/root/.hermes/skills/deep-stock-analysis")
BAK_SUFFIX = ".bak-DEB20260723"

CRON_COMMENT = "# UE score 接电 (P0.1-a, DEB-20260723-da-ue-prompt-optimization): 美东收盘后评分前一日 scan; 回滚=删除本行"
CRON_LINE = ("40 5 * * 2-6 cd /opt/ftseek/FTSeek-US && PYTHONPATH=. "
             "/opt/ftseek/FTSeek-US/venv/bin/python scripts/llm_universe_scan.py score "
             ">> /opt/ftseek/FTSeek-US/logs/universe_scan.log 2>&1")

# ── 替换规则: (相对路径, 标记(幂等), old, new) ─────────────────────────────
RULES = [
    # H2-a: SKILL.md 版本对齐
    ("SKILL.md", "version: 0.61",
     "version: 0.58",
     "version: 0.61"),
    # H2-b: changelog 前置 v0.61
    ("SKILL.md", "v0.61: skill_version桥戳",
     "\n  v0.60: 交易哲学定调",
     "\n  v0.61: skill_version桥戳+conf_semantic双写+挑战者SKIP理由+模型回显落库"
     "+挑战者三次采样shadow (DEB-20260723, 用户批准).\n  v0.60: 交易哲学定调"),
    # H1-a: pipeline.py 版本戳解析块 (插在 _today_str 与 data_dir 之间)
    ("scripts/pipeline.py", "_skill_version",
     '        _today_str = _now_et.strftime("%Y-%m-%d")\n\n'
     '        data_dir = "/opt/ftseek/FTSeek-US/data"',
     '        _today_str = _now_et.strftime("%Y-%m-%d")\n\n'
     "        # [DEB-20260723 P0.2-版本戳] bridge 归因地基, 前置一切 skill 侧变更;\n"
     "        #   单一真源 = SKILL.md frontmatter version 字段; 解析失败落 unknown 不阻塞.\n"
     '        _skill_version = "unknown"\n'
     "        try:\n"
     "            import re as _re_sv, pathlib as _pl_sv\n"
     '            _sk = _pl_sv.Path(__file__).resolve().parents[1] / "SKILL.md"\n'
     '            _m = _re_sv.search(r"(?m)^version:\\s*(\\S+)", _sk.read_text(encoding="utf-8"))\n'
     "            if _m:\n"
     "                _skill_version = _m.group(1)\n"
     "        except Exception:\n"
     "            pass\n\n"
     '        data_dir = "/opt/ftseek/FTSeek-US/data"'),
    # H1-b: bridge_data 增 skill_version
    ("scripts/pipeline.py", '"skill_version": _skill_version',
     '        bridge_data = {\n'
     '            "trade_date": _today_str,\n'
     '            "generated_at": _now_et.isoformat(),',
     '        bridge_data = {\n'
     '            "trade_date": _today_str,\n'
     '            "generated_at": _now_et.isoformat(),\n'
     '            "skill_version": _skill_version,'),
    # H3-a: WARN 分支记录语义置信度
    ("scripts/pipeline.py", "_semantic_confidence",
     '            if _gate_decision == "WARN":\n'
     "                _vr_pre = _adapter.to_voting_result(a)\n"
     '                _warn_conf = max(0.25, _vr_pre.get("confidence", 0.5) - 0.15)\n'
     '                a.setdefault("_warn_confidence_override", _warn_conf)',
     '            if _gate_decision == "WARN":\n'
     "                _vr_pre = _adapter.to_voting_result(a)\n"
     '                _warn_conf = max(0.25, _vr_pre.get("confidence", 0.5) - 0.15)\n'
     '                a.setdefault("_warn_confidence_override", _warn_conf)\n'
     "                # [DEB-20260723 P0.2-弃权语义 shadow] WARN 前的语义置信度双写 (弃权=0.50, 真反向=0.35);\n"
     "                #   record-only 进 bridge, live confidence 不变, 仓位零影响; 离线按 decision_id join 测量两周.\n"
     '                a["_semantic_confidence"] = _vr_pre.get("confidence", 0.5)'),
    # H3-b: cand 增 confidence_semantic
    ("scripts/pipeline.py", '"confidence_semantic"',
     '                cand = {\n'
     '                    "symbol": vr["symbol"],\n'
     '                    "direction": _dir_long_short,\n'
     '                    "confidence": vr["confidence"],',
     '                cand = {\n'
     '                    "symbol": vr["symbol"],\n'
     '                    "direction": _dir_long_short,\n'
     '                    "confidence": vr["confidence"],\n'
     '                    "confidence_semantic": a.get("_semantic_confidence", vr["confidence"]),'),
    # H4-a: _call_llm 注入 _model_reported
    ("scripts/llm_synth.py", "_model_reported",
     '        text = data["choices"][0]["message"]["content"]\n'
     "        return json.loads(text)\n"
     "    except Exception as e:\n"
     '        return {"_error": str(e)}',
     '        text = data["choices"][0]["message"]["content"]\n'
     "        out = json.loads(text)\n"
     "        # [DEB-20260723 H4] 记录 API 信封回显模型名 → 上游模型漂移从不可观测变为可观测\n"
     "        #   (N7: DashScope 当前仅回显别名, 但别名变更/未来快照号即信号)\n"
     "        if isinstance(out, dict):\n"
     '            out.setdefault("_model_reported", data.get("model"))\n'
     "        return out\n"
     "    except Exception as e:\n"
     '        return {"_error": str(e)}'),
    # H4-b: 挑战者 prompt 增 skip_reason 必填 (保留 SKIP 权利, 勿强制表态)
    ("scripts/llm_synth.py", "skip_reason",
     '    "my_direction": "BUY/SELL_SHORT/SKIP",\n'
     '    "my_score": 0-10,',
     '    "my_direction": "BUY/SELL_SHORT/SKIP",\n'
     '    "my_score": 0-10,\n'
     '    "skip_reason": "仅当 my_direction=SKIP 时必填: 具体观望理由 (缺哪条关键数据/哪个风险不可定价); 无理由的SKIP视为无效审查",'),
    # H5.2: 挑战者三次采样 shadow (live 仍用第 1 次方向)
    ("scripts/llm_synth.py", "_direction_samples",
     "        result = _call_llm(\n"
     "            challenger_system, challenger_user,\n"
     "            temperature=0.4, max_tokens=2000,\n"
     "            model=CHALLENGER_MODEL,\n"
     "        )\n"
     '        if result and "_error" not in result:\n'
     "            return result",
     "        result = _call_llm(\n"
     "            challenger_system, challenger_user,\n"
     "            temperature=0.4, max_tokens=2000,\n"
     "            model=CHALLENGER_MODEL,\n"
     "        )\n"
     '        if result and "_error" not in result:\n'
     "            # [DEB-20260723 H5 用户批准] 采样稳定性 shadow: 同输入再采样 2 次, 方向序列落库测翻转率;\n"
     "            #   live 仍用第 1 次结果, 行为零变更. 一周后翻转率高再议 3 取众数切换 (另批).\n"
     '            _samples = [str(result.get("my_direction", "?")).upper()]\n'
     "            for _i in range(2):\n"
     "                _r2 = _call_llm(challenger_system, challenger_user,\n"
     "                                temperature=0.4, max_tokens=2000, model=CHALLENGER_MODEL)\n"
     '                if _r2 and "_error" not in _r2:\n'
     '                    _samples.append(str(_r2.get("my_direction", "?")).upper())\n'
     '            result["_direction_samples"] = _samples\n'
     "            return result"),
    # H4-c: _debate dict 增三键
    ("scripts/llm_synth.py", "challenger_model_reported",
     '        "challenged_claims": challenge.get("challenged_claims", []),\n'
     '        "missed_risks": challenge.get("missed_risks", []),\n'
     "    }",
     '        "challenged_claims": challenge.get("challenged_claims", []),\n'
     '        "missed_risks": challenge.get("missed_risks", []),\n'
     '        "challenger_model_reported": challenge.get("_model_reported"),\n'
     '        "challenger_skip_reason": challenge.get("skip_reason"),\n'
     '        "challenge_direction_samples": challenge.get("_direction_samples"),\n'
     "    }"),
]

PY_FILES = ("scripts/pipeline.py", "scripts/llm_synth.py")


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


def _cron_apply() -> None:
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    if "llm_universe_scan.py score" in cur:
        print("[skip] crontab: score 行已存在 (幂等跳过)")
        return
    new = cur.rstrip("\n") + f"\n\n{CRON_COMMENT}\n{CRON_LINE}\n"
    r = subprocess.run(["crontab", "-"], input=new, text=True, capture_output=True)
    if r.returncode != 0:
        print(f"[FAIL] crontab 写入失败: {r.stderr.strip()}")
        return
    print("[done] crontab: 已追加 UE score 行 (40 5 * * 2-6)")


def _rollback(root: Path) -> None:
    for rel in ("SKILL.md",) + PY_FILES:
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
    ap.add_argument("--no-cron", action="store_true", help="跳过 crontab 追加")
    ap.add_argument("--rollback", action="store_true", help="从备份恢复后退出")
    args = ap.parse_args()
    root = Path(args.root)

    if args.rollback:
        _rollback(root)
        return 0

    print(f"目标 root: {root}   模式: {'APPLY' if args.apply else 'DRY-RUN'}")
    ok = _apply_rules(root, apply=args.apply)
    if args.apply and not args.no_cron:
        _cron_apply()
    elif not args.apply:
        print("[plan] crontab: 待 --apply 追加 UE score 行")
    print("结果:", "全部通过" if ok else "存在失败项 (见上)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
