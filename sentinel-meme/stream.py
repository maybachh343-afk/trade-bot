"""Watched-wallet finalized log stream; separate fetch thread keeps exits responsive."""
import json, queue, sqlite3, threading, time
from pathlib import Path
import websocket
from bot import Providers, buyer_event, Blocked, require

class WalletStream:
    def __init__(self,c,root,db_path,ws_url):
        require(ws_url.startswith('wss://'),'SOLANA_WS_URL must be a WSS mainnet endpoint')
        self.c,self.root,self.db_path,self.url=c,Path(root),str(db_path),ws_url
        self.queue=queue.Queue(maxsize=500);self.stop=threading.Event();self.lock=threading.Lock()
        self.subscriptions={};self.connected=False;self.pong=0;self.failed_until=0;self.generation=0
        self.wallets=tuple(c['watch_wallets']);self.app=None
    def mark_failure(self):
        with self.lock:self.failed_until=time.time()+self.c['signal_window_seconds'];self.generation+=1
    def ready(self):
        with self.lock:
            return self.connected and time.time()-self.pong<40 and len(self.subscriptions)==len(self.wallets) and time.time()>=self.failed_until and self.queue.qsize()<20
    def on_open(self,ws):
        with self.lock:self.connected=True;self.pong=time.time();self.subscriptions={}
        for i,w in enumerate(self.wallets):
            ws.send(json.dumps({'jsonrpc':'2.0','id':i+1,'method':'logsSubscribe','params':[{'mentions':[w]},{'commitment':'finalized'}]}))
    def on_pong(self,ws,data):
        with self.lock:self.pong=time.time()
    def on_message(self,ws,message):
        try:
            r=json.loads(message)
            if 'error' in r:raise Blocked('subscription error')
            if 'id' in r:
                i=int(r['id'])-1
                require(0<=i<len(self.wallets) and isinstance(r.get('result'),int),'invalid subscription response')
                with self.lock:self.subscriptions[r['result']]=self.wallets[i]
            elif r.get('method')=='logsNotification':
                p=r['params'];v=p['result']['value']
                if v.get('err') is not None:return
                with self.lock:w=self.subscriptions.get(p['subscription']);generation=self.generation
                if not w:raise Blocked('unrecognized subscription')
                self.queue.put_nowait((v['signature'],w,time.time(),generation))
        except Exception:
            self.mark_failure();ws.close()
    def on_close(self,ws,*args):
        with self.lock:self.connected=False
        self.mark_failure()
    def on_error(self,ws,error): self.mark_failure()
    def connect_loop(self):
        while not self.stop.is_set():
            self.app=websocket.WebSocketApp(self.url,on_open=self.on_open,on_message=self.on_message,
                on_error=self.on_error,on_close=self.on_close,on_pong=self.on_pong)
            self.app.run_forever(ping_interval=15,ping_timeout=10)
            self.stop.wait(3)
    def consume(self):
        db=sqlite3.connect(self.db_path,timeout=15)
        p=Providers(self.c)
        try:
            while not self.stop.is_set():
                try:sig,w,received,generation=self.queue.get(timeout=1)
                except queue.Empty:continue
                try:
                    if generation!=self.generation or time.time()-received>self.c['signal_window_seconds']:continue
                    if db.execute('SELECT 1 FROM seen WHERE sig=? AND wallet=?',(sig,w)).fetchone():continue
                    tx=None
                    for _ in range(3):
                        tx=p.transaction(sig)
                        if tx:break
                        self.stop.wait(1)
                    require(tx is not None,'stream transaction unavailable')
                    ts=tx.get('blockTime');require(ts is not None,'missing block time')
                    if time.time()-ts>self.c['signal_window_seconds'] or ts>time.time()+5:continue
                    e=buyer_event(tx,w)
                    # Discard events from an interrupted stream generation.
                    if generation!=self.generation:continue
                    with db:
                        if e:db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)',(sig,w,e['mint'],e['side'],e['raw'],ts))
                        db.execute('INSERT OR IGNORE INTO seen VALUES (?,?)',(sig,w))
                except Exception:self.mark_failure()
                finally:self.queue.task_done()
        finally:db.close()
    def start(self):
        # Warm up for one signal window, rather than act on an incomplete initial sample.
        self.mark_failure()
        self.threads=[threading.Thread(target=self.connect_loop,daemon=True),threading.Thread(target=self.consume,daemon=True)]
        for t in self.threads:t.start()
    def close(self):
        self.stop.set()
        if self.app:self.app.close()
        for t in getattr(self,'threads',[]):t.join(timeout=5)
