#!/usr/bin/env python3
"""
DeepSeek v4-pro Prompt Caching Test (Anthropic-compatible API)
================================================================
Tests both implicit and explicit prompt caching via the DashScope Anthropic endpoint.

Implicit caching: Same prompt sent again within a short window — API may serve
                  cache_read_input_tokens instead of re-processing all input.
Explicit caching: Mark specific content blocks with cache_control={"type": "ephemeral"}
                  to guarantee caching of that block across requests.

Reference: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
"""

import os
import json
import time
import sys
import textwrap
from typing import Dict, Any, List

from anthropic import Anthropic

# ─── Config ───────────────────────────────────────────────────────────────
BASE_URL = os.environ.get(
    "ANTHROPIC_BASE_URL",
    "https://dashscope.aliyuncs.com/apps/anthropic",
)
AUTH_TOKEN = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
MODEL = os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-pro")

if not AUTH_TOKEN:
    print("ERROR: ANTHROPIC_AUTH_TOKEN not set")
    sys.exit(1)

client = Anthropic(
    base_url=BASE_URL,
    auth_token=AUTH_TOKEN,
    max_retries=1,
)

# ─── Helper ──────────────────────────────────────────────────────────────

LONG_TEXT = textwrap.dedent("""
    HyperLoop Transportation Technologies (HTT) is a research company formed in 2013
    through a global crowdfunding initiative. Headquartered in Toulouse, France, HTT
    announced plans to build a 5-kilometer test track in Quay Valley, California in
    2016, with projected completion by 2018. The Hyperloop concept, originally proposed
    by Elon Musk in a 2013 white paper, envisions passenger pods traveling through
    low-pressure tubes at speeds exceeding 700 mph using magnetic levitation and
    electric propulsion. The system aims to be more energy-efficient than traditional
    high-speed rail while providing faster travel times than commercial aviation for
    routes under 900 miles. Key technical challenges include maintaining vacuum
    pressure across long distances, thermal expansion management, pod stability at
    high speeds, and emergency evacuation procedures. In 2017, HTT signed agreements
    with several international governments to conduct feasibility studies for potential
    Hyperloop routes connecting major cities across Europe, Asia, and the Middle East.
    The company has raised over $100 million in funding from venture capital firms and
    strategic investors. Competitors in the space include Virgin Hyperloop (formerly
    Hyperloop One), which built a 500-meter test track in Nevada and successfully
    tested passenger pods at speeds of 240 mph in 2020.
""".strip()) * 3  # ~2.5KB of text, enough to be noticeable in token counts

SYSTEM_PROMPT = textwrap.dedent("""
    You are a transportation technology analyst. Answer questions about Hyperloop
    technology precisely and concisely. Cite specific facts from the provided
    document when relevant. Keep responses under 100 words.
""".strip())

QUESTION = "What are the top 3 technical challenges mentioned for Hyperloop systems?"


def call_api(cache_control: bool = False, system_cache: bool = False) -> Dict[str, Any]:
    """
    Make one API call and return full response data.

    With explicit cache_control, we add the cache_control marker to the content block.
    With system_cache, we mark the system prompt for caching too.
    """
    system_block: Any
    if system_cache and cache_control:
        system_block = [
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    else:
        system_block = SYSTEM_PROMPT

    content_blocks: List[Dict] = [
        {
            "type": "text",
            "text": f"<document>{LONG_TEXT}</document>",
        }
    ]

    if cache_control:
        content_blocks[0]["cache_control"] = {"type": "ephemeral"}

    content_blocks.append({
        "type": "text",
        "text": QUESTION,
    })

    start = time.time()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=300,
        system=system_block,
        messages=[{"role": "user", "content": content_blocks}],
    )
    latency_ms = int((time.time() - start) * 1000)

    # Extract text from first TextBlock (skip ThinkingBlocks)
    text_content = ""
    for block in resp.content:
        if hasattr(block, 'text'):
            text_content = block.text[:120]
            break

    return {
        "latency_ms": latency_ms,
        "id": resp.id,
        "stop_reason": resp.stop_reason,
        "model": resp.model,
        "usage": {
            "input_tokens": resp.usage.input_tokens,
            "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
            "output_tokens": resp.usage.output_tokens,
        },
        "text_preview": text_content if text_content else "(empty/thinking-only)",
    }


def print_header(title: str):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def run_test(label: str, cache_control: bool = False, system_cache: bool = False,
             repeat: int = 1, sleep_between: float = 0.5):
    """Run a test N times and print results."""
    results = []
    for i in range(repeat):
        try:
            r = call_api(cache_control=cache_control, system_cache=system_cache)
            results.append(r)
            status = "OK"
        except Exception as e:
            status = f"FAIL: {e}"
            print(f"  [{i+1}/{repeat}] {status}")
            return results

        u = r['usage']
        cache_read = u.get('cache_read_input_tokens', 0)
        cache_create = u.get('cache_creation_input_tokens', 0)
        raw_input = u.get('input_tokens', 0)

        # Cache hit rate: what fraction of input tokens come from cache
        effective_input = raw_input + cache_read + cache_create
        hit_rate = (cache_read / effective_input * 100) if effective_input > 0 else 0.0
        create_rate = (cache_create / effective_input * 100) if effective_input > 0 else 0.0

        print(f"  [{i+1}/{repeat}] latency={r['latency_ms']:>4}ms  "
              f"input={raw_input:>5}  "
              f"cache_read={cache_read:>5}  "
              f"cache_create={cache_create:>5}  "
              f"hit={hit_rate:.0f}%  create={create_rate:.0f}%  "
              f"output={u.get('output_tokens', 0)}")
        if sleep_between > 0 and i < repeat - 1:
            time.sleep(sleep_between)
    return results


