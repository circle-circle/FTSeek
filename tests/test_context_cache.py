#!/usr/bin/env python3
"""
DeepSeek v4-pro Context Cache 验证测试
========================================
基于阿里云百炼官方文档: https://help.aliyun.com/zh/model-studio/context-cache

关键规则 (摘自官方文档):
- 显式缓存: 需要 cache_control={"type":"ephemeral"}, 最少 1024 Token, TTL 5分钟
- 隐式缓存: 自动开启, 最少 256 Token, 命中率不确定
- 两者互斥, 单请求只能应用一种模式
- 显式缓存计费: 创建=125% 输入价, 命中=10% 输入价
- 隐式缓存计费: 命中部分=20% 输入价
- 官方支持模型含 deepseek-v3.2, 不含 deepseek-v4-pro

Anthropic 兼容接口 Usage 字段:
- cache_creation_input_tokens: 创建的缓存 Token 数
- cache_read_input_tokens:    命中的缓存 Token 数
"""

import os, sys, time, textwrap
from typing import Dict, Any, Optional
from anthropic import Anthropic

BASE_URL = os.environ["ANTHROPIC_BASE_URL"]
AUTH_TOKEN = os.environ["ANTHROPIC_AUTH_TOKEN"]
MODEL = os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-pro")

client = Anthropic(base_url=BASE_URL, auth_token=AUTH_TOKEN, max_retries=1)

# ── 生成可控大小的文本 ──────────────────────────────────────────────────
# 英文每 token ≈ 4 chars, 中文每 token ≈ 1.5 chars
# 显式缓存最少 1024 tokens → 英文约 4096 chars

def text_of_tokens(target_tokens: int) -> str:
    """生成指定 token 数的英文文本"""
    word = "technology "
    # ~4 chars/token for English
    chars_needed = target_tokens * 4
    return (word * (chars_needed // len(word) + 1))[:chars_needed]

# 各实验用的文本
TEXT_512  = text_of_tokens(512)    # 512 tokens  - below explicit minimum
TEXT_1200 = text_of_tokens(1200)   # 1200 tokens - above explicit minimum (1024)
TEXT_2000 = text_of_tokens(2000)   # 2000 tokens - well above minimum

QUESTION = "Summarize the document in exactly 3 bullet points."

# ── 辅助函数 ────────────────────────────────────────────────────────────

def call_anthropic(
    system_text: Optional[str] = None,
    doc_text: Optional[str] = None,
    question: str = QUESTION,
    cache_control_on_system: bool = False,
    cache_control_on_doc: bool = False,
    max_tokens: int = 200,
) -> Dict[str, Any]:
    """
    Anthropic 兼容接口调用。
    per 官方文档, cache_control 标记放在 content 块上,
    系统从 messages 数组开头到 cache_control 位置创建缓存。
    """
    system_block: Any = system_text
    if cache_control_on_system and system_text:
        system_block = [{
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},
        }]

    user_content = []
    if doc_text:
        block = {"type": "text", "text": doc_text}
        if cache_control_on_doc:
            block["cache_control"] = {"type": "ephemeral"}
        user_content.append(block)
    user_content.append({"type": "text", "text": question})

    start = time.time()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system_block,
        messages=[{"role": "user", "content": user_content}],
    )
    latency_ms = int((time.time() - start) * 1000)

    u = resp.usage
    return {
        "latency_ms": latency_ms,
        "input_tokens": u.input_tokens,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "output_tokens": u.output_tokens,
    }


def fmt(label: str, r: Dict):
    cr = r['cache_creation_input_tokens']
    cd = r['cache_read_input_tokens']
    inp = r['input_tokens']
    eff = inp + cr + cd
    if eff == 0:
        print(f"  {label:<45} lat={r['latency_ms']:>5}ms  (no data)")
        return
    hit_pct = cd / eff * 100
    create_pct = cr / eff * 100
    print(f"  {label:<45} lat={r['latency_ms']:>5}ms  "
          f"input={inp:>5}  cache_creat={cr:>5}  cache_read={cd:>5}  "
          f"hit={hit_pct:.0f}%  create={create_pct:.0f}%  out={r['output_tokens']}")


