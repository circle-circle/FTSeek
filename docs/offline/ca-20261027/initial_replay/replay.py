from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import time
import types
from pathlib import Path

ROOT = Path('/tmp/da_ca_replay_20261027_qoder')
SOURCE = Path('/root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py')
sys.path.insert(0, str(SOURCE.parent))
os.environ['DA_CHALLENGER_BLIND_SHADOW'] = '0'
os.environ['DA_CHALLENGER_FULL_DATA'] = '1'
os.environ['DA_CHALLENGER_ANALYSIS_CAP'] = '6000'


def load_module(arm):
    source = (ROOT / ('baseline.py' if arm == 'baseline' else 'variant.py')).read_text(encoding='utf-8')
    module = types.ModuleType(f'da_ca_{arm}')
    module.__file__ = str(SOURCE)
    exec(compile(source, str(SOURCE), 'exec'), module.__dict__)
    return module


def recover_data_text(module, raw):
    prompt = raw['user_prompt']
    prefix, suffix = module._USER_TAIL_V2.split('{data}')
    start = prompt.rfind(prefix)
    if start < 0:
        raise ValueError('v2 data tail not found')
    data_start = start + len(prefix)
    data_end = len(prompt) - len(suffix) if suffix and prompt.endswith(suffix) else len(prompt)
    data_text = prompt[data_start:data_end]
    if not prompt.endswith(module._USER_TAIL_V2.format(data=data_text)):
        raise ValueError('recovered data_text does not reproduce the frozen tail')
    return data_text


def reconstruct_pass1(doc):
    merged = doc['_llm_output']
    debate = merged.get('_debate') or {}
    original_conf = merged.get('_llm_self_confidence')
    initial = {k: copy.deepcopy(v) for k, v in merged.items() if not k.startswith('_')}
    removed = []
    for key in ('debate_outcome', 'symbol', 'name', 'pangan_injected'):
        if key in initial:
            removed.append(key)
            initial.pop(key)
    if original_conf is None:
        removed.append('confidence(post-merge)')
        initial.pop('confidence', None)
    else:
        initial['confidence'] = original_conf
    original_direction = str(initial.get('direction') or '').upper()
    challenger_direction = str(debate.get('challenger_direction') or '').upper()
    if original_direction != challenger_direction and 'SKIP' not in (original_direction, challenger_direction):
        raise ValueError('true-direction merge requires an independently verified inverse')
    appended = {'[挑战者] ' + str(r) for r in (debate.get('missed_risks') or []) if r}
    risks = initial.get('risks') or []
    initial['risks'] = [r for r in risks if r not in appended]
    removed.append(f'challenger_risks:{len(risks) - len(initial["risks"])}')
    markers = 0
    for section in ('bull_case', 'bear_case'):
        for claim in initial.get(section) or []:
            for key in ('challenged', 'challenge_weakness', 'challenge_counter'):
                if key in claim:
                    markers += 1
                    claim.pop(key)
    removed.append(f'claim_markers:{markers}')
    return initial, removed


