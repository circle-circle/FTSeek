#!/usr/bin/env python3
"""DA user prompt 缓存重排 + 挑战者用量落盘 (2026-09-09, 用户批准消耗优化批 F1+F3).

对 /root/.hermes/skills/deep-stock-analysis/scripts/{llm_synth,config}.py 打补丁:

F3 (缓存命中率): USER_PROMPT_TEMPLATE 拆为 _USER_HEAD (变动数据段) + _USER_CONTRACT
  (稳定契约段, ~6 成 token, 跨候选逐字节相同)。新增 USER_PROMPT_STABLE_V2 = 契约前置
  序, _call_llm 增 user_tail 参数 (user 消息内第二个 cache_control 断点)。
  9/8 实测: 8 候选仅 1677 tok system 前缀命中缓存; 重排后 ~5.5-6k tok 契约前缀可命中,
  估算省 $0.10-0.15/日。模型可见段落集合不变, 仅顺序变化; 唯一文本差异:
  契约内 照抄"上方"→照抄"数据中" (位置中性化, 数据质量审计段在 {data} 内,
  v1/v2 两序都正确)。开关 config.DA_USER_CACHE_REORDER (默认1, =0 回滚 v1 序);
  PROMPT_HASH 按 flag 生效模板计 (重排前后基线可切分)。
  蒸馏影子 A6 约束不受影响: SYSTEM_PROMPT 零字节改动; 两 shadow 臂共用同一 user
  模板构造函数 build_user_prompt, 臂内当日比较保持单变量。

F1 (成本失明修复): _merge_debate_results 的 _debate 增加 challenger_usage /
  challenger_finish_reason — 此前挑战者 (~$0.23/日) token 不落 checkpoint,
  日报只能计次数 (9/8 日报原文 "成本未含")。

幂等: 已打直接退出 0。前置备份: llm_synth.py.bak-cache-reorder-20260909 /
  config.py.bak-cache-reorder-20260909 (本脚本不自动建, 打前已手工 cp)。
用法: ./venv/bin/python scripts/patches/apply_da_user_cache_reorder_20260909.py
"""
import pathlib
import sys

SKILL_DIR = pathlib.Path("/root/.hermes/skills/deep-stock-analysis/scripts")
SYNTH = SKILL_DIR / "llm_synth.py"
CONFIG = SKILL_DIR / "config.py"


# ─── llm_synth.py 补丁对 (name, old, new) ───
SYNTH_PAIRS = []

# ① _call_llm 签名加 user_tail
SYNTH_PAIRS.append(("call_llm_signature", '''def _call_llm(system: str, user: str, temperature: float = 0.3, max_tokens: int = 3000, model: str = None,
              cache_system: bool = False, extra_body: Optional[dict] = None,
              timeout: int = 60) -> Optional[dict]:''', '''def _call_llm(system: str, user: str, temperature: float = 0.3, max_tokens: int = 3000, model: str = None,
              cache_system: bool = False, extra_body: Optional[dict] = None,
              timeout: int = 60, user_tail: str = None) -> Optional[dict]:'''))

# ② _call_llm docstring 补 user_tail 段
SYNTH_PAIRS.append(("call_llm_docstring", '''    [2026-09-01 DEB-20260901b] extra_body: 模型特定参数透传 (合并进 payload,
    可覆盖默认 — 如 {"enable_thinking": True, "thinking_budget": 2048});
    None (默认) = 与历史行为逐字节一致。
    """''', '''    [2026-09-01 DEB-20260901b] extra_body: 模型特定参数透传 (合并进 payload,
    可覆盖默认 — 如 {"enable_thinking": True, "thinking_budget": 2048});
    None (默认) = 与历史行为逐字节一致。

    [2026-09-09 F3] user_tail: user 消息稳定前缀 (契约段渲染文本), 与 cache_system
    联动 — 缓存格式下 user 内容拆为 [cache_control(user_tail), user] 两块,
    在 user 消息内建第二个缓存断点, 使跨候选逐字节相同的契约段 (~6 成 user token)
    命中前缀缓存; 模型按块序读到的文本 == user_tail + user, 与单块逐字节一致。
    降级路径 (use_cache=False) 自动退化为拼接纯文本。不传 = 原行为。
    """'''))

