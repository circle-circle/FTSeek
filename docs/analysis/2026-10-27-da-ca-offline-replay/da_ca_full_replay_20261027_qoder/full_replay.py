from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import re
import time
from pathlib import Path

ROOT = Path('/tmp/da_ca_full_replay_20261027_qoder')
HELPER_PATH = Path('/tmp/da_ca_replay_20261027_qoder/replay.py')
SPEC = importlib.util.spec_from_file_location('da_ca_helpers', HELPER_PATH)
H = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(H)


def manifest():
    return json.loads((ROOT / 'manifest.json').read_text())


def fixture(label):
    return next(x for x in manifest()['fixtures'] if x['label'] == label)


def load_module(arm='baseline'):
    return H.load_module(arm)


def call_with_metrics(module, system, user, kwargs):
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
    return result, {'http_attempts': attempts,
                    'latency_ms': round((time.monotonic() - started) * 1000)}


def pass1(label):
    item = fixture(label)
    raw = json.loads((ROOT / f'{label}.raw.json').read_text())
    module = load_module('baseline')
    data_text = H.recover_data_text(module, raw)
    user, user_tail = module.build_user_prompt(data_text)
    reconstructed = (user_tail + user) if user_tail is not None else user
    if reconstructed != raw['user_prompt']:
        raise ValueError('fresh Pass1 request does not match frozen user_prompt')
    thinking = bool(getattr(module.config, 'DA_THINKING_ENABLED', False))
    extra = None
    budget = 0
    if thinking:
        budget = int(getattr(module.config, 'DA_THINKING_BUDGET', 0) or 0)
        extra = {'enable_thinking': True}
        if budget > 0:
            extra['thinking_budget'] = budget
    kwargs = {'temperature': module.config.LLM_TEMPERATURE,
              'max_tokens': (budget + 8000) if thinking and budget > 0 else (16000 if thinking else 3000),
              'cache_system': module.config.DA_PROMPT_CACHE,
              'extra_body': extra,
              'timeout': 240 if thinking else 60,
              'user_tail': user_tail,
              'model': module.MODEL}
    out = ROOT / label / 'pass1'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'system.txt').write_text(module.SYSTEM_PROMPT, encoding='utf-8')
    (out / 'user.txt').write_text(raw['user_prompt'], encoding='utf-8')
    request = {'label': label, 'symbol': item['symbol'], 'model_parameters': kwargs,
               'system_sha256': hashlib.sha256(module.SYSTEM_PROMPT.encode()).hexdigest(),
               'user_sha256': hashlib.sha256(raw['user_prompt'].encode()).hexdigest(),
               'data_text_sha256': hashlib.sha256(data_text.encode()).hexdigest(),
               'request_matches_frozen_user': True}
    (out / 'request.json').write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding='utf-8')
    result, metrics = call_with_metrics(module, module.SYSTEM_PROMPT, user, kwargs)
    raw_result = copy.deepcopy(result)
    if not isinstance(result, dict) or result.get('_error'):
        record = {**request, **metrics, 'raw_model_result': raw_result}
        (out / 'record.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'label': label, 'pass1_error': (result or {}).get('_error'), **metrics}, ensure_ascii=False))
        return
    result.setdefault('_llm_latency_ms', metrics['latency_ms'])
    result.setdefault('_thinking_enabled', thinking)
    module._apply_score_band(result)
    contract = module._validate_output_contract(result, snapshot=(raw.get('collected') or {}).get('snapshot'))
    source_verification = module._verify_sourced_numbers(result, raw.get('collected') or {}, data_text=data_text)
    (out / 'initial_analysis.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    record = {**request, **metrics, 'raw_model_result': raw_result,
              'initial_analysis_sha256': hashlib.sha256(
                  json.dumps(result, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
              'contract': contract, 'source_verification': source_verification,
              'usage': result.get('_usage'), 'finish_reason': result.get('_finish_reason'),
              'model_reported': result.get('_model_reported')}
    (out / 'record.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'label': label, 'pass1_error': None, 'direction': result.get('direction'),
                      'score': result.get('score'), 'contract_violations': contract.get('violations'),
                      'usage': result.get('_usage'), **metrics}, ensure_ascii=False))


def capture(label):
    item = fixture(label)
    raw = json.loads((ROOT / f'{label}.raw.json').read_text())
    initial = json.loads((ROOT / label / 'pass1/initial_analysis.json').read_text())
    entry = {'label': label, 'symbol': item['symbol'], 'arms': {},
             'pass1_sha256': hashlib.sha256((ROOT / label / 'pass1/initial_analysis.json').read_bytes()).hexdigest(),
             'pass1_source': 'fresh single generation; no inverse reconstruction'}
    for arm in ('baseline', 'variant'):
        module = load_module(arm)
        data_text = H.recover_data_text(module, raw)
        seen = {}

        def stub(system, user, **kwargs):
            if not seen:
                seen.update({'system': system, 'user': user, 'kwargs': kwargs})
            return {'my_direction': 'SKIP'}

        module._call_llm = stub
        module._run_challenger(copy.deepcopy(initial), data_text)
        kwargs = seen['kwargs']
        if kwargs.get('model') != 'deepseek-v4.1-flash':
            raise ValueError('unexpected primary model')
        out = ROOT / label / 'captured'
        out.mkdir(parents=True, exist_ok=True)
        (out / f'{arm}.system.txt').write_text(seen['system'], encoding='utf-8')
        (out / f'{arm}.user.txt').write_text(seen['user'], encoding='utf-8')
        entry['arms'][arm] = {
            'system_fingerprint': module._prompt_fingerprint(seen['system'], 'v3-falsify', module._SAMPLING_TAG),
            'system_sha256': hashlib.sha256(seen['system'].encode()).hexdigest(),
            'user_sha256': hashlib.sha256(seen['user'].encode()).hexdigest(),
            'kwargs': kwargs,
            'data_text_sha256': hashlib.sha256(data_text.encode()).hexdigest(),
            'analysis_view_sha256': hashlib.sha256(module._challenger_analysis_view(initial).encode()).hexdigest(),
        }
    if len({a['data_text_sha256'] for a in entry['arms'].values()}) != 1:
        raise ValueError('data mismatch')
    if len({a['analysis_view_sha256'] for a in entry['arms'].values()}) != 1:
        raise ValueError('analysis mismatch')
    if entry['arms']['baseline']['kwargs'] != entry['arms']['variant']['kwargs']:
        raise ValueError('parameter mismatch')
    (ROOT / label / 'captured/manifest.json').write_text(
        json.dumps(entry, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'label': label, 'pass1_source': entry['pass1_source'],
                      'baseline_hash': entry['arms']['baseline']['system_fingerprint'],
                      'variant_hash': entry['arms']['variant']['system_fingerprint'],
                      'model_parameters': entry['arms']['baseline']['kwargs']}, ensure_ascii=False))


