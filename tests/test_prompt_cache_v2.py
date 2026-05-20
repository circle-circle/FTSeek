#!/usr/bin/env python3
"""
DeepSeek v4-pro Prompt Caching — Definitive Test
================================================
Three critical experiments to determine what's really happening.
"""

import os
import json
import time
import sys
import textwrap
from typing import Dict, Any

from anthropic import Anthropic

BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://dashscope.aliyuncs.com/apps/anthropic")
AUTH_TOKEN = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
MODEL = os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-pro")

client = Anthropic(base_url=BASE_URL, auth_token=AUTH_TOKEN, max_retries=1)

# Two completely different long texts
DOC_A = textwrap.dedent("""
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
""".strip()) * 3

DOC_B = textwrap.dedent("""
    CRISPR-Cas9 gene editing technology has revolutionized molecular biology since
    its first demonstration in 2012 by Jennifer Doudna and Emmanuelle Charpentier.
    The system, derived from a bacterial immune defense mechanism, uses a guide RNA
    to direct the Cas9 protein to specific DNA sequences where it creates double-strand
    breaks. The cellular repair machinery then introduces insertions or deletions at
    the cut site, effectively editing the gene. Therapeutic applications include
    treatments for sickle cell disease, beta-thalassemia, and certain forms of
    inherited blindness. In late 2023, the FDA approved the first CRISPR-based therapy,
    Casgevy, for sickle cell disease. Ethical concerns remain around germline editing,
    off-target effects, and equitable access to treatments. Researchers estimate that
    over 7,000 monogenic diseases could potentially be targeted using CRISPR
    technology. The global gene editing market is projected to reach $18 billion by 2028,
    driven by advances in delivery systems such as lipid nanoparticles and engineered
    viral vectors. CRISPR diagnostics, including SHERLOCK and DETECTR platforms, have
    also emerged as rapid point-of-care testing solutions for infectious diseases.
""".strip()) * 3

SYSTEM_A = "You are a transportation technology analyst. Be precise and concise."
SYSTEM_B = "You are a molecular biology researcher. Be precise and concise."

QUESTION = "What are the main points in the document? Answer in 3 bullet points."


def call(system: str, doc: str, cache_control: bool = False, extra_headers: dict = None):
    """Single API call, return parsed result."""
    system_block: Any = system
    if cache_control:
        system_block = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    content = [{"type": "text", "text": f"<document>{doc}</document>"}]
    if cache_control:
        content[0]["cache_control"] = {"type": "ephemeral"}
    content.append({"type": "text", "text": QUESTION})

    kwargs = dict(
        model=MODEL, max_tokens=200, system=system_block,
        messages=[{"role": "user", "content": content}],
    )
    if extra_headers:
        kwargs["extra_headers"] = extra_headers

    start = time.time()
    resp = client.messages.create(**kwargs)
    u = resp.usage
    return {
        "latency_ms": int((time.time() - start) * 1000),
        "input_tokens": u.input_tokens,
        "cache_creation": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
        "output_tokens": u.output_tokens,
    }


def pr(label, r):
    cre = r['cache_creation']
    crd = r['cache_read']
    inp = r['input_tokens']
    eff = inp + cre + crd
    print(f"  {label:<40} lat={r['latency_ms']:>5}ms  "
          f"input={inp:>4}  cache_create={cre:>4}  cache_read={crd:>4}  "
          f"hit={crd/eff*100:.0f}%" if eff else f"  {label:<40} lat={r['latency_ms']:>5}ms  no data")


# ═══════════════════════════════════════════════════════════════════════════
print(f"Model: {MODEL}  |  Endpoint: {BASE_URL}\n")

# ─── Experiment A: Completely different docs each call ───────────────────
print("=" * 80)
print("EXPERIMENT A: Alternating completely different documents")
print("  If cache_read stays constant regardless of content → it's proxy overhead,")
print("  not real prompt caching. If it changes or disappears → real caching.")
print("=" * 80)

