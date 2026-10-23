"""Cloud entry point. Telegram credentials in environment, ledger on persistent disk."""
import base64, contextlib, hashlib, json, os, signal, threading, time
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from bot import Bot, Store, Providers, Keypair, Blocked, require, load_config, worker_lock, lamports
from stream import WalletStream
from coordination import Coordinator
from telegram_control import Telegram, Control

STOP=threading.Event()

def atomic_json(path,value):
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,delete=False) as f:
        json.dump(value,f);tmp=Path(f.name)
    tmp.replace(path)

def vault_key(secret):
    require(len(secret)>=32,'WALLET_VAULT_KEY must be a secret of at least 32 characters')
    return hashlib.sha256(secret.encode()).digest()

def provision_wallet(root,secret):
    path=root/'wallet.enc';cipher=AESGCM(vault_key(secret))
    if path.exists():
        b=base64.b64decode(path.read_bytes());key=Keypair.from_bytes(cipher.decrypt(b[:12],b[12:],b'sentinel-wallet-v1'))
    else:
        require(os.environ.get('BOT_MODE','paper')!='live','missing wallet vault: initialize in paper mode first')
        key=Keypair();nonce=os.urandom(12);payload=base64.b64encode(nonce+cipher.encrypt(nonce,bytes(key),b'sentinel-wallet-v1'))
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'wb') as f:f.write(payload)
    atomic_json(root/'wallet-public.json',{'address':str(key.pubkey())})
    return key