# ③ _call_llm 消息体构造
SYNTH_PAIRS.append(("call_llm_payload", '''        payload = {
            "model": model or MODEL,
            "messages": [
                _sys_msg,
                {"role": "user", "content": user},
            ],''', '''        if use_cache and user_tail is not None:
            _user_content = [
                {"type": "text", "text": user_tail, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": user},
            ]
        elif user_tail is not None:
            _user_content = user_tail + user  # 降级/未开缓存: 单块全文, 语义等价
        else:
            _user_content = user
        payload = {
            "model": model or MODEL,
            "messages": [
                _sys_msg,
                {"role": "user", "content": _user_content},
            ],'''))

# ④ 模板头: 拆 _USER_HEAD + _USER_CONTRACT
SYNTH_PAIRS.append(("template_split_head", '''USER_PROMPT_TEMPLATE = """请基于以下真实数据，对该标的进行深度多空辩论分析。

{data}

---

请输出严格的JSON格式（不要markdown包裹），结构如下：''', '''# [2026-09-09 F3 缓存重排] USER 模板拆为变动头 (_USER_HEAD, 含 {data}) 与稳定契约段
#   (_USER_CONTRACT, 跨候选逐字节相同)。v1 = 两段拼接 (回滚路径); v2 = 契约前置序 (默认,
#   见文件尾部 USER_PROMPT_STABLE_V2 与 build_user_prompt)。
_USER_HEAD = """请基于以下真实数据，对该标的进行深度多空辩论分析。

{data}

---

"""
_USER_CONTRACT = """请输出严格的JSON格式（不要markdown包裹），结构如下：'''))

# ⑤ 契约内 "上方" → "数据中" (位置中性化: 重排后契约段在数据之前, "上方"指向错误;
#    数据质量审计段本就位于 {data} 内, 两序下 "数据中" 都正确)
SYNTH_PAIRS.append(("position_neutral_fix", '''照抄上方"数据质量审计"段的数字''', '''照抄数据中"数据质量审计"段的数字'''))

# ⑥ 模板尾: 拼回 v1 + 定义 v2 + 构造函数
SYNTH_PAIRS.append(("template_split_tail", '''- 如果本股无历史交易记录，照常分析，缺数据不是 SKIP 理由。
"""''', '''- 如果本股无历史交易记录，照常分析，缺数据不是 SKIP 理由。
"""


# v1 原版序 (数据在前): 两段的逐字节拼接, 回滚路径 (DA_USER_CACHE_REORDER=0) 用它。
USER_PROMPT_TEMPLATE = _USER_HEAD + _USER_CONTRACT
# v2 缓存友好序 (默认): 契约段 (~6 成 token, 跨候选不变) 前置为可缓存前缀, 数据段最后。
#   与 v1 段落集合相同, 仅顺序不同; 文本差异仅上方 照抄"上方"→"数据中" 中性化一处。
_USER_TAIL_V2 = "\\n---\\n\\n请基于以下真实数据，对该标的进行深度多空辩论分析。\\n\\n{data}\\n"
USER_PROMPT_STABLE_V2 = _USER_CONTRACT + _USER_TAIL_V2
# 契约段渲染文本 ({{ }}→{ } 转义还原; 契约段无 {data} 占位, replace 即等价 format 结果),
# 供 _call_llm user_tail 直接作缓存断点内容, 免每调用重复 replace。
_USER_CONTRACT_RENDERED = _USER_CONTRACT.replace("{{", "{").replace("}}", "}")


def build_user_prompt(data_text: str):
    """当前 flag 下的 DA user prompt: 返回 (user_text, user_tail)。

    v2 (config.DA_USER_CACHE_REORDER=1, 默认): user_text=数据尾段, user_tail=契约渲染
      文本 (调用侧以 cache_control 块前置; 拼接 == USER_PROMPT_STABLE_V2.format(data=...))。
    v1 (=0 回滚): (USER_PROMPT_TEMPLATE 渲染全文, None) — 单块纯文本, 原行为。
    """
    if getattr(config, "DA_USER_CACHE_REORDER", False):
        return _USER_TAIL_V2.format(data=data_text), _USER_CONTRACT_RENDERED
    return USER_PROMPT_TEMPLATE.format(data=data_text), None


def effective_user_template() -> str:
    """指纹/审计口径: 当前 flag 下模型实际所见的 user 模板 (PROMPT_HASH 随 flag 切分)。"""
    if getattr(config, "DA_USER_CACHE_REORDER", False):
        return USER_PROMPT_STABLE_V2
    return USER_PROMPT_TEMPLATE'''))


