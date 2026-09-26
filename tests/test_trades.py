from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import create_app
from app.config import Settings
from app.signals.execution_models import instant
from app.trades.models import Confirmation, Closure
from app.trades.monitor import observe
from app.trades.service import TradeService
from app.trades.storage import TradeJournal

NOW = instant('2026-09-25T10:05:00+05:30')


def observation(value=24900, age=0, index='NIFTY', now=NOW):
    return {'trading_symbol': 'NIFTY 50' if index == 'NIFTY' else 'NIFTY BANK',
            'last_price': value, 'stale': False, 'last_tick_received_at': (now-timedelta(seconds=age)).isoformat()}


def signal(index='NIFTY', direction='CALL'):
    symbol = 'NIFTY 50' if index == 'NIFTY' else 'NIFTY BANK'
    return {'signal_id': index+direction, 'state': 'CONFIRMED', 'updated_at': (NOW-timedelta(seconds=60)).isoformat(),
        'plan': {'index_name': index, 'direction': direction,
                 'entry_trigger': {'instrument': symbol, 'type': 'breakout', 'level':24900, 'confirmation':'5m_close_above'},
                 'invalidation': {'level':24800 if direction == 'CALL' else 24950},
                 'target1': {'level':25000 if direction == 'CALL' else 24800},
                 'target2': {'level':25100 if direction == 'CALL' else 24700},
                 't1_rr':1.5, 't2_rr':2,
                 'option': {'trading_symbol': index+'TEST', 'instrument_token': 123, 'expiry':'2026-09-29',
                            'option_type':'CE' if direction == 'CALL' else 'PE', 'strike':24900}}}


@pytest.fixture
def service(tmp_path):
    record = signal()
    signals, stream, options = Mock(), Mock(), Mock()
    signals.detail.side_effect = lambda sid: {'record': deepcopy(record)} if sid == record['signal_id'] else None
    signals.current.side_effect = lambda index: {'index':index,'record':deepcopy(record), 'data_quality':{'stale':False}}
    stream.live.return_value = {'websocket_status':'connected', 'instruments':[observation()]}
    options.response.return_value = ({},[{**record['plan']['option'], 'lot_size':65, 'ltp':101,
        'stale':False, 'received_at':NOW.isoformat(), 'timestamp':NOW.isoformat()}])
    result = TradeService(stream, signals, options, tmp_path/'trades.sqlite3', clock=lambda: NOW)
    result.test_record = record
    yield result
    result.close()


def body(**changes):
    return Confirmation(**(dict(quantity=65, lots=1, actual_entry_premium=102.5, underlying_entry=24900, opened_at=NOW.isoformat()) | changes))


def entered(service):
    return service.confirm(service.test_record['signal_id'], body())


def test_explicit_confirmation_only_quantity_and_premium(service):
    assert service.setup('NIFTY')['can_confirm']
    service.poll()
    assert service.active()['total'] == 0
    trade = entered(service)
    assert trade['actual_entry_premium'] == 102.5
    assert trade['quantity'] == trade['lots']*trade['lot_size'] == 65
    assert service.journal.get(trade['trade_id']) == trade
    with pytest.raises(ValueError): entered(service)


def test_candidate_quantity_time_and_coordinate_rejected(service):
    for invalid in (body(quantity=64), body(opened_at=NOW+timedelta(seconds=1)), body(underlying_entry=24700)):
        with pytest.raises(ValueError): service.confirm('NIFTYCALL', invalid)
    service.test_record['state'] = 'CANDIDATE'
    assert service.setup('NIFTY')['setup_state'] == 'WAITING'
    with pytest.raises(ValueError): entered(service)
    service.test_record['state'] = 'CONFIRMED'
    service.test_record['plan']['entry_trigger']['instrument'] = 'NIFTY_FUT'
    assert not service.setup('NIFTY')['can_confirm']
    with pytest.raises(ValueError): entered(service)


@pytest.mark.parametrize('field,value', [('actual_entry_premium',float('nan')), ('underlying_entry',float('inf')),
    ('quantity',True), ('quantity',0), ('lots',1.5), ('opened_at','2026-09-25T10:05:00')])
