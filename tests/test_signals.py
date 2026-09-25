from copy import deepcopy
from dataclasses import asdict, replace
import json

import pytest

from app.signals import SignalConfig, SignalEngine, SignalInput


def setup(index="NIFTY", bullish=True):
    spot = 110 if bullish else 90
    structure = dict(spot=spot, future=spot+1, futures_vwap=100, futures_basis=1,
                     opening_range_high=105, opening_range_low=95,
                     previous_day_high=106, previous_day_low=94, previous_day_close=100,
                     day_high=111, day_low=89, stale=False,
                     future_change_percent=1 if bullish else -1,
                     spot_change_percent=1 if bullish else -1)
    for interval in ("5m", "15m", "30m"):
        structure[interval] = {"ema9": spot, "ema20": 100}
    side = "put" if bullish else "call"
    options = dict(spot=spot, call_oi_wall=105, put_oi_wall=95,
                   pcr_oi=1.5 if bullish else .5, pcr_volume=1.5 if bullish else .5,
                   near_atm_pcr_oi=1.5 if bullish else .5, max_pain=100, stale=False)
    options[side+"_writing_zones"] = [{"strike": 100, "oi_change_session": 1000}]
    options["highest_"+side+"_oi_addition"] = {"strike": 100, "value": 1000}
    for kind in ("ce", "pe"):
        options["atm_"+kind] = {"positioning": "LONG_BUILDUP" if (kind == "ce") == bullish else "SHORT_BUILDUP",
                              "liquidity_state": "LIQUID", "iv": .2, "stale": False}
    return SignalInput(index, "2026-09-25T10:00:00+05:30", structure, options,
                       {"level": 18, "change_percent": 2})


@pytest.mark.parametrize("index", ["NIFTY", "BANKNIFTY"])
@pytest.mark.parametrize("bullish", [True, False])
def test_directional_setup_and_budgets(index, bullish):
    result = SignalEngine().score(setup(index, bullish))
    expected = "bullish" if bullish else "bearish"
    for name in ("price_trend", "options_positioning", "futures_structure"):
        assert result.categories[name].direction == expected
    assert result.categories['volatility'].direction == 'neutral'
    assert result.categories['breadth_constituents'].direction == 'unavailable'
    assert result.available_weight == 85
    assert (result.bullish_points, result.bearish_points) == ((75, 0) if bullish else (0, 75))
    for category in result.categories.values():
        assert category.bullish_points + category.bearish_points <= category.available_weight


def test_neutral_setup():
    snapshot = setup()
    data = snapshot.structure
    data.update(spot=100, future=100, future_change_percent=0, spot_change_percent=0)
    for interval in ('5m', '15m', '30m'):
        data[interval] = {'ema9': 100, 'ema20': 100}
    opts = dict(spot=100, call_oi_wall=105, put_oi_wall=95, pcr_oi=1,
                pcr_volume=1, near_atm_pcr_oi=1)
    result = SignalEngine().score(replace(snapshot, options=opts))
    assert all(c.direction == 'neutral' for name, c in result.categories.items() if name != 'breadth_constituents')
    assert result.bullish_points == result.bearish_points == 0


def test_conflicts_are_explicit_without_erasing_opposition():
    snapshot = setup()
    snapshot.structure['15m'] = {'ema9': 90, 'ema20': 100}
    snapshot.structure['future_change_percent'] = -1
    snapshot.options['call_writing_zones'] = [{'strike': 105, 'oi_change_session': 1000}]
    snapshot.options['pcr_oi'] = .5
    result = SignalEngine().score(snapshot)
    for name in ('price_trend', 'options_positioning', 'futures_structure'):
        assert result.categories[name].contradictions
    assert result.categories['price_trend'].bearish_points > 0
    assert result.categories['options_positioning'].bearish_points > 0