class CloudBot(Bot):
    def __init__(self,*args,stream=None,coordinator=None,**kwargs):super().__init__(*args,**kwargs);self.stream=stream;self.coordinator=coordinator
    def pause_entries(self):
        if super().pause_entries():return True
        if not os.environ.get('TELEGRAM_BOT_TOKEN'):return False
        try:return time.time()-float((self.control_dir/'telegram-alive').read_text())>60
        except (OSError,ValueError):return True
    def scan(self):
        require(self.stream is not None and self.stream.ready(),'wallet stream warming up, disconnected or lagging')
        cutoff=time.time()-self.c['signal_window_seconds']
        latest={}
        for r in self.s.rows('SELECT * FROM events WHERE ts>=? ORDER BY ts',(cutoff,)):
            if r['wallet'] in self.c['watch_wallets']:latest[(r['mint'],r['wallet'])]=r
        groups={}
        for (mint,w),r in latest.items():
            if r['side']=='buy':groups.setdefault(mint,[]).append(w)
        return {m:ws for m,ws in groups.items() if len(ws)>=self.c['min_buyers']}
    def execution_fence(self):
        require(self.coordinator is not None,"Shared coordinator required")
        self.coordinator.check()

    def trade(self,mint,side,amount,reason):
        require(self.c["mode"]=="paper","Live execution disabled in this release")
        require(self.coordinator is not None,"Shared coordinator required")
        self.coordinator.check()
        if side=='buy':
            require(self.stream is not None and self.stream.ready(),'wallet stream not ready')
            since=int(time.time()//86400)*86400
            attempts=self.s.db.execute("SELECT COUNT(*) FROM orders WHERE side='buy' AND ts>=?",(since,)).fetchone()[0]
            require(attempts<self.c.get('max_daily_entries',50),'daily entry attempt cap reached')
            require(not (self.control_dir/('CLOSE_'+mint)).exists(),'manual exit flag still active')
            # Net round-trip quote losses plus cost allowances cannot consume the target profit.
            # This is a cost gate, NOT a proven expectation of profit.
            q=self.p.quote('So11111111111111111111111111111111111111112',mint,amount)
            reverse=self.p.quote(mint,'So11111111111111111111111111111111111111112',int(q['otherAmountThreshold']))
            worst_cost=max(0,amount-int(reverse['otherAmountThreshold']))+2*lamports(self.c['max_tx_cost_sol'])
            require(worst_cost/amount<self.c['take_profit_fraction'],'roundtrip cost budget exceeds profit target')
        return super().trade(mint,side,amount,reason)

def telegram_loop(root):
    ctl=None
    try:
        ctl=Control(root,Telegram(os.environ.get('TELEGRAM_BOT_TOKEN','')),os.environ.get('PAIRING_CODE',''))
        while not STOP.is_set():
            try:ctl.tick()
            except Exception:STOP.wait(3) # No exception URL/token leaks; entries stop on heartbeat expiry.
    finally:
        if ctl:ctl.db.close()

def serve(root):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    if os.environ.get('TELEGRAM_BOT_TOKEN'):require(len(os.environ.get('PAIRING_CODE',''))>=24,'Telegram pairing code required')
    with worker_lock(root/'cloud.lock'):
        key=provision_wallet(root,os.environ.get('WALLET_VAULT_KEY',''))
        path=root/'config.json'
        if not path.exists():atomic_json(path,json.loads(Path(__file__).with_name('config.scalp.example.json').read_text()))
        # A restart never silently enables new entries.
        (root/'PAUSE').touch()
        thread=threading.Thread(target=telegram_loop,args=(root,),daemon=True) if os.environ.get('TELEGRAM_BOT_TOKEN') else None
        if thread:thread.start()
        coordinator=Coordinator(root)
        s=stream=b=None; last_error=None
        try:
            while not STOP.is_set():
                try:
                    c=json.loads(path.read_text())
                    requested=os.environ.get('BOT_MODE','paper');require(requested=='paper','Live execution disabled in this release')
                    if c['mode']!=requested:
                        old=root/(c['mode']+'.sqlite')
                        if old.exists():
                            import sqlite3
                            d=sqlite3.connect(old)
                            try:require(d.execute('SELECT COUNT(*) FROM positions').fetchone()[0]==0 and d.execute("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0]==0,'resolve current positions before switching mode')
                            finally:d.close()
                        require(s is None,'restart required to switch mode')
                        c['mode']=requested;atomic_json(path,c)
                    # Validate using temporary file without modifying owner settings.
                    c=load_config(path)
                    if c['mode']=='live':
                        require(os.environ.get('ENABLE_LIVE_TRADING')=='YES','live environment gate missing')
                        require(os.environ.get('EXPECTED_WALLET')==str(key.pubkey()),'EXPECTED_WALLET must match /wallet')
                        require(os.environ.get('JUPITER_API_KEY'),'Jupiter key required in live mode')
                        require(str(key.pubkey()) not in c['watch_wallets'],'cannot watch own trading wallet')
                    if s is None:
                        ledger=root/(c['mode']+'.sqlite')
                        marker=root/(c['mode']+'-ledger-established')
                        require(ledger.exists() or not marker.exists(),'ledger missing: restore backup; refusing to reset trading history')
                        s=Store(ledger,c,str(key.pubkey()) if c['mode']=='live' else 'paper')
                        marker.touch()
                        p=Providers(c);b=CloudBot(c,s,p,key if c['mode']=='live' else None,root,coordinator=coordinator)
                    else:
                        require(c['mode']==s.get('mode'),'restart required for mode switch')
                        fixed={k:v for k,v in c.items() if k not in ('watch_wallets','developer_wallets')}
                        require(hashlib.sha256(json.dumps(fixed,sort_keys=True).encode()).hexdigest()==s.get('fingerprint'),'risk settings changed; restore settings until flat and explicitly migrate')
                        b.c=c;b.p.c=c
                    if stream is None or tuple(c['watch_wallets'])!=stream.wallets:
                        if stream:stream.close()
                        stream=WalletStream(c,root,root/(c['mode']+'.sqlite'),os.environ.get('SOLANA_WS_URL',''))
                        stream.start();b.stream=stream
                    coordinator.check()
                    # Heartbeat refreshed before and after work. Commands cannot assume a stuck worker is ready.
                    atomic_json(root/'health.json',{'at':time.time(),'status':'ready','detail':'Stream ready' if stream.ready() else 'Stream warming up/disconnected; entries blocked'})
                    # Circuit breakers keep exits active and require explicit operator resume.
                    last=s.rows("SELECT id,side,pnl FROM fills WHERE side IN ('sell','failed') ORDER BY id DESC LIMIT ?",(c.get('max_consecutive_losses',3),))
                    last_id=last[0]['id'] if last else 0
                    if len(last)>=c.get('max_consecutive_losses',3) and all(r['pnl']<0 for r in last) and s.get('loss_pause_id')!=str(last_id):
                        (root/'PAUSE').touch()
                        with s.db:s.set('loss_pause_id',last_id)
                        s.audit('auto_pause',{'reason':'consecutive realized losses; review before resume'})
                    b.cycle();last_error=None
                    atomic_json(root/'health.json',{'at':time.time(),'status':'ready','detail':'Stream ready' if stream.ready() else 'Stream warming up/disconnected; entries blocked'})
                    STOP.wait(c.get('exit_poll_seconds',3))
                except Exception as e:
                    reason=str(e) if isinstance(e,Blocked) else type(e).__name__
                    # Only known operational messages; never trace env values or provider URLs.
                    atomic_json(root/'health.json',{'at':time.time(),'status':'blocked','detail':reason[:250]})
                    if s and reason!=last_error:s.audit('cycle_blocked',{'reason':reason})
                    last_error=reason
                    # Invalid new settings must not disable exits for previously managed positions.
                    if b is not None and not (root/'HALT').exists():
                        try:
                            if b.c['mode']=='live':b.reconcile()
                            if not b.s.pending():b.exits()
                        except Exception:pass
                    STOP.wait(3)
        finally:
            (root/'PAUSE').touch()
            coordinator.close()
            if stream:stream.close()
            if s:s.db.close()
            if thread:thread.join(timeout=15)
            atomic_json(root/'health.json',{'at':time.time(),'status':'stopped','detail':'No monitoring until the server restarts'})

if __name__=='__main__':
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:STOP.set())
    try:serve(os.environ.get('DATA_DIR','/var/data'))
    except Exception as e:
        print('Cloud startup blocked:',str(e) if isinstance(e,Blocked) else type(e).__name__,flush=True)
        raise SystemExit(1)