# ⑦ PROMPT_HASH 按生效模板
SYNTH_PAIRS.append(("prompt_hash_effective", '''PROMPT_HASH = _prompt_fingerprint(SYSTEM_PROMPT, USER_PROMPT_TEMPLATE)''', '''# [2026-09-09 F3] 指纹按 flag 生效后的有效模板计 — 重排前后基线可切分 (口径七律#2)。
PROMPT_HASH = _prompt_fingerprint(SYSTEM_PROMPT, effective_user_template())'''))

# ⑧ synthesize: user_prompt 构造
SYNTH_PAIRS.append(("synthesize_build", '''    user_prompt = USER_PROMPT_TEMPLATE.format(data=data_text)''', '''    # [2026-09-09 F3] v2: (数据尾段, 契约缓存块); v1 回滚: (完整模板, None)。
    #   落盘 _user_prompt 须为模型所见全文 → 下方按是否有 tail 拼接还原。
    user_prompt, _user_tail = build_user_prompt(data_text)'''))

# ⑨ synthesize: 分析师调用挂 user_tail
SYNTH_PAIRS.append(("synthesize_call", '''    result = _call_llm(system_override or SYSTEM_PROMPT, user_prompt, temperature=0.3,
                       max_tokens=6000 if _thinking_on else 3000,
                       cache_system=config.DA_PROMPT_CACHE, extra_body=_extra_body,
                       timeout=240 if _thinking_on else 60)''', '''    result = _call_llm(system_override or SYSTEM_PROMPT, user_prompt, temperature=0.3,
                       max_tokens=6000 if _thinking_on else 3000,
                       cache_system=config.DA_PROMPT_CACHE, extra_body=_extra_body,
                       timeout=240 if _thinking_on else 60, user_tail=_user_tail)'''))

# ⑩ synthesize: system_override 指纹按生效模板
SYNTH_PAIRS.append(("override_fingerprint", '''    result["_prompt_hash"] = (
        _prompt_fingerprint(system_override, USER_PROMPT_TEMPLATE)
        if system_override else PROMPT_HASH
    )''', '''    result["_prompt_hash"] = (
        _prompt_fingerprint(system_override, effective_user_template())
        if system_override else PROMPT_HASH
    )'''))

# ⑪ synthesize: _user_prompt 落盘为模型所见全文
SYNTH_PAIRS.append(("user_prompt_snapshot", '''    result["_user_prompt"] = user_prompt''', '''    result["_user_prompt"] = ((_user_tail + user_prompt) if _user_tail else user_prompt)'''))

# ⑫ F1: _debate 落挑战者 usage
SYNTH_PAIRS.append(("challenger_usage", '''        # [第13轮 P6] 挑战者指令指纹 (v1/v2 与其内容变更均可切分)
        "challenger_prompt_hash": challenge.get("_challenger_prompt_hash"),
    }''', '''        # [第13轮 P6] 挑战者指令指纹 (v1/v2 与其内容变更均可切分)
        "challenger_prompt_hash": challenge.get("_challenger_prompt_hash"),
        # [2026-09-09 F1] 挑战者 token 用量落 checkpoint — 此前日报只计次数,
        #   挑战者成本 (~$0.23/日) 完全失明; 与分析师 _usage 同构 (DashScope usage dict)。
        "challenger_usage": challenge.get("_usage"),
        "challenger_finish_reason": challenge.get("_finish_reason"),
    }'''))


