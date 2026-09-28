"""No web server and no production startup. One local scenario report at a time."""
from .market import (SPECS, decision_report, timestamp_replay, ordering_replay, candle_replay,
                     future_cache_replay, disconnected_options_replay)
from .trades import TRADE_SCENARIOS, trade_report

DIAGNOSTICS = ('rest_timestamp_fix', 'old_aws_timestamp_bug', 'timestamp_ordering', 'future_cache_poisoning',
               'partial_5m_candles', 'partial_15m_candles', 'disconnected_options_cache')
NAMES = tuple(SPECS) + TRADE_SCENARIOS + DIAGNOSTICS


def run(name):
    if name in SPECS:
        return decision_report(name)
    if name in TRADE_SCENARIOS:
        return trade_report(name)
    if name not in DIAGNOSTICS:
        raise KeyError(name)
    if name in ('rest_timestamp_fix', 'old_aws_timestamp_bug'):
        result = timestamp_replay()
        mode = 'corrected' if name == 'rest_timestamp_fix' else 'old'
        checks = {'quote': result[mode], 'fresh': result[mode+'_fresh']}
    elif name == 'timestamp_ordering':
        checks = ordering_replay()
    elif name == 'future_cache_poisoning':
        checks = future_cache_replay()
    elif name == 'disconnected_options_cache':
        checks = disconnected_options_replay()
    else:
        checks = candle_replay('5m' if name == 'partial_5m_candles' else '15m')
    return {'scenario': name, 'kind': 'diagnostic', 'checks': checks}


def human(report):
    lines = [f"Scenario: {report['scenario']}"]
    if report['kind'] == 'signal':
        q = report['data_quality']
        lines.append(f"Options coverage: {q['option_coverage_percent']:.4f}%" if q['option_coverage_percent'] is not None else 'Options coverage: unavailable')
        for key in ('structure_fresh', 'options_fresh', 'breadth_fresh', 'vix_fresh', 'critical_values_present'):
            lines.append(f'{key}: {str(q[key]).lower()}')
        lines += [f"Bullish: {report['bullish_score']:g}", f"Bearish: {report['bearish_score']:g}",
                  f"Aligned categories: {len(report['aligned_categories'])}", 'Category scores:']
        for name, category in report['category_scores'].items():
            lines.append(f"  {name}: {category['direction']} (+{category['bullish_points']:g} / -{category['bearish_points']:g})")
        lines += (['Blocking:'] + ['  '+item for item in q['blocking_reasons']] if q['blocking_reasons'] else ['Blocking: none'])
        lines.append('Decision: '+report['decision'])
        lines.append('Dashboard state (no active plan): '+report['ui_signal']['decision'])
    elif report['kind'] == 'trade':
        lines += [f"Status: {report['status']}", f"Monitoring: {report['monitoring_status']}",
                  'Events: '+(', '.join(e['kind'] for e in report['events']) or 'none'),
                  f"Persisted on reopen: {report['persisted_on_reopen']}"]
    else:
        import json
        lines.append(json.dumps(report['checks'], indent=2, allow_nan=False))
    lines.extend('Note: '+note for note in report.get('notes', ()))
    return '\n'.join(lines)