def challenge(label, arm, repeat):
    out = ROOT / label / 'challenge' / arm
    out.mkdir(parents=True, exist_ok=True)
    result_path = out / f'run{repeat}.json'
    if result_path.exists():
        print(f'skip existing {result_path}')
        return
    module = load_module(arm)
    system = (ROOT / label / 'captured' / f'{arm}.system.txt').read_text()
    user = (ROOT / label / 'captured' / f'{arm}.user.txt').read_text()
    kwargs = json.loads((ROOT / label / 'captured/manifest.json').read_text())['arms'][arm]['kwargs']
    result, metrics = call_with_metrics(module, system, user, kwargs)
    record = {'label': label, 'arm': arm, 'repeat': repeat, **metrics,
              'model_parameters': kwargs, 'result': result}
    result_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'label': label, 'arm': arm, 'repeat': repeat, **metrics,
                      'error': (result or {}).get('_error'),
                      'verdict': (result or {}).get('verdict'),
                      'direction': (result or {}).get('my_direction'),
                      'usage': (result or {}).get('_usage')}, ensure_ascii=False))


def json_contract(result):
    required = {'independent_direction', 'validation', 'agreed', 'my_direction', 'my_score_band',
                'my_score', 'skip_reason', 'falsification_target', 'flip_condition',
                'challenged_claims', 'missed_risks', 'verdict'}
    errors = []
    if not isinstance(result, dict):
        return ['not_a_dict']
    errors.extend(f'missing:{key}' for key in sorted(required - set(result)))
    if result.get('independent_direction') not in ('BUY', 'SELL_SHORT', 'SKIP'):
        errors.append('bad_independent_direction')
    if result.get('my_direction') not in ('BUY', 'SELL_SHORT', 'SKIP'):
        errors.append('bad_my_direction')
    if result.get('verdict') not in ('AGREE', 'PARTIAL_AGREE', 'DISAGREE'):
        errors.append('bad_verdict')
    if result.get('my_score_band') not in ('HIGH', 'MEDIUM', 'LOW'):
        errors.append('bad_score_band')
    if not isinstance(result.get('agreed'), bool):
        errors.append('agreed_not_bool')
    if not isinstance(result.get('validation'), dict) or not {'data', 'logic', 'market', 'risk', 'levels'} <= set(result['validation']):
        errors.append('bad_validation')
    for key in ('skip_reason', 'falsification_target', 'flip_condition'):
        if not str(result.get(key) or '').strip():
            errors.append(f'empty:{key}')
    if not isinstance(result.get('challenged_claims'), list):
        errors.append('bad_challenged_claims')
    else:
        for index, claim in enumerate(result['challenged_claims']):
            if not isinstance(claim, dict) or any(not str(claim.get(k) or '').strip()
                                                   for k in ('original_thesis', 'weakness', 'counter_evidence')):
                errors.append(f'bad_claim:{index}')
    if not isinstance(result.get('missed_risks'), list) or any(not str(x or '').strip() for x in result['missed_risks']):
        errors.append('bad_missed_risks')
    if result.get('_error'):
        errors.append('model_error')
    if result.get('_finish_reason') != 'stop':
        errors.append('finish_not_stop')
    return errors