def test_input_validation(field, value):
    with pytest.raises(ValidationError): body(**{field:value})


@pytest.mark.parametrize('direction,prices,stop', [('CALL',[24840,24810,24799],24800),('PUT',[24920,24940,24951],24950)])
def test_stop_once(service,direction,prices,stop):
    trade = entered(service); trade.update(direction=direction,underlying_stop=stop,target1=None,target2=None)
    events = []
    for step,p in enumerate(prices+[prices[-1]],1):
        now = NOW+timedelta(seconds=step)
        trade,new = observe(trade,observation(p,now=now),True,now); events.extend(new)
        assert trade['status'] == ('ACTIVE' if step < 3 else 'STOP_HIT')
    assert len(events) == 1 and events[0]['kind'] == 'STOP_HIT'


@pytest.mark.parametrize('direction,prices,targets,stop', [('CALL',[25001,25101],(25000,25100),24800),('PUT',[24799,24699],(24800,24700),24950)])
def test_targets_do_not_close_and_stop_remains_active(service,direction,prices,targets,stop):
    trade = entered(service); trade.update(direction=direction,target1=targets[0],target2=targets[1],underlying_stop=stop)
    for step,p in enumerate(prices,1):
        now=NOW+timedelta(seconds=step)
        trade,events=observe(trade,observation(p,now=now),True,now)
        assert trade['status']==f'T{step}_HIT' and len(events)==1 and trade['closed_at'] is None
    now+=timedelta(seconds=1)
    trade,events=observe(trade,observation(stop,now=now),True,now)
    assert trade['status']=='STOP_HIT' and len(events)==1


@pytest.mark.parametrize('fault',['old','flag','disconnect','missing','future','exchange','nan'])
def test_stale_or_disconnected_cannot_stop_then_fresh_resumes(service,fault):
    trade=entered(service); now=NOW+timedelta(seconds=10); row=observation(24700,now=now)
    connected=True
    if fault=='old': row['last_tick_received_at']=(now-timedelta(seconds=31)).isoformat()
    if fault=='flag': row['stale']=True
    if fault=='disconnect': connected=False
    if fault=='missing': row.pop('last_tick_received_at')
    if fault=='future': row['last_tick_received_at']=(now+timedelta(seconds=1)).isoformat()
    if fault=='exchange': row['last_exchange_timestamp']=(now-timedelta(seconds=31)).isoformat()
    if fault=='nan': row['last_price']=float('nan')
    paused,events=observe(trade,row,connected,now)
    assert paused['status']=='ACTIVE' and paused['monitoring_status']=='PAUSED' and not events
    assert paused['metadata']['data_gap_count']==1
    resumed,events=observe(paused,observation(24790,now=now),True,now)
    assert resumed['status']=='STOP_HIT' and len(events)==1


def test_restart_acknowledgement_close_and_history(service,tmp_path):
    trade=entered(service)
    path=tmp_path/'trades.sqlite3'
    reopened=TradeJournal(path)
    assert reopened.get(trade['trade_id'])['status']=='ACTIVE'; reopened.close()
    now=NOW+timedelta(seconds=1)
    service.process(observation(24790,now=now),True,now)
    event=service.journal.events(trade['trade_id'])[0]
    service.acknowledge(trade['trade_id'],event['event_id'])
    reopened=TradeJournal(path)
    recovered=reopened.get(trade['trade_id'])
    assert reopened.events(trade['trade_id'])[0]['acknowledged_at']
    _,events=observe(recovered,observation(24780,now=now+timedelta(seconds=1)),True,now+timedelta(seconds=1))
    assert not events; reopened.close()
    closed=service.close_trade(trade['trade_id'],Closure(actual_exit_premium=80))
    assert closed['exit_option_price']==80 and closed['closed_at'] and closed['exit_underlying']==24900
    assert not service.active()['items']
    assert service.journal.listing()['items'][0]['status']=='USER_CLOSED'
    with pytest.raises(ValueError): entered(service)


def test_one_open_per_index_including_hit_status(service):
    first=entered(service)
    second=deepcopy(first); second.update(trade_id='second',signal_id='other',status='T1_HIT')
    with pytest.raises(ValueError): service.journal.save(second,create=True)
    second.update(index_name='BANKNIFTY')
    service.journal.save(second,create=True)
    assert service.journal.listing(active=True,limit=2)['total']==2