def section(title: str):
    print(f"\n{'='*90}")
    print(f"  {title}")
    print(f"{'='*90}")


# ══════════════════════════════════════════════════════════════════════════
print(f"Model: {MODEL}")
print(f"Endpoint: {BASE_URL}")
print(f"参考文档: https://help.aliyun.com/zh/model-studio/context-cache")
print(f"官方缓存支持模型: qwen3.6-max, qwen3.6-plus, qwen3.6-flash, deepseek-v3.2, kimi-k2.6, kimi-k2.5, glm-5.1")
print(f"注意: {MODEL} 未在官方显式缓存支持列表中, 此处实测验证。")

# ══════════════════════════════════════════════════════════════════════════
# 实验 1: 隐式缓存 — 交替不同内容测自动缓存
# ══════════════════════════════════════════════════════════════════════════
section("实验 1: 隐式缓存 (自动, 最少 256 Token)")

doc_a = text_of_tokens(800)
doc_b = text_of_tokens(800)

print(f"  DocA={len(doc_a)} chars (~800 tokens), DocB={len(doc_b)} chars (~800 tokens)")
print(f"  交替发送不同文档看缓存是否跟随内容变化")

r = call_anthropic(doc_text=doc_a, question="First query about document A.")
fmt("1a: DocA (首次)", r)
time.sleep(0.5)

r = call_anthropic(doc_text=doc_b, question="Completely different doc B query.")
fmt("1b: DocB (完全不同)", r)
time.sleep(0.5)

r = call_anthropic(doc_text=doc_a, question="Back to document A, repeat query.")
fmt("1c: DocA (回到A)", r)
time.sleep(0.5)

r = call_anthropic(doc_text=doc_b, question="Back to document B, repeat query.")
fmt("1d: DocB (回到B)", r)

# ══════════════════════════════════════════════════════════════════════════
# 实验 2: 显式缓存 — 严格按文档要求 (≥1024 token, cache_control marker)
# ══════════════════════════════════════════════════════════════════════════
section("实验 2: 显式缓存 (cache_control, 最少 1024 Token)")

doc_1200 = text_of_tokens(1200)
print(f"  文本={len(doc_1200)} chars (~1200 tokens, 超过 1024 最小阈值)")
print(f"  按官方文档格式添加 cache_control 标记到 content 块")

# 第1次: 创建缓存
r = call_anthropic(doc_text=doc_1200, cache_control_on_doc=True)
fmt("2a: 带 cache_control (应 CREATE cache)", r)

time.sleep(1)

# 第2次: 命中缓存 (同一内容 + cache_control)
r = call_anthropic(doc_text=doc_1200, cache_control_on_doc=True)
fmt("2b: 相同内容+cache_control (应 READ cache)", r)

time.sleep(1)

# 第3次: 不带 cache_control 的相同内容 (验证缓存是否独立于 marker)
r = call_anthropic(doc_text=doc_1200, cache_control_on_doc=False)
fmt("2c: 相同内容, 无 cache_control (验证隐式/显式)", r)

time.sleep(1)

# 第4次: 带 cache_control, 但改变 user question
r = call_anthropic(doc_text=doc_1200, cache_control_on_doc=True,
                   question="Now provide a different analysis: list 5 key terms.")
fmt("2d: cache_control, 不同问题 (应 READ cache)", r)

# ══════════════════════════════════════════════════════════════════════════
# 实验 3: 显式缓存 — system prompt 上打标记
# ══════════════════════════════════════════════════════════════════════════
section("实验 3: 显式缓存 — System Prompt 上打 cache_control")

doc_1200b = text_of_tokens(1200)  # 新文档, 避免与实验2的缓存混淆
system_prompt = (
    "You are a professional technical analyst. You analyze documents carefully "
    "and provide structured responses. " * 20
)
print(f"  System prompt: {len(system_prompt)} chars")
print(f"  文档: {len(doc_1200b)} chars (~1200 tokens)")

