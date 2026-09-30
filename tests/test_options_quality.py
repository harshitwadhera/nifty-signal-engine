"""Production-shaped chains exercise analytics -> scoring -> qualification."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.logging_config import JsonFormatter
from app.main import create_app
from app.options.analytics import OptionsAnalytics, positioning
from app.options.config import OptionsConfig
from app.options.state import normalize_quote
from app.signals import SignalConfig, SignalEngine
from app.signals.live import LiveSignals
from app.signals.lifecycle import Submission
from test_decisions import ready
from test_options import service, raw
from unittest.mock import Mock


NOW = datetime.fromisoformat('2026-09-25T10:00:00+05:30')
EXPIRY = date(2026, 9, 30)
SPOT = 25000


def chain(expected=214, non_fresh=0):
    rows = []
    for n in range(expected//2):
        strike = SPOT + 50*(n-expected//4)
        for kind in ('CE', 'PE'):
            oi = 1000 if kind == 'CE' else 2000
            if strike == SPOT + (50 if kind == 'CE' else -50):
                oi *= 10
            row = dict(strike=strike, option_type=kind, expiry=EXPIRY.isoformat(),
                       ltp=100, bid=99.9, ask=100.1, bid_quantity=100, ask_quantity=100,
                       oi=oi, oi_change_session=100, oi_change_percent=10,
                       volume=1000 if kind == 'CE' else 2000,
                       price_change_percent=2 if kind == 'CE' else -2,
                       iv=.2, liquidity_state='LIQUID', stale=False)
            row['positioning'] = positioning(row, OptionsConfig())
            rows.append(row)
    for row in sorted(rows, key=lambda r: abs(r['strike']-SPOT), reverse=True)[:non_fresh]:
        # Leave attractive old data in the stale row: it must never be used.
        row.update(stale=True, oi=10**12, oi_change_session=-10**10, volume=10**12,
                   positioning='SHORT_COVERING')
    return rows


def summarize(rows, index='NIFTY'):
    return OptionsAnalytics.summarize(index, EXPIRY, rows, SPOT, None, len(rows), NOW,
                                      NOW.isoformat(), None, OptionsConfig())


def availability(score):
    return {k: v['available_weight'] for k, v in score.component_availability.items()}


@pytest.mark.parametrize('index,expected,non_fresh,percent', [
    ('NIFTY', 214, 0, 100), ('NIFTY', 214, 6, 97.2), ('BANKNIFTY', 314, 48, 84.7)])
def test_production_chains_keep_all_usable_components(index, expected, non_fresh, percent):
    summary = summarize(chain(expected, non_fresh), index)
    score = SignalEngine().options(summary)
    assert summary['full_chain_fresh'] is (non_fresh == 0)
    assert summary['coverage']['fresh_contracts'] == expected-non_fresh
    assert summary['coverage']['percent'] == percent
    assert summary['near_atm_quality']['percent'] == 100
    component_weights = availability(score)
    assert component_weights['atm_behavior'] == 9
    assert score.direction == 'bullish'
    if non_fresh == 0:
        assert component_weights == dict(positioning_flow=10.5, atm_behavior=9, oi_wall_breakout=4.5, pcr_confirmation=6)
        assert score.available_weight == 30
        assert score.bullish_points == 25.5 and score.bearish_points == 0
    else:
        assert 9 < score.available_weight < 30
        assert 0 < component_weights['positioning_flow'] < 10.5
        assert 0 < component_weights['oi_wall_breakout'] < 4.5
        assert 0 < component_weights['pcr_confirmation'] < 6
        assert score.bullish_points > 9 and score.bearish_points == 0
    result = SignalEngine().decide(replace(ready(index=index), options=summary))
    assert result.decision == 'CALL' and result.confidence > 70
    gates = {g['key']: g for g in result.qualification_gates}
    assert gates['option_coverage']['status'] == gates['full_chain_fresh']['status'] == 'INFO'
    assert gates['options_signal_data']['status'] == 'PASS'
    assert not result.data_quality['blocking_reasons']
    json.dumps(asdict(result), allow_nan=False)



def test_sparse_chain_scales_broad_components_instead_of_claiming_full_budget():
    rows = chain()
    for row in rows:
        if row['strike'] != SPOT:
            row['stale'] = True
    summary = summarize(rows)
    score = SignalEngine().options(summary)
    weights = availability(score)
    assert summary['coverage']['fresh_contracts'] == 2
    assert summary['coverage']['percent'] < 1
    assert weights['atm_behavior'] == 9
    assert 0 < weights['positioning_flow'] < .2
    assert 0 < weights['oi_wall_breakout'] < .1
    assert 0 < weights['pcr_confirmation'] < .2
    assert 9 < score.available_weight < 10
    assert score.direction == 'bullish'
    result = SignalEngine().decide(replace(ready(), options=summary))
    assert result.decision == 'NO_TRADE'
    assert 'Winning score below minimum' in result.data_quality['blocking_reasons']


def test_component_population_scaling_has_no_hard_global_threshold():
    engine = SignalEngine()
    heavier = engine.options(summarize(chain(non_fresh=6)))
    lighter = engine.options(summarize(chain(non_fresh=48)))
    assert heavier.component_availability['atm_behavior']['available_weight'] == 9
    assert lighter.component_availability['atm_behavior']['available_weight'] == 9
    for key in ('positioning_flow', 'oi_wall_breakout', 'pcr_confirmation'):
        assert 0 < lighter.component_availability[key]['available_weight'] < heavier.component_availability[key]['available_weight']
    assert lighter.available_weight > 9

def test_far_contracts_age_without_availability_collapse_or_max_pain_dependency():
    engine = SignalEngine()
    full, partial = summarize(chain()), summarize(chain(non_fresh=6))
    assert full['max_pain'] is not None and partial['max_pain'] is None
    before, after = engine.options(full), engine.options(partial)
    assert before.available_weight == 30
    assert 9 < after.available_weight < 30
    assert before.bullish_points == 25.5
    assert 9 < after.bullish_points < before.bullish_points
    assert before.bearish_points == after.bearish_points == 0
    assert availability(after)['atm_behavior'] == availability(before)['atm_behavior'] == 9
    assert partial['call_oi_wall'] == SPOT+50 and partial['put_oi_wall'] == SPOT-50


@pytest.mark.parametrize('stale_sides,weight', [(('CE', 'PE'), 0), (('CE',), 4.5), (('PE',), 4.5)])
def test_atm_freshness_is_independent_even_at_high_coverage(stale_sides, weight):
    rows = chain()
    for row in rows:
        if row['strike'] == SPOT and row['option_type'] in stale_sides:
            row['stale'] = True
    summary = summarize(rows)
    assert summary['coverage']['percent'] >= 99
    scored = SignalEngine().options(summary)
    assert availability(scored)['atm_behavior'] == weight
    assert scored.available_weight == 21+weight
    for side in stale_sides:
        assert not scored.component_availability['atm_behavior']['sides'][side.lower()]['usable']


@pytest.mark.parametrize('field,value', [
    ('stale', True), ('ltp', None), ('ltp', 0), ('bid', 0), ('ask', 1),
    ('bid_quantity', 0), ('ask_quantity', None), ('iv', None), ('iv', 0), ('iv', 6),
    ('iv', float('nan')), ('liquidity_state', 'POOR'), ('positioning', 'UNAVAILABLE'),
    ('oi', None), ('oi_change_session', None), ('price_change_percent', None)])
def test_invalid_atm_side_contributes_neither_weight_nor_points(field, value):
    summary = summarize(chain())
    row = dict(summary['atm_ce'], **{field: value})
    score = SignalEngine().options({'atm_ce': row})
    assert score.available_weight == score.bullish_points == score.bearish_points == 0
    assert score.direction == 'unavailable'


def test_each_ratio_uses_its_own_paired_population():
    rows = chain()
    for row in rows:
        row['volume'] = None
    rows[0].update(oi=None)  # Its PE counterpart must be omitted from OI PCR too.
    summary = summarize(rows)
    assert summary['pcr_volume'] is None and summary['near_atm_pcr_volume'] is None
    assert summary['pcr_oi'] == 2 and summary['near_atm_pcr_oi'] == 2
    assert summary['pcr_quality']['pcr_oi']['usable_contracts'] == 212
    score = SignalEngine().options(summary)
    assert availability(score)['pcr_confirmation'] == 4
    assert score.component_availability['pcr_confirmation']['usable_ratios'] == 2


def test_one_sided_and_zero_denominator_pcr_and_walls_are_unavailable():
    for missing in (None, 0):
        rows = chain()
        for row in rows:
            if row['option_type'] == 'CE':
                row.update(oi=missing, volume=missing)
        summary = summarize(rows)
        score = SignalEngine().options(summary)
        assert summary['pcr_oi'] is None and summary['near_atm_pcr_oi'] is None
        assert availability(score)['pcr_confirmation'] == availability(score)['oi_wall_breakout'] == 0


def test_oi_and_volume_components_do_not_require_a_valid_option_price():
    rows = chain()
    for row in rows:
        row['ltp'] = None
    summary = summarize(rows)
    assert summary['pcr_oi'] == summary['pcr_volume'] == summary['near_atm_pcr_oi'] == 2
    assert availability(SignalEngine().options(summary)) == dict(
        positioning_flow=0, atm_behavior=0, oi_wall_breakout=4.5, pcr_confirmation=6)


def test_missing_walls_remove_only_wall_budget_and_case_b_sums_to_23_5():
    summary = summarize(chain())
    summary.update(call_oi_wall=None, pcr_volume=None)
    scored = SignalEngine().options(summary)
    assert availability(scored) == dict(positioning_flow=10.5, atm_behavior=9, oi_wall_breakout=0, pcr_confirmation=4)
    assert scored.available_weight == 23.5


def test_only_atm_data_is_9_points_and_naturally_cannot_qualify():
    summary = summarize(chain())
    data = {k: summary[k] for k in ('atm_ce', 'atm_pe', 'timestamp')}
    scored = SignalEngine().options(data)
    assert scored.available_weight == scored.bullish_points == 9
    result = SignalEngine().decide(replace(ready(), options=data))
    assert result.bullish_score == 69 and len(result.aligned_categories) == 4
    assert result.decision == 'NO_TRADE'
    assert result.data_quality['blocking_reasons'] == ('Winning score below minimum',)


def test_partial_positioning_never_invents_a_missing_side_or_calls_oi_additions_writing():
    rows = chain()
    for row in rows:
        if row['option_type'] == 'PE':
            row.update(price_change_percent=None, positioning='UNAVAILABLE')
    summary = summarize(rows)
    assert summary['positioning_quality'] == {'call': 107, 'put': 0}
    assert summary['put_writing_zones'] == summary['put_unwinding_zones'] == []
    assert summary['call_writing_zones'] == []  # Calls have rising price AND OI.
    score = SignalEngine().options(summary)
    assert availability(score)['positioning_flow'] == 0
    assert availability(score)['atm_behavior'] == 4.5
    # Neutral usable positioning gets its budget without fabricating votes.
    for row in rows:
        row.update(price_change_percent=0, oi_change_percent=0, oi_change_session=0, positioning='NEUTRAL')
    summary = summarize(rows)
    score = SignalEngine().options(summary)
    assert availability(score)['positioning_flow'] == 10.5
    assert score.bullish_points == score.bearish_points == 0
    # A sub-threshold price move keeps positioning neutral even if OI fell.
    for row in rows:
        row.update(oi_change_session=-100, oi_change_percent=-10)
    summary = summarize(rows)
    assert summary['call_unwinding_zones'] == summary['put_unwinding_zones'] == []
    score = SignalEngine().options(summary)
    assert availability(score)['positioning_flow'] == 10.5
    assert score.bullish_points == score.bearish_points == 0


@pytest.mark.parametrize('ratio', [.1, 1, 5])
def test_pcr_never_establishes_or_reverses_non_pcr_direction(ratio):
    quality = {"paired_strikes": 10, "population_pairs": 10}
    summary = dict(evidence_fresh=True, pcr_oi=ratio, pcr_volume=ratio, near_atm_pcr_oi=ratio,
                   pcr_quality={key: quality for key in ("pcr_oi", "pcr_volume", "near_atm_pcr_oi")})
    engine = SignalEngine()
    result = engine.options(summary)
    assert result.direction == 'neutral' and result.available_weight == 6
    assert result.bullish_points == result.bearish_points == 0
    summary['atm_ce'] = summarize(chain())['atm_ce']
    result = engine.options(summary)
    assert result.direction == 'bullish' and result.bearish_points == 0
    assert result.bullish_points == (10.5 if ratio > 1.2 else 4.5)
    assert ('PCR conflicts with non-PCR positioning' in result.contradictions) is (ratio < .8)


@pytest.mark.parametrize('mask', range(16))
def test_component_sums_and_original_tally_direction_margin(mask):
    summary = summarize(chain())
    if mask & 1:
        summary['positioning_quality'] = {}
    if mask & 2:
        summary.update(atm_ce=None, atm_pe=None)
    if mask & 4:
        summary['call_oi_wall'] = None
    if mask & 8:
        summary.update(pcr_oi=None, pcr_volume=None, near_atm_pcr_oi=None)
    before = deepcopy(summary)
    for bearish in (False, True):
        if bearish and summary['atm_ce']:
            summary['atm_ce'] = dict(summary['atm_ce'], positioning='SHORT_BUILDUP')
            summary['atm_pe'] = dict(summary['atm_pe'], positioning='LONG_BUILDUP')
        score = SignalEngine().options(summary)
        assert score.available_weight == sum(availability(score).values()) <= 30
        assert 0 <= score.bullish_points+score.bearish_points <= score.available_weight
        net = score.bullish_points-score.bearish_points
        expected = ('unavailable' if not score.available_weight else 'neutral' if abs(net) <= score.available_weight*.1
                    else 'bullish' if net > 0 else 'bearish')
        assert score.direction == expected
    assert before['coverage'] == summary['coverage']


def test_completely_stale_chain_has_no_directional_or_available_points():
    summary = summarize(chain(non_fresh=214))
    scored = SignalEngine().options(summary)
    assert scored.available_weight == scored.bullish_points == scored.bearish_points == 0
    assert summary['pcr_oi'] is None and summary['call_oi_wall'] is None
    result = SignalEngine().decide(replace(ready(), options=summary))
    assert result.decision == 'NO_TRADE' and not result.data_quality['options_signal_data']


def test_snapshot_age_gate_uses_component_observation_not_rest_completion():
    summary = summarize(chain(non_fresh=48))
    summary['last_full_chain_refresh_at'] = (NOW-timedelta(minutes=5)).isoformat()
    assert SignalEngine().decide(replace(ready(), options=summary)).decision == 'CALL'
    summary['timestamp'] = (NOW-timedelta(seconds=61)).isoformat()
    result = SignalEngine().decide(replace(ready(), options=summary))
    assert result.decision == 'NO_TRADE' and not result.data_quality['options_signal_data']


def test_thresholds_breadth_expiry_and_other_budgets_unchanged():
    config = SignalConfig()
    assert (config.minimum_score, config.minimum_aligned, config.minimum_separation) == (70, 4, 15)
    assert (config.minimum_breadth_coverage, config.minimum_option_coverage) == (90, 95)
    assert (config.price_weight, config.options_weight, config.breadth_weight, config.volatility_weight, config.futures_weight) == (30,30,15,10,15)
    sample = replace(ready(), options=summarize(chain(non_fresh=48)))
    sample.breadth['coverage_percent'] = 89
    assert SignalEngine().decide(sample).decision == 'NO_TRADE'
    sample.breadth['coverage_percent'] = 100
    sample.options['expiry_selection'] = {'analysis_expiry': '2026-10-06'}
    assert SignalEngine().decide(sample).decision == 'NO_TRADE'


def test_service_api_and_stream_atm_quality_agree_without_extra_polling(service):
    options, client, clock = service
    options.cycle(force=True)
    calls = client.quote.call_count
    clock.return_value += timedelta(seconds=61)
    options._context = lambda index: (100, None)
    for kind in ('CE', 'PE'):
        contract = options.discovery.get_option_contract('NIFTY', date(2026, 9, 28), 100, kind)
        quote = raw(clock, kind)
        quote['volume_traded'] = quote['volume']
        options.process_tick(options._generation, contract.instrument_token,
                             normalize_quote(quote, clock(), 'stream'), clock())
    summary, _ = options.response('NIFTY')
    assert summary['coverage']['fresh_contracts'] == 0 and not summary['full_chain_fresh']
    assert summary['options_total_available_weight'] == 9
    assert all(q['fresh'] and q['usable'] for q in summary['atm_quality'].values())
    assert summary['component_availability'] == SignalEngine().options(summary).component_availability
    with TestClient(create_app(Settings(), stream=Mock(), engine=Mock(), options=options, signals=Mock())) as browser:
        data = browser.get('/api/options/nifty').json()
        assert data['options_total_available_weight'] == 9
        assert data['atm_quality']['ce']['liquidity'] == 'LIQUID'
        assert browser.get('/api/options/nifty/chain').json()['full_chain_fresh'] is False
    assert client.quote.call_count == calls


def test_live_quality_and_logs_report_component_freshness(service, caplog):
    options, client, clock = service
    quote = client.quote.side_effect
    client.quote.side_effect = lambda symbols: quote(symbols[1:])
    with caplog.at_level(logging.INFO, logger='market_app'):
        options.cycle(force=True)
    records = [r for r in caplog.records if getattr(r, 'event', '') == 'options_chain_health']
    assert {r.index for r in records} == {'NIFTY', 'BANKNIFTY'}
    payload = json.loads(JsonFormatter().format(records[0]))
    assert payload['non_fresh_contracts'] == 1 and 'missing_contracts' not in payload
    for key in ('near_atm_expected', 'near_atm_fresh', 'atm_ce_fresh', 'atm_pe_fresh',
                'atm_ce_liquidity', 'atm_pe_liquidity', 'positioning_available_weight', 'atm_available_weight',
                'walls_available_weight', 'pcr_available_weight', 'options_total_available_weight', 'full_chain_fresh'):
        assert key in payload
    records[0].access_token = records[0].login_url = records[0].msg = 'SECRET'
    assert 'SECRET' not in JsonFormatter().format(records[0])
    summary, _ = options.response('NIFTY')
    stream = Mock()
    stream.state.clock.return_value = clock()
    live = LiveSignals(stream, Mock(), options, Mock())
    try:
        live._publish('NIFTY', replace(ready(), options=summary), Submission('NO_TRADE', None, False, ()))
        view = live.current('NIFTY')
        assert not view['data_quality']['stale']
        assert view['data_quality']['options_quality']['full_chain_fresh'] is False
    finally:
        live.close()
