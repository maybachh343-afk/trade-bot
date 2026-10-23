"""Private Telegram controls and durable, at-least-once notifications.
No wallet keys or API credentials are accepted via chat.
"""
import hashlib, hmac, json, os, secrets, sqlite3, time, urllib.request
from pathlib import Path
from bot import Blocked, address, require

HELP = '''SENTINEL • private trading controls
/status — mode, worker health, open positions
/positions — managed holdings
/pnl — today's realized results (UTC)
/wallet — public deposit address only
/settings — trading limits
/watch ADDRESS — add a watched wallet
/unwatch ADDRESS — remove a watched wallet
/watches — list watched wallets
/dev MINT ADDRESS — add a verified developer mapping
/pause — stop buys; keep exits running
/resume — request confirmation to allow buys
/exit_all — request confirmation to exit positions
/close MINT — request one position's exit
/halt — stop all new submissions, INCLUDING exits
/unhalt — clear HALT; entries stay paused
/clear_exit — clear exit-all after positions are flat
Never send private keys, seed phrases or API tokens here.'''

class Telegram:
    def __init__(self, token):
        require(token and ':' in token,'Set TELEGRAM_BOT_TOKEN in hosting secrets')
        self.base='https://api.telegram.org/bot'+token+'/'
    def call(self, method, data):
        req=urllib.request.Request(self.base+method,json.dumps(data).encode(),{'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req,timeout=12) as r: result=json.load(r)
            require(result.get('ok') is True,'Telegram rejected request')
            return result['result']
        except Exception: raise Blocked('Telegram request failed') from None
    def send(self, chat, text):
        return self.call('sendMessage',{'chat_id':chat,'text':text[:4000], 'link_preview_options':{'is_disabled':True}})

class Control:
    def __init__(self,root,api,pair_code):
        self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True)
        self.api=api; self.pair_code=pair_code
        require(len(pair_code)>=24,'PAIRING_CODE must contain at least 24 characters')
        self.db=sqlite3.connect(self.root/'control.sqlite',timeout=15);self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY,v TEXT);
        CREATE TABLE IF NOT EXISTS outbox(k TEXT PRIMARY KEY,text TEXT,sent INTEGER DEFAULT 0,ts REAL);
        CREATE TABLE IF NOT EXISTS challenges(token TEXT PRIMARY KEY,action TEXT,expires REAL);
        CREATE TABLE IF NOT EXISTS throttle(k TEXT PRIMARY KEY,ts REAL);
        ''')
    def get(self,k,default=None):
        r=self.db.execute('SELECT v FROM kv WHERE k=?',(k,)).fetchone();return r[0] if r else default
    def put(self,k,v): self.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)',(k,str(v)))
    def config(self): return json.loads((self.root/'config.json').read_text())
    def save_config(self,c):
        temp=self.root/'config.tmp';temp.write_text(json.dumps(c,indent=2));temp.replace(self.root/'config.json')
    def queue(self,key,text):
        with self.db: self.db.execute('INSERT OR IGNORE INTO outbox(k,text,ts) VALUES (?,?,?)',(key,text,time.time()))
    def state_db(self):
        mode=self.config()['mode'];path=self.root/(mode+'.sqlite')
        if not path.exists(): return None
        d=sqlite3.connect(path,timeout=5);d.row_factory=sqlite3.Row;return d
    def rows(self,query,args=()):
        d=self.state_db()
        if d is None:return []
        try:return [dict(r) for r in d.execute(query,args)]
        finally:d.close()
    def positions(self): return self.rows('SELECT * FROM positions')
    def pending(self): return self.rows("SELECT id,signature FROM orders WHERE status='pending'")
    def health(self):
        try:return json.loads((self.root/'health.json').read_text())
        except (OSError,ValueError):return {'status':'starting','at':0}
    def challenge(self,action,uid):
        nonce=secrets.token_hex(8)
        with self.db:
            self.db.execute('DELETE FROM challenges WHERE expires<?',(time.time(),))
            self.db.execute('INSERT INTO challenges VALUES (?,?,?)',(nonce,action,time.time()+120))
        c=self.config()
        text=f'{action.upper()} requested. Mode: {c["mode"].upper()}. '
        if action=='resume':text+=f'Entry {c["trade_sol"]} SOL; daily risk budget {c["max_daily_risk_sol"]} SOL. '
        if action=='exit_all' or action.startswith('close '):text+='Exits can fail or realize losses; existing slippage limits still apply. '
        return text+f'Confirm within 2 minutes: /confirm {nonce}'
    def apply(self,action):
        if action=='resume':
            require(self.get('owner') is not None,'pair first')
            require(not (self.root/'HALT').exists(),'HALT active; use /unhalt first')
            require(not (self.root/'EXIT_ALL').exists(),'exit-all active; use /clear_exit after flat')
            require(not self.pending(),'pending transaction blocks resume')
            h=self.health();require(time.time()-h.get('at',0)<30 and h.get('status')=='ready','worker not ready; check /status')
            require(self.config()['mode']!='live' or os.environ.get('ENABLE_LIVE_TRADING')=='YES','live trading not enabled in hosting settings')
            (self.root/'PAUSE').unlink(missing_ok=True)
            return 'Entries enabled. Risk filters still apply; trades are not guaranteed.'
        if action=='exit_all':
            (self.root/'PAUSE').touch();(self.root/'EXIT_ALL').touch()
            return 'Exit-all requested. Check /positions and fill alerts. HALT or pending transactions can block exits.'
        if action.startswith('close '):
            mint=address(action.split()[1]);require(any(p['mint']==mint for p in self.positions()),'position not found')
            (self.root/('CLOSE_'+mint)).touch();return 'Exit requested. A request is not a confirmed sale.'
        raise Blocked('unknown action')
    def handle(self,u):
        uid=int(u['update_id'])
        if uid<int(self.get('offset','0')):return
        m=u.get('message',{});chat=m.get('chat',{});sender=m.get('from',{})
        if chat.get('type')!='private' or sender.get('is_bot') or chat.get('id')!=sender.get('id'):
            with self.db:self.put('offset',uid+1)
            return
        owner=self.get('owner');text=m.get('text','');parts=text.split(); cmd=parts[0].split('@')[0] if parts else ''
        # Do not persist raw chat contents; they could contain accidentally pasted secrets.
        response=None
        fresh=0<=time.time()-m.get('date',0)<=120
        if owner is None:
            if cmd=='/pair' and fresh and len(parts)==2 and hmac.compare_digest(parts[1],self.pair_code):
                with self.db:self.put('owner',chat['id'])
                response='Paired to your Telegram account. Entries are PAUSED.\n'+HELP
                try:self.api.call('deleteMessage',{'chat_id':chat['id'],'message_id':m['message_id']})
                except Blocked:pass
            elif cmd=='/start':response='Private bot. Pair using /pair followed by your hosting PAIRING_CODE. Do not send API tokens or wallet secrets.'
        elif str(chat['id'])==owner:
            try:
                if not fresh: response='Old command ignored. Send it again if still needed.'
                elif cmd in ('/start','/help'):response=HELP
                elif cmd=='/status':
                    h=self.health(); age=int(time.time()-h.get('at',0)); c=self.config()
                    response=f'{c["mode"].upper()} | {"PAUSED" if (self.root/"PAUSE").exists() else "ENTRIES ENABLED"}\nWorker: {h.get("status")} (heartbeat {age}s ago)\n{h.get("detail","")}\nPositions: {len(self.positions())} | Pending: {len(self.pending())}\nHALT: {(self.root/"HALT").exists()} | EXIT_ALL: {(self.root/"EXIT_ALL").exists()}'
                elif cmd=='/positions':response='No managed positions.' if not self.positions() else '\n\n'.join(f'{p["mint"]}\nCost {p["cost"]/1e9:.6f} SOL | held {int(time.time()-p["opened"])}s' for p in self.positions())
                elif cmd=='/pnl':
                    rows=self.rows('SELECT side,pnl FROM fills WHERE ts>=?',(int(time.time()//86400)*86400,))
                    response=f'{self.config()["mode"].upper()} • UTC today\nCompleted exits: {sum(r["side"]=="sell" for r in rows)}\nRealized net: {sum(r["pnl"] for r in rows)/1e9:+.6f} SOL\nIncludes recorded failed transaction fees. Open-position changes excluded.'
                elif cmd=='/wallet':
                    public=json.loads((self.root/'wallet-public.json').read_text())['address']
                    response=f'Dedicated wallet PUBLIC address:\n{public}\nMode: {self.config()["mode"].upper()}. Do not fund until live testing and setup are reviewed.'
                elif cmd=='/settings':response=json.dumps({k:v for k,v in self.config().items() if k not in ('watch_wallets','developer_wallets')},indent=2)
                elif cmd=='/watches':response='\n'.join(self.config()['watch_wallets']) or 'Watchlist empty. Use /watch PUBLIC_ADDRESS.'
                elif cmd in ('/watch','/unwatch') and len(parts)==2:
                    w=address(parts[1]);c=self.config();ws=c['watch_wallets']
                    if cmd=='/watch' and w not in ws:
                        require(len(ws)<20,'maximum 20 watched wallets in this build');ws.append(w)
                    if cmd=='/unwatch' and w in ws:ws.remove(w)
                    self.save_config(c);response='Watchlist saved. Stream will reconnect; entries pause until it is ready.'
                elif cmd=='/dev' and len(parts)==3:
                    mint,w=address(parts[1]),address(parts[2]);c=self.config()
                    ws=c['developer_wallets'].setdefault(mint,[])
                    if w not in ws:ws.append(w)
                    self.save_config(c);response='Developer mapping saved as YOUR supplied attribution; identity is not independently verified by the bot.'
                elif cmd=='/pause':(self.root/'PAUSE').touch();response='New buys paused. Exit monitoring continues.'
                elif cmd=='/halt':(self.root/'HALT').touch();(self.root/'PAUSE').touch();response='HALT active: all new submissions stopped, INCLUDING protective exits. Already submitted transactions cannot be cancelled.'
                elif cmd=='/unhalt':(self.root/'PAUSE').touch();(self.root/'HALT').unlink(missing_ok=True);response='HALT cleared. Exit monitoring resumes; entries remain paused.'
                elif cmd=='/clear_exit':
                    require(not self.positions() and not self.pending(),'positions or pending orders remain')
                    (self.root/'EXIT_ALL').unlink(missing_ok=True);response='Exit-all cleared. Entries remain paused.'
                elif cmd in ('/resume','/exit_all'):response=self.challenge(cmd[1:],uid)
                elif cmd=='/close' and len(parts)==2:response=self.challenge('close '+address(parts[1]),uid)
                elif cmd=='/confirm' and len(parts)==2:
                    with self.db:
                        row=self.db.execute('SELECT * FROM challenges WHERE token=?',(parts[1],)).fetchone()
                        require(row is not None and row['expires']>=time.time(),'confirmation missing or expired')
                        self.db.execute('DELETE FROM challenges WHERE token=?',(parts[1],))
                    response=self.apply(row['action'])
                else:response='Unknown command. Use /help. Do not send wallet secrets here.'
            except (Blocked,ValueError,KeyError,IndexError):response='Request blocked. Check the command, /status and /help; configuration and position checks must pass.'
        # Mutations are idempotent; confirmation tokens are consumed before executing their action.
        with self.db:
            if response:self.db.execute('INSERT OR IGNORE INTO outbox(k,text,ts) VALUES (?,?,?)',(f'command:{uid}:{chat["id"]}',response,time.time()))
            self.put('offset',uid+1)
        # An unpaired /start response is sent directly only to its sender, never queued to a future owner.
        if owner is None and self.get('owner') is None and response:
            try:self.api.send(chat['id'],response)
            finally:
                with self.db:self.db.execute('DELETE FROM outbox WHERE k=?',(f'command:{uid}:{chat["id"]}',))
    def collect(self):
        mode=self.config()['mode'];last=int(self.get('fills:'+mode,'0'))
        rows=self.rows('SELECT f.*,o.signature,o.reason FROM fills f LEFT JOIN orders o ON o.id=f.order_id WHERE f.id>? ORDER BY f.id',(last,))
        with self.db:
            for r in rows:
                t=f'{mode.upper()} • {r["side"].upper()} #{r["id"]}\n{r["mint"]}\nSOL movement: {r["sol"]/1e9:.6f}'
                if r['side']=='sell':t+=f'\nRealized net: {r["pnl"]/1e9:+.6f} SOL'
                if r['side']=='failed':t+='\nTransaction failed on-chain; fee recorded.'
                if r.get('reason'):t+='\nReason: '+r['reason']
                if r.get('signature'):t+='\nhttps://solscan.io/tx/'+r['signature']
                self.db.execute('INSERT OR IGNORE INTO outbox(k,text,ts) VALUES (?,?,?)',(f'fill:{mode}:{r["id"]}',t,time.time()))
                self.put('fills:'+mode,r['id'])
        last=int(self.get('audit:'+mode,'0'))
        audits=self.rows('SELECT * FROM audit WHERE id>? ORDER BY id LIMIT 200',(last,))
        for r in audits:
            if r['event'] in ('exit_blocked','submission_uncertain','failed_on_chain','cycle_blocked','auto_pause'):
                key=hashlib.sha256((r['event']+r['data']).encode()).hexdigest()
                old=self.db.execute('SELECT ts FROM throttle WHERE k=?',(key,)).fetchone()
                if old is None or time.time()-old[0]>300:
                    self.queue(f'alert:{mode}:{r["id"]}',f'{mode.upper()} • {r["event"]}\n{r["data"][:1500]}')
                    with self.db:self.db.execute('INSERT OR REPLACE INTO throttle VALUES (?,?)',(key,time.time()))
            with self.db:self.put('audit:'+mode,r['id'])
    def flush(self):
        owner=self.get('owner')
        if not owner:return
        for row in self.db.execute('SELECT k,text FROM outbox WHERE sent=0 ORDER BY ts LIMIT 10').fetchall():
            self.api.send(int(owner),row['text'])
            with self.db:self.db.execute('UPDATE outbox SET sent=1 WHERE k=?',(row['k'],))
    def tick(self):
        updates=self.api.call('getUpdates',{'offset':int(self.get('offset','0')),'timeout':2,'allowed_updates':['message']})
        for u in updates:self.handle(u)
        self.collect();self.flush()
        (self.root/'telegram-alive').write_text(str(time.time()))