def test_missing_data_does_not_become_neutral_or_renormalize():
    engine = SignalEngine()
    result = engine.score(SignalInput('NIFTY', 'replay-1'))
    assert all(c.direction == 'unavailable' for c in result.categories.values())
    assert result.available_weight == result.bullish_points == result.bearish_points == 0
    partial = engine.score(SignalInput('NIFTY', 'replay-1', {'spot': 110, 'previous_day_close': 100}))
    assert partial.available_weight == partial.bullish_points == 4.5


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), float('-inf'), None])
def test_nonfinite_input_cannot_produce_points_or_invalid_json(bad):
    def scrub(value):
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return bad if isinstance(value, (float, int)) and not isinstance(value, bool) else value
    sample = setup()
    # Remove label-only positioning as it is independent supplied evidence.
    sample.options.pop('atm_ce')
    sample.options.pop('atm_pe')
    result = SignalEngine().score(replace(sample, structure=scrub(sample.structure),
                                         options=scrub(sample.options), volatility=scrub(sample.volatility)))
    assert result.available_weight == 0
    json.dumps(asdict(result), allow_nan=False)


def test_replay_is_deterministic_and_input_unchanged():
    sample = setup()
    before = deepcopy(sample)
    engine = SignalEngine()
    first = engine.score(sample)
    engine.score(setup(bullish=False))
    assert first == engine.score(sample) == SignalEngine().score(sample)
    assert sample == before
    assert json.loads(json.dumps(asdict(first), allow_nan=False))['version'] == '5.3.1'


def test_ema_budget_is_shared_and_partial_coverage_is_explicit():
    engine = SignalEngine()
    data = {'5m': {'ema9': 110, 'ema20': 100}}
    first = engine.price(data)
    data.update({'15m': data['5m'], '30m': data['5m']})
    full = engine.price(data)
    assert first.bullish_points == first.available_weight == 3
    assert full.bullish_points == full.available_weight == 9


def test_pcr_basis_and_max_pain_cannot_determine_direction():
    result = SignalEngine().score(SignalInput('NIFTY', 'replay',
        {'futures_basis': 50}, {'pcr_oi': 3, 'pcr_volume': 3, 'near_atm_pcr_oi': 3, 'max_pain': 25000}))
    assert result.bullish_points == result.bearish_points == 0
    assert result.categories['options_positioning'].direction == 'neutral'
    assert result.categories['futures_structure'].direction == 'unavailable'


@pytest.mark.parametrize('level,change', [(10,-5), (18,0), (40,10)])
def test_vix_is_risk_context_not_a_direction(level, change):
    category = SignalEngine().volatility({'level': level, 'change_percent': change})
    assert category.direction == 'neutral'
    assert category.available_weight == 10
    assert category.bullish_points == category.bearish_points == 0
    assert len(category.evidence) == 3


def test_stale_sources_are_unavailable():
    sample = setup()
    for source in (sample.structure, sample.options, sample.volatility):
        source['stale'] = True
    assert SignalEngine().score(sample).available_weight == 0


def test_unwinding_writing_and_liquidity():
    engine = SignalEngine()
    assert engine.options({'call_unwinding_zones': [{'strike': 100, 'value': -500}]}).direction == 'bullish'
    assert engine.options({'put_unwinding_zones': [{'strike': 100, 'value': -500}]}).direction == 'bearish'
    assert engine.options({'highest_put_oi_addition': {'strike': 100, 'value': 500}}).direction == 'unavailable'
    row = {'positioning': 'LONG_BUILDUP', 'liquidity_state': 'POOR'}
    assert engine.options({'atm_ce': row}).direction == 'unavailable'
    row['liquidity_state'] = 'LIQUID'
    assert engine.options({'atm_ce': row}).direction == 'bullish'
    row['iv'] = float('inf')
    assert engine.options({'atm_ce': row}).direction == 'unavailable'


@pytest.mark.parametrize('kwargs', [dict(pcr_bullish=.5), dict(price_weight=float('inf')),
    dict(price_weight=40), dict(price_parts=(.5,.5)), dict(day_upper_fraction=.1), dict(direction_margin=1)])
def test_config_rejects_invalid_thresholds_and_weights(kwargs):
    with pytest.raises(ValueError):
        SignalConfig(**kwargs)


def test_thresholds_and_weights_are_configurable():
    config = SignalConfig(price_deadband_percent=20, price_weight=20, options_weight=40)
    engine = SignalEngine(config)
    result = engine.score(SignalInput('NIFTY', 'replay', {'spot': 110, 'previous_day_close': 100}))
    assert result.categories['price_trend'].direction == 'neutral'
    assert result.available_weight == 3
    assert result.config == config


def test_unsupported_index_rejected():
    with pytest.raises(ValueError):
        SignalInput('OTHER', 'replay')
