"""End-to-end input scenarios, plus isolated production-policy boundary checks."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

import pytest

from app.analytics.session import IST
from app.options.analytics import OptionsAnalytics
from app.options.state import timestamp
from app.signals import SignalEngine
from app.signals.execution_models import ExecutionConfig
from app.signals.selection import liquid, select_option
from app.trades.models import Closure
from scenarios.market import (FIXTURE, NOW, OPTIONS_CONFIG, SPECS, baseline, build, quote_fresh,
                              timestamp_replay, ordering_replay, candle_replay, future_cache_replay,
                              disconnected_options_replay, decision_report)
from scenarios.trades import trade_report, trade_session

FRESHNESS = 'Critical data stale or freshness unavailable'
COVERAGE = 'Insufficient options-chain coverage'


def result(name):
    return SignalEngine().decide(build(name).snapshot)


def test_original_response_preserved_with_no_repair_or_missing_field_invention():
    original = baseline()
    canonical = json.dumps(original, sort_keys=True, separators=(',', ':')).encode()
    assert sha256(canonical).hexdigest() == '4308cc8df41efe967a5180de57428ea17059ba089007839e1fac2667a61b7416'
    assert original['futures'] is None and original['spot'] == 22834.95
    assert original['coverage']['received_contracts'] == 0
    assert original['atm_ce']['timestamp'] == '2026-09-28T10:27:00+00:00'
    assert build('baseline').snapshot.options == original
    decision = result('baseline')
    assert decision.decision == 'NO_TRADE'
    assert not decision.data_quality['critical_values_present']
    assert COVERAGE in decision.data_quality['blocking_reasons']


def test_scenarios_deep_copy_nested_inputs_and_leave_fixture_unchanged():
    before = FIXTURE.read_bytes()
    one = build('healthy')
    original = deepcopy(one.snapshot.options)
    one.snapshot.options['atm_ce']['depth']['buy'][0]['quantity'] = -999
    one.rows[0]['depth']['buy'][0]['quantity'] = -888
    two = build('healthy')
    assert two.snapshot.options == original
    assert two.rows[0]['depth']['buy'][0]['quantity'] == 100
    assert FIXTURE.read_bytes() == before


@pytest.mark.parametrize('name,direction', [('healthy', 'CALL'), ('qualified_call', 'CALL'), ('qualified_put', 'PUT')])
def test_qualified_cases_are_computed_through_production_categories(name, direction):
    scenario = build(name)
    decision = SignalEngine().decide(scenario.snapshot)
    assert decision.decision == direction and decision.confidence == 90
    assert len(decision.aligned_categories) == 4
    assert abs(decision.bullish_score-decision.bearish_score) >= 15
    assert all(decision.data_quality[k] for k in ('structure_fresh', 'options_fresh', 'breadth_fresh', 'vix_fresh'))
    assert decision.data_quality['blocking_reasons'] == ()
    assert scenario.snapshot.options['coverage']['received_contracts'] == 276
    for key in ('pcr_oi', 'pcr_volume', 'max_pain', 'call_oi_wall', 'put_oi_wall'):
        assert scenario.snapshot.options[key] is not None
    for key in ('atm_ce', 'atm_pe'):
        assert scenario.snapshot.options[key]['liquidity_state'] == 'LIQUID'
        assert scenario.snapshot.options[key]['stale'] is False
    # End-to-end chain summarization must agree with the healthy summary evidence.
    expected = OptionsAnalytics.summarize(scenario.snapshot.index_name,
        datetime.fromisoformat(scenario.snapshot.options['selected_expiry']).date(), scenario.rows,
        scenario.snapshot.structure['spot'], scenario.snapshot.structure['future'], 276,
        NOW, NOW.isoformat(), NOW.isoformat(), OPTIONS_CONFIG)
    for key in ('pcr_oi', 'pcr_volume', 'call_oi_wall', 'put_oi_wall', 'stale', 'coverage'):
        assert expected[key] == scenario.snapshot.options[key]


@pytest.mark.parametrize('name,expected,received,blocked', [
    ('nifty_99_percent', 276, 274, False), ('coverage_above_95', 276, 264, False),
    ('coverage_below_95', 276, 261, True), ('banknifty_low_coverage', 312, 272, True)])
def test_coverage_gate_and_full_chain_freshness_are_distinct(name, expected, received, blocked):
    scenario = build(name)
    decision = SignalEngine().decide(scenario.snapshot)
    q = decision.data_quality
    assert scenario.snapshot.options['coverage']['received_contracts'] == received
    assert q['option_coverage_percent'] == pytest.approx(100*received/expected)
    assert (COVERAGE in q['blocking_reasons']) == blocked
    assert q['options_fresh'] is False and FRESHNESS in q['blocking_reasons']
    assert decision.decision == 'NO_TRADE'
    assert decision.category_scores['options_positioning'].available_weight == 0
    assert scenario.snapshot.options['pcr_oi'] is None  # No partial-chain authoritative analytics.


@pytest.mark.parametrize('name,key', [('stale_structure', 'structure_fresh'), ('stale_vix', 'vix_fresh')])
def test_stale_critical_sources_fail_closed(name, key):
    decision = result(name)
    assert decision.data_quality[key] is False
    assert FRESHNESS in decision.data_quality['blocking_reasons']
    assert decision.decision == 'NO_TRADE'


@pytest.mark.parametrize('name,allowed', [('breadth_89_9', False), ('breadth_90', True),
                                       ('breadth_95', True), ('breadth_not_full', False)])
def test_exact_breadth_coverage_and_full_index_gate(name, allowed):
    decision = result(name)
    assert (decision.decision == 'CALL') == allowed
    assert ('Insufficient full-index breadth coverage' in decision.data_quality['blocking_reasons']) != allowed


@pytest.mark.parametrize('name,direction,score,aligned', [
    ('bearish_51', 'NO_TRADE', 51, 3), ('score_exactly_70', 'CALL', 70, 4),
    ('three_aligned', 'NO_TRADE', 75, 3), ('four_aligned', 'CALL', 90, 4)])
def test_score_and_alignment_boundaries_without_mocked_scores(name, direction, score, aligned):
    decision = result(name)
    assert decision.decision == direction
    assert max(decision.bullish_score, decision.bearish_score) == score
    assert min(decision.bullish_score, decision.bearish_score) == 0
    assert len(decision.aligned_categories) == aligned
    assert ('Winning score below minimum' in decision.data_quality['blocking_reasons']) == (score < 70)
    assert ('Fewer than minimum aligned categories' in decision.data_quality['blocking_reasons']) == (aligned < 4)


@pytest.mark.parametrize('name,difference,blocked', [('separation_below_15', 12, True),
    ('separation_exactly_15', 15, False), ('separation_above_15', 18, False)])
def test_separation_gate_with_naturally_scored_inputs(name, difference, blocked):
    decision = result(name)
    assert abs(decision.bullish_score-decision.bearish_score) == difference
    assert ('Directional scores too close' in decision.data_quality['blocking_reasons']) == blocked
    # Score/alignment still block: never fabricate a >=70 winner + small separation.
    assert decision.decision == 'NO_TRADE'
    assert 'Winning score below minimum' in decision.data_quality['blocking_reasons']


def test_meaningful_futures_contradiction_blocks_otherwise_high_score():
    decision = result('futures_contradiction')
    assert (decision.bullish_score, decision.bearish_score) == (75, 15)
    assert decision.category_scores['futures_structure'].direction == 'bearish'
    assert 'Major category contradiction: futures_structure' in decision.data_quality['blocking_reasons']
    assert decision.decision == 'NO_TRADE'


@pytest.mark.parametrize('name', ['missing_spot', 'missing_future', 'missing_vix'])
def test_missing_critical_values_fail_closed(name):
    decision = result(name)
    assert decision.data_quality['critical_values_present'] is False
    assert 'Critical values unavailable' in decision.data_quality['blocking_reasons']
    assert decision.decision == 'NO_TRADE'


@pytest.mark.parametrize('name,direction,key', [('stale_atm_ce', 'CALL', 'atm_ce'), ('stale_atm_pe', 'PUT', 'atm_pe')])
def test_stale_atm_contributes_no_live_evidence_and_cannot_confirm_trade(name, direction, key):
    scenario = build(name)
    quote = scenario.snapshot.options[key]
    assert not quote_fresh(quote) and quote['stale'] and quote['positioning'] == 'UNAVAILABLE'
    decision = SignalEngine().decide(scenario.snapshot)
    # Other fresh evidence may qualify; stale ATM behavior's 4.5 points are absent.
    assert max(decision.bullish_score, decision.bearish_score) == 85.5
    assert decision.category_scores['options_positioning'].available_weight == 25.5
    assert not liquid(quote, NOW, ExecutionConfig())
    selected = select_option(scenario.snapshot, direction, scenario.rows, ExecutionConfig())
    assert selected is None or selected.instrument_token != quote['instrument_token']
    with trade_session(direction, scenario) as h:
        setup = h.service.setup('NIFTY')
        assert not setup['can_confirm'] and setup['option_ltp'] is None
        with pytest.raises(ValueError, match='fresh'):
            h.confirm()
        assert h.service.journal.listing()['total'] == 0


def test_disconnected_snapshot_cache_and_manual_entry_all_fail_closed():
    assert result('disconnected').decision == 'NO_TRADE'
    replay = disconnected_options_replay()
    assert replay['before_coverage']['received_contracts'] == 276
    assert replay['after_coverage']['received_contracts'] == 0
    assert replay['stale'] and replay['all_quotes_stale']
    assert replay['atm_positioning'] == 'UNAVAILABLE'
    with trade_session() as h:
        h.state.connected = False
        assert not h.service.setup('NIFTY')['can_confirm']
        assert h.service.setup('NIFTY')['underlying_current'] is None
        with pytest.raises(ValueError, match='fresh'):
            h.confirm()


@pytest.mark.parametrize('tz', [IST, timezone.utc])
def test_real_rest_wall_clock_bug_and_aware_quotes(tz):
    replay = timestamp_replay()
    assert replay['corrected']['timestamp'] == '2026-09-28T10:27:00+05:30'
    assert replay['old']['timestamp'] == baseline()['atm_ce']['timestamp']
    assert quote_fresh(replay['corrected'], NOW.astimezone(tz))
    assert not quote_fresh(replay['old'], NOW.astimezone(tz))
    aware = datetime(2026, 9, 28, 4, 57, tzinfo=timezone.utc).astimezone(tz)
    assert timestamp(aware, 'rest') is aware


def test_naive_rest_normalization_never_asks_for_host_timezone():
    class HostSensitive(datetime):
        def astimezone(self, tz=None):
            raise AssertionError('REST normalization must not consult the host-local timezone')
    assert timestamp(HostSensitive(2026, 9, 28, 10, 27), 'rest').utcoffset() == timedelta(hours=5, minutes=30)


@pytest.mark.parametrize('seconds,expected', [(5, True), (5.001, False), (-60, True), (-60.001, False)])
def test_options_exchange_age_boundaries(seconds, expected):
    quote = dict(timestamp_replay()['corrected'], received_at=NOW.isoformat(),
                 timestamp=(NOW+timedelta(seconds=seconds)).isoformat())
    assert quote_fresh(quote) == expected


def test_older_option_event_cannot_overwrite_newer_and_future_tolerance_is_bounded():
    replay = ordering_replay()
    assert replay['retained_ltp'] == 120 and replay['retained_timestamp'] == NOW.isoformat()
    assert replay['within_tolerance'] and not replay['beyond_tolerance']


@pytest.mark.parametrize('name', ['active_trade_stale_stop', 'active_trade_stale_targets'])
def test_stale_trade_data_never_emits_stop_or_target(name):
    replay = trade_report(name)
    assert replay['status'] == 'ACTIVE' and replay['monitoring_status'] == 'PAUSED'
    assert replay['events'] == [] and replay['hits'] == []
    assert replay['persisted_on_reopen']


@pytest.mark.parametrize('direction', ['CALL', 'PUT'])
def test_stop_is_persisted_once_and_remains_acknowledgeable(direction):
    with trade_session(direction) as h:
        trade = h.confirm()
        for _ in range(3):
            h.observe(trade['underlying_stop']-h.sign)
        events = h.service.journal.events(trade['trade_id'])
        assert len(events) == 1 and events[0]['kind'] == 'STOP_HIT'
        h.service.acknowledge(trade['trade_id'], events[0]['event_id'])
        assert h.service.journal.events(trade['trade_id'])[0]['acknowledged_at'] == h.state.now.isoformat()
        assert h.service.journal.get(trade['trade_id'])['status'] == 'STOP_HIT'
        h.service.close_trade(trade['trade_id'], Closure(actual_exit_premium=None))
        h.observe(trade['underlying_stop']-h.sign)
        assert h.service.journal.get(trade['trade_id'])['status'] == 'USER_CLOSED'
        assert len(h.service.journal.events(trade['trade_id'])) == 1


@pytest.mark.parametrize('name', ['targets', 'targets_put'])
def test_both_targets_persist_once_without_closing(name):
    replay = trade_report(name)
    assert replay['status'] == 'T2_HIT' and replay['closed_at'] is None
    assert replay['hits'] == ['T1_HIT', 'T2_HIT']
    assert [e['kind'] for e in replay['events']] == ['T1_HIT', 'T2_HIT']
    assert replay['persisted_on_reopen']


@pytest.mark.parametrize('name,monitor', [('trade_out_of_order', 'LIVE'), ('trade_future_timestamp', 'PAUSED')])
def test_trade_received_order_and_stricter_future_guard(name, monitor):
    replay = trade_report(name)
    assert replay['status'] == 'ACTIVE' and replay['events'] == []
    assert replay['monitoring_status'] == monitor


@pytest.mark.parametrize('interval', ['5m', '15m'])
def test_partial_and_forming_candles_do_not_change_production_emas(interval):
    replay = candle_replay(interval)
    assert replay['clean'] == replay['with_partial_and_forming']
    assert replay['clean']['ema20'] == 110.5
    decision = result('partial_candles')
    assert decision.category_scores['price_trend'].bullish_points == 24
    assert not any('5m EMA' in e or '15m EMA' in e for e in decision.category_scores['price_trend'].evidence)
    assert decision.decision == 'CALL'  # Partial intervals reduce evidence; they do not impose a blanket veto.


def test_ui_adapter_distinguishes_qualification_from_an_active_signal():
    qualified = decision_report('qualified_call')
    assert qualified['decision'] == 'CALL'
    assert qualified['ui_signal']['current_qualification']['decision'] == 'CALL'
    assert qualified['ui_signal']['decision'] == 'NO_TRADE' and qualified['ui_signal']['record'] is None
    stale = decision_report('nifty_99_percent')['ui_signal']
    assert stale['data_quality']['stale'] and FRESHNESS in stale['data_quality']['blocking_reasons']


@pytest.mark.xfail(strict=True, raises=AssertionError, reason='PROD-1: monitor orders receipts, not exchange events; see simulator findings')
def test_regression_older_exchange_event_must_not_trigger_a_new_stop():
    replay = trade_report('trade_exchange_out_of_order')
    assert replay['events'] == []


@pytest.mark.xfail(strict=True, raises=AssertionError, reason='PROD-2: future OptionBook event blocks subsequent valid quotes; see simulator findings')
def test_regression_future_quote_must_not_poison_the_option_cache():
    replay = future_cache_replay()
    assert replay['valid_later_quote_retained'] is True
