"""Continuous discovery only; these unverified feeds never authorize trades."""
import json
import os
import threading
import time
import urllib.request
import websocket
from pathlib import Path

class MarketFeeds:
    def __init__(self, root, stop):
        self.root, self.stop = Path(root), stop
        self.lock = threading.Lock()
        self.state = {'dex_status':'starting','pump_status':'starting','pairs':[],'new_tokens':[]}
        self.ws = None
    def update(self, **fields):
        with self.lock:
            self.state.update(fields, at=time.time())
            tmp=self.root/'market.tmp';tmp.write_text(json.dumps(self.state));tmp.replace(self.root/'market.json')
    def start(self):
        self.threads=[threading.Thread(target=f,daemon=True) for f in (self.dex,self.pump)]
        for t in self.threads:t.start()
    def close(self):
        if self.ws:self.ws.close()
        for t in self.threads:t.join(timeout=2)
    def get(self,url):
        req=urllib.request.Request(url,headers={'User-Agent':'SENTINEL-paper/0.3'})
        with urllib.request.urlopen(req,timeout=15) as r:return json.load(r)
    def dex(self):
        while not self.stop.is_set():
            try:
                profiles=self.get('https://api.dexscreener.com/token-profiles/latest/v1')
                from solders.pubkey import Pubkey
                mints=[]
                for p in profiles:
                    if p.get('chainId')=='solana':
                        try: mints.append(str(Pubkey.from_string(p['tokenAddress'])))
                        except (ValueError,KeyError):continue
                    if len(mints)>=20:break
                pairs=self.get('https://api.dexscreener.com/tokens/v1/solana/'+','.join(mints)) if mints else []
                self.update(dex_status='connected',dex_at=time.time(),pairs=pairs[:60])
            except Exception:self.update(dex_status='unavailable — previous data may be stale')
            self.stop.wait(30)
    def pump(self):
        key=os.environ.get('PUMPPORTAL_API_KEY','')
        if not key:
            self.update(pump_status='not configured — API key required');return
        from urllib.parse import quote
        def opened(ws):
            ws.send(json.dumps({'method':'subscribeNewToken'}))
            self.update(pump_status='connected; awaiting events')
        def message(ws,raw):
            try:
                value=json.loads(raw)
                if value.get('errors') or value.get('error'):
                    self.update(pump_status='provider rejected subscription');return
                if not value.get('mint'):return
                fields={k:value.get(k) for k in ('mint','name','symbol','traderPublicKey','signature','txType')}
                with self.lock:items=[fields]+self.state['new_tokens'][:49]
                self.update(pump_status='receiving token events',pump_at=time.time(),new_tokens=items)
            except (ValueError,AttributeError):self.update(pump_status='invalid provider event')
        while not self.stop.is_set():
            try:
                self.ws=websocket.WebSocketApp('wss://pumpportal.fun/api/data?api-key='+quote(key,safe=''),on_open=opened,on_message=message)
                self.ws.run_forever(ping_interval=20,ping_timeout=10)
            except Exception:pass
            self.update(pump_status='disconnected; reconnecting')
            self.stop.wait(5)
