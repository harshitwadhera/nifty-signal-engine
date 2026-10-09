from dataclasses import asdict, replace
from datetime import datetime, time
from unittest.mock import Mock
import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.signals import SignalConfig, SignalEngine
from app.signals.execution_models import ExecutionConfig
from app.signals.live import LiveSignals
from app.signals.lifecycle import Submission
from app.signals.planning import SignalPlanner
from test_decisions import ready
from test_execution import example
from test_options import service


def gates(decision):
    return {g['key']: g for g in decision.qualification_gates}


@pytest.mark.parametrize('bullish,direction', [(True, 'CALL'), (False, 'PUT')])
def test_gates_explain_the_authoritative_decision(bullish, direction):
    result = SignalEngine().decide(ready(bullish))
    checks = gates(result)
    assert result.decision == direction
    assert all(g['status'] in ('PASS', 'INFO') for g in checks.values())
    assert checks['minimum_score']['actual'] == 90 and checks['minimum_score']['required'] == 60
    assert checks['minimum_aligned']['actual'] == checks['minimum_aligned']['required'] == 4
    assert checks['minimum_separation']['required'] == 15
    assert checks['option_coverage']['status'] == 'INFO' and checks['option_coverage']['required'] is None and checks['breadth_coverage']['required'] == 90
    assert checks['structure_fresh']['required'] == 'Fresh within 60s'
    assert {'structure_fresh', 'options_signal_data', 'breadth_fresh', 'vix_fresh', 'critical_values_present',
            'major_contradiction', 'index_match'} <= checks.keys()
    json.dumps(asdict(result), allow_nan=False)


def test_incomplete_chain_keeps_usable_components_and_informational_coverage(service):
    options, client, clock = service
    complete_quote = client.quote.side_effect
    client.quote.side_effect = lambda symbols: complete_quote(symbols[1:])
    options.cycle(force=True)
    summary, _ = options.response('NIFTY')
    sample = replace(ready(), options=summary)
    result = SignalEngine().decide(sample)
    checks = gates(result)
    assert summary['stale'] and summary['coverage']['percent'] > 95
    assert checks['option_coverage']['status'] == 'INFO'
    assert checks['options_signal_data']['status'] == 'PASS'
    assert checks['full_chain_fresh']['status'] == 'INFO'
    assert result.category_scores['options_positioning'].available_weight > 0
    assert result.category_scores['options_positioning'].maximum_weight == 30


@pytest.mark.parametrize('source,key', [('structure', 'structure_fresh'), ('options', 'options_signal_data'),
                                       ('breadth', 'breadth_fresh'), ('volatility', 'vix_fresh')])
def test_each_freshness_gate_blocks_independently(source, key):
    sample = ready()
    getattr(sample, source)['stale'] = True
    if source == 'options':
        sample.options.update(evidence_fresh=False, atm_ce=None, atm_pe=None)
    result = SignalEngine().decide(sample)
    assert gates(result)[key]['status'] == 'BLOCK' and result.decision == 'NO_TRADE'


def test_partial_weights_and_configured_maximum_are_distinct():
    sample = ready()
    sample.options.update(evidence_fresh=False, atm_ce=None, atm_pe=None)
    sample.breadth['percent_above_15m_ema20'] = None
    categories = SignalEngine().decide(sample).category_scores
    assert [(categories[name].available_weight, categories[name].maximum_weight) for name in
            ('price_trend', 'options_positioning', 'breadth_constituents', 'volatility', 'futures_structure')] == [
                (30, 30), (0, 30), (12, 15), (10, 10), (15, 15)]
    customized = SignalConfig(price_weight=25, options_weight=35)
    categories = SignalEngine(customized).decide(sample).category_scores
    assert categories['price_trend'].maximum_weight == 25
    assert categories['options_positioning'].maximum_weight == 35
    assert sum(c.maximum_weight for c in categories.values()) == 100


def test_gate_failures_match_critical_coverage_and_contradiction_rules():
    sample = ready()
    sample.structure['spot'] = None
    sample.options['coverage']['received_contracts'] = 94
    sample.breadth['full_index'] = False
    sample.structure['future_change_percent'] = -1
    result = SignalEngine().decide(sample)
    checks = gates(result)
    for key in ('critical_values_present', 'breadth_coverage', 'major_contradiction'):
        assert checks[key]['status'] == 'BLOCK'
    assert checks['option_coverage']['status'] == 'INFO'
    assert 'full index unavailable' in checks['breadth_coverage']['detail']
    assert 'futures_structure' in checks['major_contradiction']['actual']


@pytest.mark.parametrize('stamp,opened', [
    ('2026-09-28T09:14:59+05:30', False), ('2026-09-28T09:15:00+05:30', True),
    ('2026-09-28T14:59:59+05:30', True), ('2026-09-28T15:00:00+05:30', False),
    ('2026-09-28T09:30:00+00:00', False), ('2026-10-03T10:00:00+05:30', False),
])
def test_planner_and_explanation_share_the_unchanged_ist_entry_window(stamp, opened):
    planner = SignalPlanner()
    gate = planner.entry_window_gate(stamp)
    assert gate['passed'] is opened
    assert gate['status'] == ('PASS' if opened else 'BLOCK')
    assert '15:00' in gate['required'] and 'IST' in gate['required']
    sample = replace(ready(), as_of=stamp)
    if not opened:
        assert planner.build(sample, []).reasons == ('Outside new-entry window',)