def capture(label, day, symbol):
    raw = json.loads((ROOT / 'fixtures' / f'{day}.{symbol}.raw.json').read_text())
    doc = json.loads((ROOT / 'fixtures' / f'{day}.{symbol}.json').read_text())
    debate = (doc.get('_llm_output') or {}).get('_debate') or {}
    initial, removed = reconstruct_pass1(doc)
    entry = {'label': label, 'date': day, 'symbol': symbol, 'arms': {},
             'historical_challenger_hash': debate.get('challenger_prompt_hash'),
             'historical_model': debate.get('challenger_model'),
             'historical_tier': debate.get('challenger_tier'),
             'pass1_reconstruction': 'documented_inverse; original request not persisted',
             'postprocessing_removed': removed}
    for arm in ('baseline', 'variant'):
        module = load_module(arm)
        data_text = recover_data_text(module, raw)
        seen = {}

        def stub(system, user, **kwargs):
            if not seen:
                seen.update({'system': system, 'user': user, 'kwargs': kwargs})
            return {'my_direction': 'SKIP'}

        module._call_llm = stub
        module._run_challenger(copy.deepcopy(initial), data_text)
        if not seen:
            raise ValueError('no primary prompt captured')
        kwargs = seen['kwargs']
        if kwargs.get('model') != debate.get('challenger_model') and debate.get('challenger_tier') == 'primary':
            raise ValueError(f"model mismatch: {kwargs.get('model')} vs {debate.get('challenger_model')}")
        system_hash = module._prompt_fingerprint(seen['system'], 'v3-falsify', module._SAMPLING_TAG)
        arm_dir = ROOT / 'captured' / label
        arm_dir.mkdir(parents=True, exist_ok=True)
        (arm_dir / f'{arm}.system.txt').write_text(seen['system'], encoding='utf-8')
        (arm_dir / f'{arm}.user.txt').write_text(seen['user'], encoding='utf-8')
        entry['arms'][arm] = {
            'system_fingerprint': system_hash,
            'system_sha256': hashlib.sha256(seen['system'].encode()).hexdigest(),
            'user_sha256': hashlib.sha256(seen['user'].encode()).hexdigest(),
            'kwargs': kwargs,
            'data_text_sha256': hashlib.sha256(data_text.encode()).hexdigest(),
            'data_tail_exact': True,
            'analysis_view_sha256': hashlib.sha256(
                module._challenger_analysis_view(initial).encode()).hexdigest(),
        }
    if entry['arms']['baseline']['data_text_sha256'] != entry['arms']['variant']['data_text_sha256']:
        raise ValueError('data differs across arms')
    if entry['arms']['baseline']['analysis_view_sha256'] != entry['arms']['variant']['analysis_view_sha256']:
        raise ValueError('analysis differs across arms')
    if entry['arms']['baseline']['kwargs'] != entry['arms']['variant']['kwargs']:
        raise ValueError('model parameters differ across arms')
    (ROOT / 'captured' / label / 'manifest.json').write_text(
        json.dumps(entry, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'label': label, 'historical_hash': entry['historical_challenger_hash'],
                      'baseline_hash': entry['arms']['baseline']['system_fingerprint'],
                      'variant_hash': entry['arms']['variant']['system_fingerprint'],
                      'model_kwargs': entry['arms']['baseline']['kwargs'],
                      'pass1': entry['pass1_reconstruction']}, ensure_ascii=False))


def live(label, arm):
    manifest = json.loads((ROOT / 'captured' / label / 'manifest.json').read_text())
    result_path = ROOT / 'results' / label / f'{arm}.json'
    result_path.parent.mkdir(parents=True, exist_ok=True)
    if result_path.exists():
        print(f'skip existing {result_path}')
        return
    module = load_module(arm)
    system = (ROOT / 'captured' / label / f'{arm}.system.txt').read_text(encoding='utf-8')
    user = (ROOT / 'captured' / label / f'{arm}.user.txt').read_text(encoding='utf-8')
    kwargs = manifest['arms'][arm]['kwargs']
    attempts = 0
    real_urlopen = module.urllib.request.urlopen

    def counted(*args, **kw):
        nonlocal attempts
        attempts += 1
        return real_urlopen(*args, **kw)

    started = time.monotonic()
    module.urllib.request.urlopen = counted
    try:
        result = module._call_llm(system, user, **kwargs)
    finally:
        module.urllib.request.urlopen = real_urlopen
    record = {'label': label, 'arm': arm, 'http_attempts': attempts,
              'latency_ms': round((time.monotonic() - started) * 1000),
              'model_parameters': kwargs,
              'system_fingerprint': manifest['arms'][arm]['system_fingerprint'],
              'user_sha256': manifest['arms'][arm]['user_sha256'],
              'result': result}
    result_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'label': label, 'arm': arm, 'attempts': attempts,
                      'latency_ms': record['latency_ms'],
                      'error': (result or {}).get('_error'),
                      'verdict': (result or {}).get('verdict'),
                      'direction': (result or {}).get('my_direction'),
                      'usage': (result or {}).get('_usage')}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capture', action='store_true')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--label')
    parser.add_argument('--arm', choices=('baseline', 'variant'))
    args = parser.parse_args()
    fixtures = json.loads((ROOT / 'fixture_manifest.json').read_text())
    if args.capture:
        for item in fixtures:
            capture(item['label'], item['date'], item['symbol'])
    if args.live:
        if not args.label or not args.arm:
            parser.error('--live requires --label and --arm')
        live(args.label, args.arm)


if __name__ == '__main__':
    main()