NUMBER_RE = re.compile(r'(?<![A-Za-z_])-?\$?\d[\d,]*(?:\.\d+)?%?')


def numbers(text):
    return NUMBER_RE.findall(str(text or ''))


def normalize_number(value):
    return value.replace('$', '').replace(',', '').replace('%', '').lstrip('+')


def evidence_report(result, data_text):
    raw_numbers = {normalize_number(x) for x in numbers(data_text)}
    rows = []
    for index, claim in enumerate(result.get('challenged_claims') or []):
        evidence = claim.get('counter_evidence') or ''
        cited = numbers(evidence)
        missing = [x for x in cited if normalize_number(x) not in raw_numbers]
        rows.append({'index': index, 'original_thesis': claim.get('original_thesis'),
                     'weakness': claim.get('weakness'), 'counter_evidence': evidence,
                     'cited_numbers': cited, 'missing_literal_numbers': missing,
                     'status': 'needs_semantic_review'})
    return rows


def downstream(module, initial, challenge, scanner_direction):
    challenge = copy.deepcopy(challenge)
    challenge.update({'_challenger_tier': 'primary', '_challenger_model_used': 'deepseek-v4.1-flash',
                      '_challenger_fallback': False, '_challenger_self_debate': False})
    merged = module._merge_debate_results(copy.deepcopy(initial), challenge)
    llm_out = merged
    conf = module.adapter._compute_confidence({'_llm_output': llm_out}) if hasattr(module, 'adapter') else None
    if conf is None:
        import adapter
        conf = adapter._compute_confidence({'_llm_output': llm_out})
    score = float(llm_out.get('score') or 0)
    direction = llm_out.get('direction')
    deb = llm_out.get('_debate') or {}
    cd = str(deb.get('challenger_direction') or '').upper()
    od = str(llm_out.get('direction') or '').upper()
    oc = str(llm_out.get('debate_outcome') or '').upper()
    gate_conf = 0.5
    if cd == od:
        if (deb.get('challenger_agreed') or 'AGREE' in oc) and not deb.get('challenger_fallback'):
            gate_conf += 0.2
    elif 'SKIP' in (cd, od):
        gate_conf = max(gate_conf, score / 10)
    else:
        gate_conf -= 0.15
    gate_conf = max(0.1, min(0.95, gate_conf))
    if direction not in ('BUY', 'SELL_SHORT'):
        tier = 'SKIP'
    elif score >= 7 and gate_conf >= 0.7:
        tier = '优'
    elif score >= 6 and gate_conf >= 0.6:
        tier = '良'
    elif score >= 5 and gate_conf >= 0.5:
        tier = '中'
    else:
        tier = '差'
    if scanner_direction == 'bearish' and direction == 'BUY':
        tier = 'SKIP'
    decision = {'优': 'PASS', '良': 'PASS', '中': 'WARN', '差': 'BLOCK', 'SKIP': 'BLOCK'}[tier]
    final_conf = max(0.25, conf - 0.15) if decision == 'WARN' else conf
    return {'state': deb.get('challenger_verdict_state'), 'adapter_conf': conf,
            'gate_decision_conf': gate_conf, 'tier': tier, 'gate_decision': decision,
            'bridge_conf_after_warn': final_conf,
            'publishable': direction in ('BUY', 'SELL_SHORT') and decision in ('PASS', 'WARN'),
            'merged_direction': direction}