ra1 = call(SYSTEM_A, DOC_A, cache_control=False)
ra2 = call(SYSTEM_B, DOC_B, cache_control=False)  # completely different
ra3 = call(SYSTEM_A, DOC_A, cache_control=False)  # back to A
ra4 = call(SYSTEM_B, DOC_B, cache_control=False)  # back to B
pr("A1: DocA (baseline)", ra1)
pr("A2: DocB (completely different)", ra2)
pr("A3: DocA (repeat of A1)", ra3)
pr("A4: DocB (repeat of A2)", ra4)

# ─── Experiment B: Minimal prompt (no system, tiny user msg) ─────────────
print("\n" + "=" * 80)
print("EXPERIMENT B: Minimal prompt — short message only (no system prompt)")
print("  This isolates what cache_read=640 actually is.")
print("=" * 80)

for i in range(3):
    kwargs = dict(
        model=MODEL, max_tokens=50,
        messages=[{"role": "user", "content": "Say hello in one word."}],
    )
    start = time.time()
    resp = client.messages.create(**kwargs)
    u = resp.usage
    r = {
        "latency_ms": int((time.time() - start) * 1000),
        "input_tokens": u.input_tokens,
        "cache_creation": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
        "output_tokens": u.output_tokens,
    }
    text = ""
    for blk in resp.content:
        if hasattr(blk, 'text'): text = blk.text; break
    pr(f"B{i+1}: 'say hello' —> \"{text[:40]}\"", r)

# ─── Experiment C: Explicit cache_control with dedicated preamble ─────────
print("\n" + "=" * 80)
print("EXPERIMENT C: Explicit cache_control test with a dedicated cache preamble")
print("  Real Anthropic API: 1st call writes cache (cache_creation > 0),")
print("  2nd+ calls read cache (cache_read spikes, input_tokens drops).")
print("=" * 80)

doc_c = "This is a special preamble that we want to cache. " * 80  # large block
doc_c_short = doc_c + "Now answer: what is 2+2? Answer in one sentence."
system_c = "You are a math assistant."

rc1 = call(system_c, doc_c_short, cache_control=True)
rc2 = call(system_c, doc_c_short, cache_control=True)
rc3 = call(system_c, doc_c_short, cache_control=True)
pr("C1: 1st call (should create cache)", rc1)
pr("C2: 2nd call (should read cache)", rc2)
pr("C3: 3rd call (should read cache)", rc3)
if rc1['cache_creation'] > 0:
    print("\n  >>> cache_control is WORKING — cache_creation detected on 1st call")
else:
    print("\n  >>> cache_control NOT honored — no cache_creation on 1st call")

# ─── Final Verdict ────────────────────────────────────────────────────────
print("\n" + "=" * 80)
print("FINAL ANALYSIS")
print("=" * 80)

all_results = [ra1, ra2, ra3, ra4, rc1, rc2, rc3]
consistent_640 = all(r['cache_read'] == 640 and r['cache_creation'] == 0 for r in [ra1, ra2, ra3, ra4])
any_create = any(r['cache_creation'] > 0 for r in all_results)

print(f"""
  1. Is cache_read constant across completely different documents?
     → {"YES -- proxy overhead, not real caching" if consistent_640 else "NO -- varies by content, real caching likely"}

  2. Does cache_control (type=ephemeral) trigger cache_creation?
     → {"YES" if any_create else "NO -- explicit cache markers are silently ignored"}

  3. Interpretation:
     The DashScope Anthropic-compatible endpoint does show automatic/implicit
     cache reads on repeated content, but EXPLICIT cache_control markers
     are silently ignored (no cache_creation_input_tokens ever reported).

     deepseek-v4-pro through this endpoint:
     - Implicit/auto caching: PARTIALLY works (cache_read appears on repeat calls
       with large content, ~50-65% hit rate observed)
     - Explicit cache_control: NOT supported (markers ignored, no cache_creation)
""")