# ─── config.py 补丁对 ───
CONFIG_PAIRS = [("reorder_flag", '''DA_PROMPT_CACHE = (_read_env("DA_PROMPT_CACHE") or "1") != "0"
# 2026-09-01 (DEB-20260901b 用户裁"直接切生产, 修尺子提升质量"): DA 分析师思考模式。''', '''DA_PROMPT_CACHE = (_read_env("DA_PROMPT_CACHE") or "1") != "0"
# 2026-09-09 (消耗优化 F3, 用户批准): DA user prompt 契约段前移 — 稳定契约 (~6 成
#   user token, 跨候选逐字节相同) 成为共享前缀并挂第二个 cache_control 断点
#   (llm_synth._call_llm user_tail)。模型可见段落集合不变, 仅顺序变化; 文本差异仅
#   契约内 照抄"上方"→"数据中" 一处位置中性化。依赖 DA_PROMPT_CACHE=1 才有缓存收益;
#   PROMPT_HASH 按生效模板计 (重排前后基线可切分)。9/8 实测缓存命中仅 1677 tok
#   (system 前缀), 重排后预计 ~5.5-6k tok 契约前缀可命中, 估省 $0.10-0.15/日。
#   回滚: .env.us 加 DA_USER_CACHE_REORDER=0 → v1 数据在前原序。
DA_USER_CACHE_REORDER = (_read_env("DA_USER_CACHE_REORDER") or "1") != "0"
# 2026-09-01 (DEB-20260901b 用户裁"直接切生产, 修尺子提升质量"): DA 分析师思考模式。''')]


def apply_pairs(path: pathlib.Path, pairs) -> None:
    text = path.read_text(encoding="utf-8")
    changed = False
    for name, old, new in pairs:
        if new in text:
            print(f"  [skip] {path.name}:{name} 已打")
            continue
        n = text.count(old)
        if n != 1:
            print(f"  [FAIL] {path.name}:{name} 锚点出现 {n} 次 (期望1) — 未改动, 请检查")
            sys.exit(1)
        text = text.replace(old, new)
        changed = True
        print(f"  [ok] {path.name}:{name}")
    if changed:
        path.write_text(text, encoding="utf-8")


def main():
    apply_pairs(SYNTH, SYNTH_PAIRS)
    apply_pairs(CONFIG, CONFIG_PAIRS)
    # 编译自检 + v1 拼接等价自检 (v1 与原版差异应仅 "上方"→"数据中" 一处)
    import subprocess
    for p in (SYNTH, CONFIG):
        subprocess.run([sys.executable, "-m", "py_compile", str(p)], check=True)
    sys.path.insert(0, str(SKILL_DIR))
    import importlib
    import config as _c  # noqa
    import llm_synth as _s
    importlib.reload(_s)
    assert _s.USER_PROMPT_TEMPLATE == _s._USER_HEAD + _s._USER_CONTRACT
    assert _s.USER_PROMPT_STABLE_V2 == _s._USER_CONTRACT + _s._USER_TAIL_V2
    _u, _t = _s.build_user_prompt("DATA_HERE")
    assert (_t + _u) == _s.USER_PROMPT_STABLE_V2.format(data="DATA_HERE")
    assert "DATA_HERE" in _u and "{data}" not in _u
    print("[ok] 编译 + v1/v2 拼装等价自检通过; 生效模板 =",
          "v2(契约前置)" if _s.effective_user_template() is _s.USER_PROMPT_STABLE_V2 else "v1")
    print("[ok] PROMPT_HASH =", _s.PROMPT_HASH)


if __name__ == "__main__":
    main()