def evaluate():
    module = load_module('baseline')
    report = {'samples': [], 'totals': {'logical_calls': 0, 'http_attempts': 0,
                                       'prompt_tokens': 0, 'completion_tokens': 0,
                                       'total_tokens': 0, 'latency_ms': 0, 'errors': 0}}
    for item in manifest()['fixtures']:
        label = item['label']
        raw = json.loads((ROOT / f'{label}.raw.json').read_text())
        data_text = H.recover_data_text(module, raw)
        initial = json.loads((ROOT / label / 'pass1/initial_analysis.json').read_text())
        pass1_record = json.loads((ROOT / label / 'pass1/record.json').read_text())
        sample = {'label': label, 'symbol': item['symbol'],
                  'pass1': {'direction': initial.get('direction'), 'score': initial.get('score'),
                            'contract': pass1_record.get('contract'),
                            'source_verification': pass1_record.get('source_verification')},
                  'arms': {}}
        records = [pass1_record]
        for arm in ('baseline', 'variant'):
            runs = []
            for path in sorted((ROOT / label / 'challenge' / arm).glob('run*.json')):
                rec = json.loads(path.read_text())
                result = rec['result']
                records.append(rec)
                runs.append({'repeat': rec['repeat'], 'json_errors': json_contract(result),
                             'verdict': result.get('verdict'), 'my_direction': result.get('my_direction'),
                             'independent_direction': result.get('independent_direction'),
                             'raw_agreed': result.get('agreed'),
                             'challenged_count': len(result.get('challenged_claims') or []),
                             'missed_risks_count': len(result.get('missed_risks') or []),
                             'evidence': evidence_report(result, data_text),
                             'downstream': downstream(module, initial, result, item['scanner_direction']),
                             'usage': result.get('_usage'), 'http_attempts': rec['http_attempts'],
                             'latency_ms': rec['latency_ms'], 'finish_reason': result.get('_finish_reason')})
            sample['arms'][arm] = {'runs': runs,
                                   'verdict_stable': len({r['verdict'] for r in runs}) == 1,
                                   'direction_stable': len({r['my_direction'] for r in runs}) == 1,
                                   'json_all_valid': all(not r['json_errors'] for r in runs)}
        sample['cross_arm_verdicts'] = {arm: [r['verdict'] for r in value['runs']]
                                       for arm, value in sample['arms'].items()}
        sample['cross_arm_downstream'] = {arm: [r['downstream'] for r in value['runs']]
                                         for arm, value in sample['arms'].items()}
        report['samples'].append(sample)
        for rec in records:
            result = rec.get('result') or rec.get('raw_model_result') or {}
            usage = rec.get('usage') or result.get('_usage') or {}
            report['totals']['logical_calls'] += 1
            report['totals']['http_attempts'] += rec['http_attempts']
            report['totals']['latency_ms'] += rec['latency_ms']
            report['totals']['errors'] += bool(result.get('_error'))
            for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                report['totals'][key] += usage.get(key, 0)
    source = json.loads((ROOT / 'manifest.json').read_text())['source_manifest']
    current = hashlib.sha256(Path(source['source']).read_bytes()).hexdigest()
    report['production_prompt_unchanged'] = current == source['source_sha256']
    report['evidence_note'] = 'Literal-number checks are leads, not truth verdicts; semantic review required.'
    (ROOT / 'evaluation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'totals': report['totals'], 'production_prompt_unchanged': report['production_prompt_unchanged'],
                      'samples': [{'label': s['label'], 'pass1': s['pass1'],
                                   'cross_arm_verdicts': s['cross_arm_verdicts'],
                                   'cross_arm_downstream': s['cross_arm_downstream'],
                                   'stability': {a: {'verdict': v['verdict_stable'],
                                                     'direction': v['direction_stable'],
                                                     'json': v['json_all_valid']}
                                                 for a, v in s['arms'].items()}}
                                  for s in report['samples']]}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('pass1', 'capture'):
        p = sub.add_parser(name)
        p.add_argument('--label', required=True)
    p = sub.add_parser('challenge')
    p.add_argument('--label', required=True)
    p.add_argument('--arm', choices=('baseline', 'variant'), required=True)
    p.add_argument('--repeat', type=int, choices=(1, 2), required=True)
    sub.add_parser('evaluate')
    args = parser.parse_args()
    if args.command == 'pass1':
        pass1(args.label)
    elif args.command == 'capture':
        capture(args.label)
    elif args.command == 'challenge':
        challenge(args.label, args.arm, args.repeat)
    else:
        evaluate()


if __name__ == '__main__':
    main()