def test_entry_window_explanation_uses_execution_config():
    gate = SignalPlanner(config=ExecutionConfig(new_entry_cutoff=time(14, 45))).entry_window_gate('2026-09-28T14:50:00+05:30')
    assert gate['status'] == 'BLOCK' and '14:45' in gate['required']
    assert ExecutionConfig().new_entry_cutoff == time(15)


def test_live_api_distinguishes_qualified_score_from_planning_cutoff():
    stamp = '2026-09-28T15:01:00+05:30'
    sample = replace(ready(), as_of=stamp)
    for source in (sample.structure, sample.breadth, sample.volatility):
        source['as_of'] = stamp
    sample.options.update(timestamp=stamp, last_full_chain_refresh_at=stamp, selected_expiry='2026-10-06', expiry_selection={
        'nearest': '2026-09-29', 'monthly': '2026-09-29', 'analysis_expiry': '2026-10-06',
        'analysis_expiry_policy': {'code': 'NIFTY_NEXT_WEEK_EXPIRY', 'label': 'Next-week expiry',
                                 'reason': 'Current-week expiry skipped for Monday/Tuesday analysis', 'available': True}})
    stream = Mock()
    stream.state.clock.return_value = datetime.fromisoformat(stamp)
    live = LiveSignals(stream, Mock(), Mock(), Mock())
    try:
        assert live.engine.decide(sample).decision == 'CALL'
        submission = live.lifecycle.submit(sample, [])
        live._publish('NIFTY', sample, submission)
        wrapper = Mock()
        wrapper.current.side_effect = live.current
        with TestClient(create_app(Settings(), stream=Mock(), engine=Mock(), options=Mock(), signals=wrapper)) as browser:
            data = browser.get('/api/signals/nifty').json()
        assert data['decision'] == 'NO_TRADE' and data['current_qualification']['decision'] == 'CALL'
        assert data['expiry_selection']['analysis_expiry'] == '2026-10-06'
        checks = {g['key']: g for g in data['qualification_gates']}
        assert checks['minimum_score']['status'] == 'PASS'
        assert checks['entry_window']['status'] == checks['planning']['status'] == 'BLOCK'
        assert checks['planning']['detail'] == 'Outside new-entry window'
        assert data['category_scores']['options_positioning']['maximum_weight'] == 30
        assert 'Outside new-entry window' in data['data_quality']['planning_reasons']
    finally:
        live.close()


def test_active_creation_scores_and_expiry_remain_separate_from_current_gates():
    sample, rows, engine = example()
    engine.config = SignalConfig()
    engine.decide.return_value = SignalEngine().decide(ready())
    stream = Mock()
    stream.state.clock.return_value = datetime.fromisoformat(sample.as_of)
    live = LiveSignals(stream, Mock(), Mock(), Mock())
    live.lifecycle.planner = SignalPlanner(engine)
    try:
        submission = live.lifecycle.submit(sample, rows)
        sample.options.update(evidence_fresh=False, atm_ce=None, atm_pe=None, stale=True, selected_expiry='2026-10-06', expiry_selection={'analysis_expiry': '2026-10-06'})
        live._publish('NIFTY', sample, submission)
        data = live.current('NIFTY')
        assert data['score_basis'] == 'candidate_creation'
        assert data['category_scores']['options_positioning']['available_weight'] == 30
        assert data['current_qualification']['category_scores']['options_positioning']['available_weight'] == 0
        assert data['score_expiry_selection']['analysis_expiry'] == '2026-09-28'
        assert data['expiry_selection']['analysis_expiry'] == '2026-10-06'
        assert next(g for g in data['qualification_gates'] if g['key'] == 'options_signal_data')['status'] == 'BLOCK'
    finally:
        live.close()


def test_current_entry_diagnostics_are_separate_from_planned_rr_and_clear_when_stale():
    from test_execution import move
    sample, rows, engine = example()
    engine.config = SignalConfig()
    engine.decide.return_value = SignalEngine().decide(ready())
    stream = Mock()
    live = LiveSignals(stream, Mock(), Mock(), Mock())
    live.engine = engine
    live.lifecycle.planner = SignalPlanner(engine)
    try:
        submission = live.lifecycle.submit(sample, rows)
        later, quotes = move(sample, rows, 1, 104)
        stream.state.clock.return_value = datetime.fromisoformat(later.as_of)
        record = live.lifecycle.advance(submission.record.signal_id, later, quotes)
        live._publish('NIFTY', later, Submission('NO_TRADE', record, False, ()))
        data = live.current('NIFTY')
        assert data['entry_diagnostics']['status'] == 'WAITING_FOR_PULLBACK'
        assert data['entry_diagnostics']['t1_rr'] == 1.2
        assert data['record']['plan']['t1_rr'] == 10
        assert data['record']['entry_diagnostics']['underlying'] == 104
        decision = engine.decide.return_value
        engine.decide.return_value = replace(decision, decision='NO_TRADE',
            data_quality={**decision.data_quality, 'structure_fresh':False})
        live._publish('NIFTY', later, Submission('NO_TRADE', record, False, ()))
        data = live.current('NIFTY')
        assert data['entry_diagnostics']['status'] == 'DATA_UNAVAILABLE'
        assert data['entry_diagnostics']['t1_rr'] is None
        assert data['entry_diagnostics']['underlying'] is None
    finally:
        live.close()