# 首次: system + doc 都带 cache_control
r = call_anthropic(system_text=system_prompt, doc_text=doc_1200b,
                   cache_control_on_system=True, cache_control_on_doc=True)
fmt("3a: sys+doc 均 cache_control (首次 CREATE)", r)

time.sleep(1)

# 二次: 同样 system + doc, 都带 cache_control
r = call_anthropic(system_text=system_prompt, doc_text=doc_1200b,
                   cache_control_on_system=True, cache_control_on_doc=True)
fmt("3b: sys+doc 均 cache_control (二次 READ)", r)

time.sleep(1)

# 三次: 只 doc 带 cache_control, system 不带
r = call_anthropic(system_text=system_prompt, doc_text=doc_1200b,
                   cache_control_on_system=False, cache_control_on_doc=True)
fmt("3c: 仅 doc cache_control (验证 system 缓存)", r)

# ══════════════════════════════════════════════════════════════════════════
# 实验 4: 缓存 TTL 测试 (官方文档说 5 分钟, 命中后重置)
# ══════════════════════════════════════════════════════════════════════════
section("实验 4: 缓存 TTL 测试 (等待 60s)")

doc_c = text_of_tokens(1200)

# 建立缓存
r = call_anthropic(doc_text=doc_c, cache_control_on_doc=True)
fmt("4a: 创建显式缓存", r)

# 立即命中验证
time.sleep(1)
r = call_anthropic(doc_text=doc_c, cache_control_on_doc=True)
fmt("4b: 1s后命中", r)

# 等60秒再测
print("  等待 60 秒...")
time.sleep(60)

r = call_anthropic(doc_text=doc_c, cache_control_on_doc=True)
fmt("4c: 60s后命中 (应仍有效, TTL=5min)", r)

# ══════════════════════════════════════════════════════════════════════════
# 实验 5: 隐式缓存 — 阈值测试 (256 vs 200 tokens)
# ══════════════════════════════════════════════════════════════════════════
section("实验 5: 隐式缓存阈值测试 (256 vs 200 tokens)")

doc_300 = text_of_tokens(300)   # > 256 threshold
doc_200 = text_of_tokens(200)   # < 256 threshold

r = call_anthropic(doc_text=doc_300); time.sleep(0.5)
fmt("5a: 300 tokens (首次)", r)
r = call_anthropic(doc_text=doc_300); time.sleep(0.5)
fmt("5b: 300 tokens (重复)", r)
r = call_anthropic(doc_text=doc_200); time.sleep(0.5)
fmt("5c: 200 tokens (首次)", r)
r = call_anthropic(doc_text=doc_200); time.sleep(0.5)
fmt("5d: 200 tokens (重复, 低于256阈值)", r)

# ══════════════════════════════════════════════════════════════════════════
# 汇总
# ══════════════════════════════════════════════════════════════════════════
section("汇总与结论")

print(f"""
  官方文档支持的显式缓存模型: qwen3.6-max/plus/flash, qwen3-coder-plus/flash,
                          qwen3-vl-plus/flash, deepseek-v3.2, kimi-k2.6/2.5, glm-5.1
  当前测试模型: {MODEL} {"(不在官方列表中)" if MODEL not in ["deepseek-v3.2"] else "(在官方列表中)"}

  显式缓存规则:
    - 最少 1024 Token
    - TTL 5 分钟 (命中后重置)
    - cache_control: {{"type": "ephemeral"}} 标记在 content 块上
    - 首次创建后, 请求返回 cache_creation_input_tokens > 0
    - 命中后, 请求返回 cache_read_input_tokens > 0

  隐式缓存规则:
    - 最少 256 Token, 自动开启, 无法关闭
    - 命中率不确定
    - 与显式缓存互斥: 带 cache_control 标记的请求只走显式缓存
""")