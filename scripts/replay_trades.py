"""Development-only localhost UI fixture. Run with python -m scripts.replay_trades.

No Kite client, credentials, live data, or production database is used. The fixture
drives the exact production TradeService/observe logic with a deterministic clock.
"""
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from fastapi.responses import FileResponse
from app.config import Settings
from app.main import create_app
from app.signals.execution_models import instant
from app.trades.service import TradeService


def replay_app(path):
    now = [instant('2026-09-25T10:05:00+05:30')]
    step = [0]
    row = {'trading_symbol':'NIFTY 50', 'last_price':24900, 'stale':False, 'last_tick_received_at':now[0].isoformat()}
    option = {'trading_symbol':'SIMULATED_NIFTY_CE', 'instrument_token':1, 'expiry':'2026-09-29',
              'option_type':'CE', 'strike':24900, 'lot_size':65, 'ltp':100, 'stale':False,
              'received_at':now[0].isoformat(), 'timestamp':now[0].isoformat(), 'spread_percent':.2,'oi':10000,'volume':1000}
    record = {'signal_id':'simulation-only', 'state':'CONFIRMED', 'updated_at':now[0].isoformat(),
              'confirmation_price':24900, 'plan':{'index_name':'NIFTY','direction':'CALL',
                'entry_trigger':{'instrument':'NIFTY 50','type':'breakout','level':24900,'confirmation':'5m_close_above'},
                'invalidation':{'level':24800},'target1':{'level':25000},'target2':{'level':25100},
                't1_rr':1,'t2_rr':2,'option':option}}
    noop = lambda: None
    signals = SimpleNamespace(start=noop, close=noop,
        current=lambda index: {'index':index,'record':record if index=='NIFTY' else None,'data_quality':{'stale':False},'confidence':85},
        detail=lambda sid: {'record':record} if sid=='simulation-only' else None)
    stream = SimpleNamespace(start=noop, shutdown=noop, live=lambda:{'websocket_status':'connected','instruments':[row]})
    options = SimpleNamespace(start=noop,shutdown=noop,response=lambda *args: ({},[option]))
    engine = SimpleNamespace(start=noop,shutdown=noop)
    trades = TradeService(stream,signals,options,path,clock=lambda:now[0])
    trades.start = noop  # Steps, not wall time, advance this fixture.
    app = create_app(Settings(), stream=stream, engine=engine, options=options, signals=signals, trades=trades)

    @app.get('/replay')
    def page():
        return FileResponse(Path(__file__).parent/'replay.html')

    @app.get('/replay.js')
    def javascript():
        return FileResponse(Path(__file__).parent/'replay.js', media_type='text/javascript')

    @app.post('/api/trades/replay/next')
    def next_observation():
        sequence = [(24840,False),(24790,True),(24790,False)]
        value,stale = sequence[min(step[0],len(sequence)-1)]
        step[0]+=1;now[0]+=timedelta(seconds=5)
        row.update(last_price=value, stale=stale,
                   last_tick_received_at=(now[0]-timedelta(seconds=60) if stale else now[0]).isoformat())
        option.update(received_at=now[0].isoformat(),timestamp=now[0].isoformat())
        trades.process(row,True,now[0])
        return {'step':step[0],'price':value,'stale':stale}

    return app


if __name__ == '__main__':
    import uvicorn
    with TemporaryDirectory(prefix='manual-trade-replay-') as directory:
        uvicorn.run(replay_app(Path(directory)/'replay.sqlite3'),host='127.0.0.1',port=8001,access_log=False)