def test_trade_apis_validate_and_do_not_create_on_get(service):
    service.start=Mock(); service.close=Mock()
    app=create_app(Settings(),stream=Mock(),engine=Mock(),options=Mock(),trades=service)
    with TestClient(app) as client:
        assert client.get('/api/trades/active').json()['total']==0
        assert client.get('/api/trades/setup/nifty').json()['setup_state']=='READY'
        url='/api/trades/NIFTYCALL/confirm'; payload=body().model_dump(mode='json')
        assert client.post(url,json=payload,headers={'Origin':'https://evil.example'}).status_code==403
        assert client.post(url,json={**payload,'lots':0}).status_code==422
        response=client.post(url,json=payload); assert response.status_code==201
        tid=response.json()['trade_id']
        assert client.post(url,json=payload).status_code==409
        assert client.get('/api/trades/history?limit=0').status_code==422
        assert client.post(f'/api/trades/{tid}/close',json={'actual_exit_premium':99}).status_code==200
        assert client.get('/api/trades/active').json()['total']==0
        assert client.get('/api/trades/history?index=nifty').json()['total']==1
    service.journal.close()


def test_development_replay_uses_real_journal_and_monitor(tmp_path):
    from scripts.replay_trades import replay_app
    with TestClient(replay_app(tmp_path/'replay.sqlite3')) as client:
        setup=client.get('/api/trades/setup/nifty').json()
        payload=body(opened_at=setup['server_time']).model_dump(mode='json')
        assert client.post('/api/trades/simulation-only/confirm',json=payload).status_code==201
        for state,monitoring,count in [('ACTIVE','LIVE',0),('ACTIVE','PAUSED',0),('STOP_HIT','LIVE',1)]:
            assert client.post('/api/trades/replay/next',json={}).status_code==200
            trade=client.get('/api/trades/active').json()['items'][0]
            assert (trade['status'],trade['monitoring_status'],len(trade['events']))==(state,monitoring,count)


def test_full_replay_sequence_with_target_then_stop(service):
    trade=entered(service); all_events=[]
    for step,(value,stale) in enumerate([(24920,False),(24870,False),(24790,True),(24840,False),
                                      (25005,False),(25040,False),(24795,False)],1):
        now=NOW+timedelta(seconds=step)
        trade,events=observe(trade,observation(value,age=60 if stale else 0,now=now),True,now)
        all_events.extend(events)
        if stale:
            assert trade['status']=='ACTIVE' and trade['monitoring_status']=='PAUSED' and not events
    assert [e['kind'] for e in all_events]==['T1_HIT','STOP_HIT']
    assert trade['closed_at'] is None


@pytest.mark.parametrize('direction,current,expected', [
    ('CALL',24900,True),('CALL',24801,True),('CALL',24800,False),('CALL',24790,False),
    ('CALL',25000,False),('CALL',25010,False),
    ('PUT',24900,True),('PUT',24999,True),('PUT',25000,False),('PUT',25010,False),
    ('PUT',24800,False),('PUT',24790,False)])
def test_setup_structural_range_boundaries(service,direction,current,expected):
    plan=service.test_record['plan']
    plan.update(direction=direction, invalidation={'level':24800 if direction=='CALL' else 25000},
                target1={'level':25000 if direction=='CALL' else 24800})
    service.stream.live.return_value['instruments']=[observation(current)]
    setup=service.setup('NIFTY')
    assert setup['can_confirm'] is expected
    assert setup['setup_state']==('READY' if expected else 'NO_TRADE')
    if not expected:
        assert setup['reason']=='Underlying has already reached the structural stop or first target; this setup is no longer actionable.'
        with pytest.raises(ValueError): entered(service)


@pytest.mark.parametrize('confirmation',[24800,24790,25000,25010,float('nan'),float('inf')])
def test_invalid_confirmation_coordinate_cannot_show_ready(service,confirmation):
    service.test_record['confirmation_price']=confirmation
    setup=service.setup('NIFTY')
    assert not setup['can_confirm'] and setup['setup_state']=='NO_TRADE'
