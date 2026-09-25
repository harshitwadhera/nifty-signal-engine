from dataclasses import asdict, replace
import json
from unittest.mock import Mock

import pytest

from app.signals import SignalEngine, SignalConfig, SignalInput
from test_signals import setup


def ready(bullish=True, index="NIFTY"):
    sample = setup(index, bullish)
    for source in (sample.structure, sample.volatility):
        source.update(as_of=sample.as_of, stale=False)
    sample.options.update(last_full_chain_refresh_at=sample.as_of,
                          coverage={"expected_contracts": 100, "received_contracts": 100})
    return replace(sample, breadth={"index": index, "as_of": sample.as_of, "stale": False,
        "coverage_percent": 100, "full_index": True, "weighting": "unweighted",
        "percent_positive": 80 if bullish else 20, "percent_negative": 20 if bullish else 80,
        "percent_above_5m_ema20": 80 if bullish else 20, "percent_above_15m_ema20": 80 if bullish else 20})


@pytest.mark.parametrize('bullish,expected', [(True, 'CALL'), (False, 'PUT')])
@pytest.mark.parametrize('index', ['NIFTY', 'BANKNIFTY'])
def test_directional_decision(bullish, expected, index):
    sample = ready(bullish, index)
    result = SignalEngine().decide(sample)
    assert result.decision == expected
    assert len(result.aligned_categories) == 4
    assert result.confidence == 90
    assert result == SignalEngine().decide(sample)
    json.dumps(asdict(result), allow_nan=False)


def test_default_no_trade():
    result = SignalEngine().decide(SignalInput('NIFTY', 'missing'))
    assert result.decision == 'NO_TRADE' and result.confidence == 0


def test_only_three_categories_aligned():
    sample = ready()
    sample.breadth.update(percent_positive=50, percent_negative=50,
                         percent_above_5m_ema20=50, percent_above_15m_ema20=50)
    result = SignalEngine().decide(sample)
    assert result.decision == 'NO_TRADE'
    assert len(result.aligned_categories) == 3
    assert 'Fewer than minimum aligned categories' in result.data_quality['blocking_reasons']


def test_score_below_70_with_four_categories():
    sample = ready()
    for field in ('opening_range_high', 'previous_day_high', 'previous_day_close'):
        sample.structure.pop(field)
    sample.structure.pop('15m')
    sample.structure.pop('30m')
    result = SignalEngine().decide(sample)
    assert len(result.aligned_categories) == 4
    assert result.bullish_score < 70
    assert result.decision == 'NO_TRADE'


def test_separation_gate_independent_of_other_gates():
    # Isolate decision policy with a supplied category result; normal setup tests
    # above exercise scoring end-to-end. Small separation is arithmetically
    # incompatible with a 70-point winner on a bounded 100-point budget today.
    sample = ready()
    engine = SignalEngine()
    scores = replace(engine.score(sample), bullish_points=75, bearish_points=65)
    engine.score = Mock(return_value=scores)
    result = engine.decide(sample)
    assert result.decision == 'NO_TRADE'
    assert 'Directional scores too close' in result.data_quality['blocking_reasons']


@pytest.mark.parametrize('source', ['structure', 'options', 'volatility', 'breadth'])
def test_stale_data(source):
    sample = ready()
    getattr(sample, source)['stale'] = True
    assert SignalEngine().decide(sample).decision == 'NO_TRADE'


@pytest.mark.parametrize('stamp', [None, 'bad', '2026-09-25T09:58:00+05:30', '2026-09-25T10:01:00+05:30'])
def test_timestamps_checked_against_replay_clock(stamp):
    sample = ready()
    sample.structure['as_of'] = stamp
    assert SignalEngine().decide(sample).decision == 'NO_TRADE'


@pytest.mark.parametrize('received', [94, None, float('nan'), float('inf'), 101])
def test_insufficient_option_coverage(received):
    sample = ready()
    sample.options['coverage']['received_contracts'] = received
    result = SignalEngine().decide(sample)
    assert result.decision == 'NO_TRADE'
    assert 'Insufficient options-chain coverage' in result.data_quality['blocking_reasons']
    json.dumps(asdict(result), allow_nan=False)


def test_major_conflicting_evidence_blocks():
    sample = ready()
    sample.options['call_writing_zones'] = [{'strike': 105, 'oi_change_session': 100}]
    sample.options['atm_pe']['positioning'] = 'LONG_BUILDUP'
    result = SignalEngine().decide(sample)
    assert result.bullish_score >= 70
    assert len(result.aligned_categories) == 4
    assert result.decision == 'NO_TRADE'
    assert any('Major category contradiction' in reason for reason in result.data_quality['blocking_reasons'])
    assert result.contradictions


def test_partial_proxy_and_low_breadth_coverage_block():
    sample = ready()
    sample.breadth['full_index'] = False
    assert SignalEngine().decide(sample).decision == 'NO_TRADE'
    sample.breadth.update(full_index=True, coverage_percent=80)
    assert SignalEngine().decide(sample).decision == 'NO_TRADE'


def test_all_unchanged_is_neutral():
    data = dict(coverage_percent=100, percent_positive=0, percent_negative=0, stale=False)
    assert SignalEngine().breadth(data).direction == 'neutral'


def test_decision_config_validation():
    for args in ({'minimum_aligned': 3.5}, {'minimum_score': float('nan')}, {'minimum_option_coverage': 101}):
        with pytest.raises(ValueError):
            SignalConfig(**args)


def test_futures_divergence_is_a_major_contradiction():
    sample = ready()
    sample.structure['future_change_percent'] = -1
    result = SignalEngine().decide(sample)
    assert result.decision == 'NO_TRADE'
    assert 'Major category contradiction: futures_structure' in result.contradictions


def test_live_adapter_builds_snapshot_without_api_or_another_feed():
    from app.signals.live import LiveSignals
    from datetime import datetime
    sample = ready()
    stream, structure, options, breadth = Mock(), Mock(), Mock(), Mock()
    stream.state.clock.return_value = datetime.fromisoformat(sample.as_of)
    stream.live.return_value = {'instruments': [
        dict(trading_symbol='NIFTY 50', last_price=110, ohlc={'close': 100}, stale=False),
        dict(trading_symbol='NIFTYFUT', alias='NIFTY_FUT', last_price=111, ohlc={'close': 100}, stale=False),
        dict(trading_symbol='INDIA VIX', last_price=18, ohlc={'close': 17}, stale=False, last_tick_received_at=sample.as_of)]}
    structure.structure.return_value = {'as_of': sample.as_of, 'nifty': sample.structure}
    options.response.return_value = (sample.options, [])
    breadth.snapshot.return_value = sample.breadth
    live = LiveSignals(stream, structure, options, breadth)
    assert live.snapshot('NIFTY').structure['future_change_percent'] == pytest.approx(11)
    assert live.decision('NIFTY').decision == 'CALL'
    stream.start.assert_not_called()
    stream.live.return_value['instruments'][0]['stale'] = True
    assert live.decision('NIFTY').decision == 'NO_TRADE'