def summarize(prefix: str, results: list):
    if not results:
        print(f"  {prefix}: no data")
        return
    latencies = [r['latency_ms'] for r in results]
    avg_lat = sum(latencies) / len(latencies)
    cache_reads = [r['usage']['cache_read_input_tokens'] for r in results]
    cache_creates = [r['usage']['cache_creation_input_tokens'] for r in results]
    print(f"  {prefix}: avg_latency={avg_lat:.0f}ms  "
          f"cache_read_total={sum(cache_reads)}  "
          f"cache_create_total={sum(cache_creates)}  "
          f"(n={len(results)})")


# ══════════════════════════════════════════════════════════════════════════
#  TEST SUITE
# ══════════════════════════════════════════════════════════════════════════

print(f"Model: {MODEL}")
print(f"Endpoint: {BASE_URL}")
print(f"Document size: {len(LONG_TEXT)} chars (~{len(LONG_TEXT)//4} tokens)")
print(f"System prompt size: {len(SYSTEM_PROMPT)} chars (~{len(SYSTEM_PROMPT)//4} tokens)")

# ─── Test 1: Baseline (no caching) ─────────────────────────────────────────
print_header("TEST 1: Baseline — Single call (no cache markers)")
r1 = run_test("baseline", cache_control=False, repeat=1)

# ─── Test 2: Implicit caching ───────────────────────────────────────────────
print_header("TEST 2: Implicit Caching — Repeat identical calls (no cache_control)")
print("  Same document + same system prompt, repeated 4x to test implicit cache reuse")
r2 = run_test("implicit", cache_control=False, repeat=4, sleep_between=1.0)

# ─── Test 3: Explicit caching (document only) ────────────────────────────────
print_header("TEST 3: Explicit Caching — cache_control on document block")
print("  First call: writes to cache. Subsequent calls: should read from cache.")
r3 = run_test("explicit-doc", cache_control=True, system_cache=False, repeat=4, sleep_between=1.0)

# ─── Test 4: Explicit caching (system + document) ────────────────────────────
print_header("TEST 4: Explicit Caching — cache_control on system + document")
print("  First call: writes both to cache. Subsequent calls: read from cache.")
r4 = run_test("explicit-both", cache_control=True, system_cache=True, repeat=4, sleep_between=1.0)

# ─── Test 5: Same as Test 1 but after a gap (cache TTL check) ──────────────
print_header("TEST 5: Explicit cache TTL check — wait 3s then re-call with cache_control")
print("  After previous explicit cache writes, this should still read from cache.")
time.sleep(3)
r5 = run_test("explicit-ttl", cache_control=True, system_cache=True, repeat=1)

# ─── Summary ────────────────────────────────────────────────────────────────
print_header("SUMMARY")
summarize("T1: Baseline           ", r1)
summarize("T2: Implicit (×4)      ", r2)
summarize("T3: Explicit-doc (×4)  ", r3)
summarize("T4: Explicit-both (×4) ", r4)
summarize("T5: Explicit-TTL       ", r5)

# Cache capability detection
print(f"\n{'='*70}")
print(f"  CACHE SUPPORT ANALYSIS")
print(f"{'='*70}")

implicit_hits = any(r['usage']['cache_read_input_tokens'] > 0 for r in r2)
explicit_creates = any(r['usage']['cache_creation_input_tokens'] > 0 for r in r3)
explicit_reads = any(r['usage']['cache_read_input_tokens'] > 0 for r in r3)

print(f"  Implicit caching (repeated identical calls):")
print(f"    Cache reads detected: {'YES ✓' if implicit_hits else 'NO ✗'}")

print(f"  Explicit caching (cache_control ephemeral):")
print(f"    Cache creation detected: {'YES ✓' if explicit_creates else 'NO ✗'}")
print(f"    Cache reads detected:    {'YES ✓' if explicit_reads else 'NO ✗'}")

if not explicit_creates and not explicit_reads and not implicit_hits:
    print(f"\n  VERDICT: deepseek-v4-pro does NOT support Anthropic-style prompt caching")
    print(f"           through the DashScope Anthropic-compatible endpoint.")
elif explicit_creates and explicit_reads:
    print(f"\n  VERDICT: Full explicit caching support detected!")
elif implicit_hits and not explicit_creates:
    print(f"\n  VERDICT: Implicit caching works, but explicit cache_control may not be supported.")
else:
    print(f"\n  VERDICT: Partial or unusual caching behavior — see raw data above.")

print()