"""SENTINEL MEME: local, single-writer Solana auto-trading prototype."""
from __future__ import annotations
import argparse, base64, contextlib, hashlib, json, math, os, sqlite3, time, urllib.request, urllib.parse
from pathlib import Path
from decimal import Decimal
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.message import to_bytes_versioned
from solders.transaction import VersionedTransaction

SOL = 'So11111111111111111111111111111111111111112'
TOKEN = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
JUP = 'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4'
SYSTEM = '11111111111111111111111111111111'
ATA = 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'
COMPUTE = 'ComputeBudget111111111111111111111111111111'
LAMPORTS = 1_000_000_000

class Blocked(Exception): pass

def require(ok, reason):
    if not ok: raise Blocked(reason)

def integer(x):
    require(isinstance(x, (str, int)) and not isinstance(x, bool), 'invalid integer')
    n = int(x)
    require(n >= 0 and str(n) == str(x), 'invalid unsigned amount')
    return n

def number(x):
    require(x is not None and not isinstance(x, bool), 'missing number')
    n = float(x)
    require(math.isfinite(n), 'nonfinite number')
    return n

def lamports(x):
    n = Decimal(str(x)) * LAMPORTS
    require(n.is_finite() and n >= 0 and n == n.to_integral_value(), 'invalid SOL amount')
    return int(n)

def address(x):
    Pubkey.from_string(x)
    return x

def load_config(path):
    c = json.loads(Path(path).read_text())
    require(c['mode'] in ('paper','live'), 'mode must be paper or live')
    for key in ('trade_sol','max_total_buy_sol','max_open_cost_sol','max_daily_risk_sol','reserve_sol','max_tx_cost_sol','paper_start_sol'):
        require(lamports(c[key]) > 0, f'{key} must be positive')
    for key in ('poll_seconds','signal_window_seconds','min_buyers','max_positions','max_hold_seconds','min_liquidity_usd','min_pool_age_seconds','max_wallet_transactions'):
        require(number(c[key]) > 0, f'{key} must be positive')
    require(1 <= c['max_wallet_transactions'] <= 1000 and isinstance(c['max_wallet_transactions'], int), 'invalid scan limit')
    require(0 < number(c['max_roundtrip_loss_fraction']) <= 0.2, 'invalid roundtrip threshold')
    require(c['poll_seconds'] >= 10, 'poll interval must be >=10 seconds')
    require(1 <= c['slippage_bps'] <= 500, 'slippage must be 1..500 bps')
    require(0 < c['max_price_impact_pct'] <= 5, 'impact must be >0 and <=5 percent')
    require(0 < c['max_top20_fraction'] <= 1, 'invalid concentration limit')
    require(0 < c['stop_loss_fraction'] < 1 and 0 < c['trailing_fraction'] < 1 and c['take_profit_fraction'] > 0, 'invalid exit thresholds')
    require(len(c['watch_wallets']) >= c['min_buyers'], 'add at least min_buyers real wallet addresses')
    for w in c['watch_wallets']: address(w)
    require(len(set(c['watch_wallets'])) == len(c['watch_wallets']), 'duplicate watch wallets')
    for mint, wallets in c['developer_wallets'].items():
        address(mint)
        require(bool(wallets), 'developer wallet list empty')
        for w in wallets: address(w)
    require(lamports(c['trade_sol']) + lamports(c['max_tx_cost_sol']) <= lamports(c['max_open_cost_sol']), 'trade exceeds exposure limit')
    return c

class HTTP:
    def __init__(self): self.last = {}
    def json(self, url, data=None, headers=None):
        # No POST retry: a timeout after submission is an ambiguous outcome.
        host = urllib.parse.urlsplit(url).netloc
        delay = 2.1 - (time.monotonic() - self.last.get(host, 0))
        if delay > 0: time.sleep(delay)
        self.last[host] = time.monotonic()
        h = {'Content-Type':'application/json', **(headers or {})}
        req = urllib.request.Request(url, data=None if data is None else json.dumps(data).encode(), headers=h)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                require(r.status == 200, 'provider HTTP error')
                return json.load(r)
        except Blocked: raise
        except Exception as e:
            # Never log provider URLs, which can contain API keys.
            raise Blocked('provider unavailable: ' + type(e).__name__) from None

class Providers:
    def __init__(self, c):
        self.c, self.http = c, HTTP()
        self.rpc_url = os.environ.get('SOLANA_RPC_URL','')
        require(self.rpc_url.startswith('https://'), 'set SOLANA_RPC_URL to an HTTPS mainnet RPC endpoint')
        self.jup_headers = {'x-api-key':os.environ['JUPITER_API_KEY']} if os.environ.get('JUPITER_API_KEY') else {}
    def rpc(self, method, params):
        r = self.http.json(self.rpc_url, {'jsonrpc':'2.0','id':1,'method':method,'params':params})
        require('error' not in r and 'result' in r, 'RPC failed: '+method)
        return r['result']
    def account(self, key):
        r = self.rpc('getAccountInfo',[key,{'encoding':'jsonParsed','commitment':'confirmed'}])
        require(r['value'] is not None, 'account missing')
        return r['value']
    def balance(self, wallet): return self.rpc('getBalance',[wallet,{'commitment':'confirmed'}])['value']
    def token_accounts(self, wallet):
        return self.rpc('getTokenAccountsByOwner',[wallet,{'programId':TOKEN},{'encoding':'jsonParsed','commitment':'confirmed'}])['value']
    def quote(self, input_mint, output_mint, amount, wallet=None):
        params = {'inputMint':input_mint,'outputMint':output_mint,'amount':str(amount),
                  'slippageBps':self.c['slippage_bps'],'excludeRouters':'jupiterz,dflow,okx',
                  'maxSupportedTransactionVersion':'0','priorityFeeLamports':'100000','broadcastFeeType':'maxCap'}
        if wallet: params['taker'] = wallet
        q = self.http.json('https://api.jup.ag/swap/v2/order?'+urllib.parse.urlencode(params), headers=self.jup_headers)
        validate_quote(q,input_mint,output_mint,amount,self.c)
        q['_received'] = time.time()
        return q
    def execute(self, signed, q):
        raise Blocked('Live execution disabled in this release')
        return self.http.json('https://api.jup.ag/swap/v2/execute', {
            'signedTransaction':signed, 'requestId':q['requestId'], 'lastValidBlockHeight':str(q['lastValidBlockHeight'])},self.jup_headers)
    def transaction(self, signature):
        return self.rpc('getTransaction',[signature,{'encoding':'jsonParsed','commitment':'finalized','maxSupportedTransactionVersion':0}])
    def wallet_history(self, wallet):
        rows = self.rpc('getSignaturesForAddress',[wallet,{'limit':self.c['max_wallet_transactions'],'commitment':'finalized'}])
        cutoff = time.time()-self.c['signal_window_seconds']
        require(len(rows)<self.c['max_wallet_transactions'] or (rows[-1].get('blockTime') or 0)<cutoff,
                'wallet scan truncated; increase max_wallet_transactions')
        return rows
    def token_check(self, mint):
        a = self.account(mint)
        require(a['owner'] == TOKEN, 'unsupported token program; Token-2022 rejected in v0.1')
        p = a['data']['parsed']
        require(p['type']=='mint','not a mint')
        i = p['info']
        require(i.get('isInitialized') is True,'mint not initialized')
        require(i.get('mintAuthority') is None and i.get('freezeAuthority') is None,'active mint/freeze authority')
        supply=integer(i['supply']); require(supply>0,'zero supply')
        top = self.rpc('getTokenLargestAccounts',[mint,{'commitment':'confirmed'}])['value']
        require(bool(top),'holder data missing')
        concentration=sum(integer(x['amount']) for x in top)/supply
        # Deliberately counts pool accounts too; conservative false positives are documented.
        require(concentration<=self.c['max_top20_fraction'],'top-20 token-account concentration too high')
        pairs=self.http.json('https://api.dexscreener.com/token-pairs/v1/solana/'+mint)
        eligible=[x for x in pairs if x.get('chainId')=='solana' and x.get('baseToken',{}).get('address')==mint]
        require(bool(eligible),'no supported pool data')
        best=max(eligible,key=lambda x:number(x.get('liquidity',{}).get('usd',0)))
        require(number(best.get('liquidity',{}).get('usd'))>=self.c['min_liquidity_usd'],'liquidity too low')
        require(time.time()-number(best.get('pairCreatedAt'))/1000>=self.c['min_pool_age_seconds'],'pool too new')
        return {'mint':mint,'top20_fraction':concentration,'liquidity_usd':best['liquidity']['usd'],
                'pair':best['pairAddress'],'observed_at':time.time()}

def validate_quote(q, input_mint, output_mint, amount, c):
    require(not q.get('error') and not q.get('errorCode'),'quote error')
    require(q.get('inputMint')==input_mint and q.get('outputMint')==output_mint,'quote mint mismatch')
    require(q.get('swapMode')=='ExactIn' and integer(q['inAmount'])==amount,'quote input mismatch')
    out=integer(q['outAmount']); minimum=integer(q['otherAmountThreshold'])
    require(out>0 and 0<minimum<=out,'invalid quote output')
    require(minimum>=out*(10000-c['slippage_bps'])//10000,'quote exceeds slippage budget')
    require(0<=number(q['slippageBps'])<=c['slippage_bps'],'slippage limit exceeded')
    require(abs(number(q['priceImpact']))<=c['max_price_impact_pct'],'price impact limit exceeded')
    require(q.get('router')=='metis','unsupported router')

def wallet_deltas(tx, wallet):
    require(tx and tx.get('meta') is not None,'transaction missing metadata')
    meta=tx['meta']; require(meta.get('err') is None,'failed transaction')
    keys=tx['transaction']['message']['accountKeys']
    keys=[k['pubkey'] if isinstance(k,dict) else k for k in keys]
    require(wallet in keys,'wallet absent from transaction')
    idx=keys.index(wallet)
    native=int(meta['postBalances'][idx])-int(meta['preBalances'][idx])
    amounts={}
    for sign, field in ((-1,'preTokenBalances'),(1,'postTokenBalances')):
        for b in meta.get(field,[]):
            if b.get('owner')==wallet:
                amounts[b['mint']]=amounts.get(b['mint'],0)+sign*integer(b['uiTokenAmount']['amount'])
    return native,{k:v for k,v in amounts.items() if v}

def buyer_event(tx, wallet):
    # Only known Jupiter swaps, one non-SOL asset, paid with SOL. Transfers aren't buys.
    if not tx or not tx.get('meta') or tx['meta'].get('err') is not None: return None
    msg=tx['transaction']['message']
    if not any(x.get('programId')==JUP for x in msg.get('instructions',[])): return None
    keys=msg['accountKeys']
    if not any(isinstance(k,dict) and k.get('pubkey')==wallet and k.get('signer') for k in keys): return None
    native, delta=wallet_deltas(tx,wallet)
    assets={k:v for k,v in delta.items() if k!=SOL}
    if len(assets)!=1: return None
    mint, qty=next(iter(assets.items()))
    sol_delta=native+delta.get(SOL,0)
    if qty>0 and sol_delta<0: return {'mint':mint,'side':'buy','raw':str(qty)}
    if qty<0 and sol_delta>0: return {'mint':mint,'side':'sell','raw':str(-qty)}
    return None

class Store:
    def __init__(self, path, c, wallet):
        self.db=sqlite3.connect(path); self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL'); self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(sig TEXT,wallet TEXT,mint TEXT,side TEXT,raw TEXT,ts REAL,PRIMARY KEY(sig,wallet));
        CREATE TABLE IF NOT EXISTS seen(sig TEXT,wallet TEXT,PRIMARY KEY(sig,wallet));
        CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY,mint TEXT,side TEXT,amount TEXT,min_out TEXT,signature TEXT UNIQUE,status TEXT,ts REAL,reason TEXT);
        CREATE TABLE IF NOT EXISTS positions(mint TEXT PRIMARY KEY,raw TEXT,cost INTEGER,opened REAL,high INTEGER);
        CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY,order_id INTEGER UNIQUE,mint TEXT,side TEXT,sol INTEGER,raw TEXT,pnl INTEGER,ts REAL);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,ts REAL,event TEXT,data TEXT);
        ''')
        fixed={k:v for k,v in c.items() if k not in ('watch_wallets','developer_wallets')}
        fingerprint=hashlib.sha256(json.dumps(fixed,sort_keys=True).encode()).hexdigest()
        existing=self.get('fingerprint')
        require(existing is None or existing==fingerprint,'config changed: restore config or use a NEW database; never abandon live positions')
        require(self.get('wallet') in (None,wallet),'wallet differs from database')
        with self.db:
            self.set('fingerprint',fingerprint); self.set('wallet',wallet); self.set('mode',c['mode'])
            if self.get('cash') is None: self.set('cash',str(lamports(c['paper_start_sol'])))
    def get(self,k):
        r=self.db.execute('SELECT v FROM meta WHERE k=?',(k,)).fetchone()
        return r['v'] if r else None
    def set(self,k,v): self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(k,str(v)))
    def rows(self,q,args=()): return [dict(x) for x in self.db.execute(q,args)]
    def audit(self,event,data):
        with self.db: self.db.execute('INSERT INTO audit(ts,event,data) VALUES (?,?,?)',(time.time(),event,json.dumps(data)))
        print(json.dumps({'time':time.strftime('%H:%M:%S'),'event':event,**data}),flush=True)
    def positions(self): return self.rows('SELECT * FROM positions')
    def pending(self): return self.rows("SELECT * FROM orders WHERE status='pending'")
    def entry_limits(self,c):
        require(not self.pending(),'unresolved transaction; no new order')
        positions=self.positions(); exposure=sum(p['cost'] for p in positions)
        cost=lamports(c['trade_sol'])+lamports(c['max_tx_cost_sol'])
        require(len(positions)<c['max_positions'],'position count limit')
        require(exposure+cost<=lamports(c['max_open_cost_sol']),'open exposure limit')
        gross=self.db.execute("SELECT COALESCE(SUM(sol),0) FROM fills WHERE side='buy'").fetchone()[0]
        require(gross+cost<=lamports(c['max_total_buy_sol']),'lifetime buy budget exhausted')
        midnight=int(time.time()//86400)*86400
        # Realized LOSSES (not net profits) + all open principal + proposed cost.
        losses=self.db.execute('SELECT COALESCE(SUM(-pnl),0) FROM fills WHERE pnl<0 AND ts>=?',(midnight,)).fetchone()[0]
        require(losses+exposure+cost<=lamports(c['max_daily_risk_sol']),'daily worst-case risk budget')
    def fill(self, order, native, raw):
        require((order['side']=='buy' and native<0 and raw>0) or (order['side']=='sell' and native>0 and raw<0),'unexpected fill deltas; manual review required')
        with self.db:
            require(self.db.execute('SELECT status FROM orders WHERE id=?',(order['id'],)).fetchone()[0]=='pending','order already settled')
            pnl=0
            if order['side']=='buy':
                require(not self.db.execute('SELECT 1 FROM positions WHERE mint=?',(order['mint'],)).fetchone(),'position already exists')
                self.db.execute('INSERT INTO positions VALUES (?,?,?,?,?)',(order['mint'],str(raw),-native,time.time(),-native))
            else:
                p=self.db.execute('SELECT * FROM positions WHERE mint=?',(order['mint'],)).fetchone()
                require(p is not None and int(p['raw'])==-raw,'partial/mismatched exit requires manual review')
                pnl=native-p['cost']
                self.db.execute('DELETE FROM positions WHERE mint=?',(order['mint'],))
            self.db.execute('INSERT INTO fills(order_id,mint,side,sol,raw,pnl,ts) VALUES (?,?,?,?,?,?,?)',
                (order['id'],order['mint'],order['side'],abs(native),str(abs(raw)),pnl,time.time()))
            self.db.execute("UPDATE orders SET status='filled' WHERE id=?",(order['id'],))
            if self.get('mode')=='paper': self.set('cash',str(int(self.get('cash'))+native))

class Bot:
    def __init__(self,c,store,providers,key=None,control_dir=Path('.')):
        self.c,self.s,self.p,self.key,self.control_dir=c,store,providers,key,Path(control_dir)
        self.wallet=store.get('wallet')
    def pause_entries(self): return (self.control_dir/'PAUSE').exists()
    def reconcile(self):
        for o in self.s.pending():
            tx=self.p.transaction(o['signature'])
            if tx is None:
                self.s.audit('pending',{'order_id':o['id'],'signature':o['signature']}); continue
            if tx['meta']['err'] is not None:
                fee=integer(tx['meta']['fee'])
                with self.s.db:
                    self.s.db.execute("UPDATE orders SET status='failed' WHERE id=?",(o['id'],))
                    self.s.db.execute('INSERT INTO fills(order_id,mint,side,sol,raw,pnl,ts) VALUES (?,?,?,?,?,?,?)',
                                      (o['id'],o['mint'],'failed',fee,'0',-fee,time.time()))
                self.s.audit('failed_on_chain',{'order_id':o['id']}); continue
            native,delta=wallet_deltas(tx,self.wallet)
            require(set(delta)=={o['mint']},'unexpected assets changed; manual review required')
            raw=delta[o['mint']]
            if o['side']=='buy':
                require(-native<=int(o['amount'])+lamports(self.c['max_tx_cost_sol']),'actual spending exceeded budget')
                require(raw>=int(o['min_out']),'actual output below expected minimum')
            else:
                require(-raw==int(o['amount']),'actual sell input mismatch')
                require(native>=int(o['min_out'])-lamports(self.c['max_tx_cost_sol']),'actual SOL output mismatch')
            self.s.fill(o,native,raw)
            self.s.audit('filled',{'order_id':o['id'],'side':o['side'],'mint':o['mint'],'signature':o['signature']})
    def scan(self):
        cutoff=time.time()-self.c['signal_window_seconds']
        for w in self.c['watch_wallets']:
            for row in self.p.wallet_history(w):
                ts=row.get('blockTime')
                if ts is None: continue
                if ts<cutoff: break
                if row.get('err') is not None: continue
                sig=row['signature']
                if self.s.db.execute('SELECT 1 FROM seen WHERE sig=? AND wallet=?',(sig,w)).fetchone(): continue
                tx=self.p.transaction(sig)
                if tx is None: raise Blocked('finalized wallet transaction unavailable')
                e=buyer_event(tx,w)
                with self.s.db:
                    if e: self.s.db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)',(sig,w,e['mint'],e['side'],e['raw'],ts))
                    self.s.db.execute('INSERT OR IGNORE INTO seen VALUES (?,?)',(sig,w))
                if e: self.s.audit('wallet_'+e['side'],{'wallet':w,**e})
        rows=self.s.rows('SELECT * FROM events WHERE ts>=? ORDER BY ts',(cutoff,))
        # Count each watched wallet once, and remove wallets whose latest action is a sell.
        latest={}
        for r in rows: latest[(r['mint'],r['wallet'])]=r
        buyers={}
        for (mint,w),r in latest.items():
            if r['side']=='buy': buyers.setdefault(mint,[]).append(w)
        return {m:ws for m,ws in buyers.items() if len(ws)>=self.c['min_buyers']}
    def dev_check(self,mint):
        wallets=self.c['developer_wallets'].get(mint,[])
        require(wallets or not self.c['require_known_developer'],'developer unknown; add independently verified mapping')
        cutoff=time.time()-self.c['signal_window_seconds']
        for w in wallets:
            for row in self.p.wallet_history(w):
                if (row.get('blockTime') or 0)<cutoff: break
                if row.get('err') is not None: continue
                tx=self.p.transaction(row['signature'])
                require(tx is not None,'developer history incomplete')
                _,delta=wallet_deltas(tx,w)
                require(delta.get(mint,0)>=0,'developer token outflow detected')
        return wallets
    def sign_checked(self,q,mint,side,amount):
        require(time.time()-q['_received']<=30,'stale order')
        require(q.get('taker')==self.wallet and q.get('gasless') is False,'unsupported taker/gasless order')
        require(q.get('requestId') and q.get('transaction'),'missing transaction')
        require(integer(q['lastValidBlockHeight'])>self.p.rpc('getBlockHeight',[{'commitment':'confirmed'}]),'expired blockhash')
        tx=VersionedTransaction.from_bytes(base64.b64decode(q['transaction'],validate=True))
        msg=tx.message
        require(msg.header.num_required_signatures==1 and str(msg.account_keys[0])==self.wallet,'unexpected transaction signer')
        # Programs in compiled instructions must be static keys; reject unknown top-level programs.
        programs=[]
        for ix in msg.instructions:
            require(ix.program_id_index<len(msg.account_keys),'dynamic program id unsupported')
            prog=str(msg.account_keys[ix.program_id_index]); programs.append(prog)
            require(prog in {SYSTEM,TOKEN,ATA,COMPUTE,JUP},'unexpected instruction program')
            data=bytes(ix.data)
            if prog==TOKEN: require(data and data[0] in (9,17),'unexpected standalone token instruction')
        require(JUP in programs,'missing Jupiter swap instruction')
        accounts=self.p.token_accounts(self.wallet)
        target=str(Pubkey.find_program_address([bytes(Pubkey.from_string(self.wallet)),bytes(Pubkey.from_string(TOKEN)),bytes(Pubkey.from_string(mint))],Pubkey.from_string(ATA))[0])
        tracked=[self.wallet]+[a['pubkey'] for a in accounts]
        if target not in tracked: tracked.append(target)
        require(len(tracked)<=60,'too many accounts for transaction review')
        pre={a['pubkey']:a['account'] for a in accounts}
        before_sol=self.p.balance(self.wallet)
        signed=VersionedTransaction.populate(msg,[self.key.sign_message(to_bytes_versioned(msg))])
        encoded=base64.b64encode(bytes(signed)).decode()
        sim=self.p.rpc('simulateTransaction',[encoded,{'encoding':'base64','commitment':'confirmed','sigVerify':True,
              'accounts':{'encoding':'jsonParsed','addresses':tracked}}])['value']
        require(sim.get('err') is None,'transaction simulation failed')
        after=sim.get('accounts'); require(after and len(after)==len(tracked),'simulation account review unavailable')
        require(after[0] is not None and after[0]['owner']==SYSTEM,'wallet account changed')
        cost_cap=lamports(self.c['max_tx_cost_sol'])
        if side=='buy':
            require(before_sol-after[0]['lamports']<=amount+cost_cap,'simulation SOL spend exceeds cap')
        else:
            require(after[0]['lamports']-before_sol>=integer(q['otherAmountThreshold'])-cost_cap,'simulation sell proceeds below limit')
        target_post=after[tracked.index(target)]
        if side=='buy':
            require(target_post is not None and target_post['owner']==TOKEN, 'missing output token account')
            received=target_post['data']['parsed']['info']
            require(received.get('owner')==self.wallet and received.get('mint')==mint and received.get('delegate') is None and received.get('closeAuthority') in (None,self.wallet) and received.get('state')=='initialized', 'unsafe output token account')
            before_raw=integer(pre[target]['data']['parsed']['info']['tokenAmount']['amount']) if target in pre else 0
            require(integer(received['tokenAmount']['amount'])-before_raw>=integer(q['otherAmountThreshold']), 'simulation output below minimum')
        for key,post in zip(tracked[1:],after[1:]):
            if key not in pre: continue
            old=pre[key]['data']['parsed']['info']; old_amount=integer(old['tokenAmount']['amount'])
            if post is None:
                require(side=='sell' and old['mint']==mint and old_amount==amount,'unexpected token account closure')
                continue
            require(post['owner']==TOKEN and isinstance(post.get('data'),dict),'unexpected token account format')
            new=post['data']['parsed']['info']
            require(new.get('owner')==old.get('owner') and new.get('mint')==old.get('mint'),'token ownership changed')
            for f in ('delegate','delegatedAmount','closeAuthority','state'):
                require(new.get(f)==old.get(f),'token authority or state changed')
            decrease=old_amount-integer(new['tokenAmount']['amount'])
            require(decrease<=0 or (side=='sell' and old['mint']==mint and decrease<=amount),'unexpected token spending')
        require(time.time()-q['_received']<=45,'order stale after validation')
        return encoded,str(signed.signatures[0])
    def execution_fence(self):
        pass

    def trade(self,mint,side,amount,reason):
        require(not self.s.pending(),'unresolved transaction blocks submissions')
        require(not (self.control_dir/'HALT').exists(),'HALT active')
        require(side in ('buy','sell') and isinstance(amount,int) and amount>0,'invalid trade intent')
        if side=='buy':
            require(amount==lamports(self.c['trade_sol']),'trade size differs from configured amount')
            require(not self.s.db.execute('SELECT 1 FROM positions WHERE mint=?',(mint,)).fetchone(),'already holding this mint')
            require(not self.pause_entries(),'entries paused'); self.s.entry_limits(self.c)
        else:
            position=self.s.db.execute('SELECT raw FROM positions WHERE mint=?',(mint,)).fetchone()
            require(position is not None and int(position['raw'])==amount,'sell must match managed position')
        input_mint,output_mint=(SOL,mint) if side=='buy' else (mint,SOL)
        q=self.p.quote(input_mint,output_mint,amount,self.wallet if self.c['mode']=='live' else None)
        if side=='buy':
            reverse=self.p.quote(mint,SOL,integer(q['otherAmountThreshold']))
            require(integer(reverse['otherAmountThreshold'])>=amount*(1-self.c['max_roundtrip_loss_fraction']),'roundtrip quote loss too high')
        cap=lamports(self.c['max_tx_cost_sol'])
        balance=self.p.balance(self.wallet) if self.c['mode']=='live' else int(self.s.get('cash'))
        require(balance>=(amount if side=='buy' else 0)+cap+lamports(self.c['reserve_sol']),'SOL reserve limit')
        signed=sig=None
        if self.c['mode']=='live': signed,sig=self.sign_checked(q,mint,side,amount)
        # Check control files again immediately before durable submission intent.
        require(not (self.control_dir/'HALT').exists(),'HALT active')
        require(side!='buy' or not self.pause_entries(),'entries paused')
        self.execution_fence()
        with self.s.db:
            cur=self.s.db.execute('INSERT INTO orders(mint,side,amount,min_out,signature,status,ts,reason) VALUES (?,?,?,?,?,?,?,?)',
                 (mint,side,str(amount),q['otherAmountThreshold'],sig,'pending',time.time(),reason))
            oid=cur.lastrowid
        o=self.s.rows('SELECT * FROM orders WHERE id=?',(oid,))[0]
        if self.c['mode']=='paper':
            # Quote threshold, not optimistic mid-price, with full configured cost allowance.
            out=integer(q['otherAmountThreshold'])
            self.s.fill(o,-amount-cap if side=='buy' else out-cap,out if side=='buy' else -amount)
            self.s.audit('paper_fill',{'order_id':oid,'mint':mint,'side':side})
        else:
            self.s.audit('submitting',{'order_id':oid,'signature':sig,'side':side,'mint':mint})
            try: self.p.execute(signed,q)
            except Blocked: self.s.audit('submission_uncertain',{'order_id':oid,'signature':sig})
            # API success isn't a ledger fill; finalized chain evidence is authoritative.
            self.reconcile()
    def exits(self):
        for p in self.s.positions():
            try:
                q=self.p.quote(p['mint'],SOL,int(p['raw']))
                net=max(0,integer(q['otherAmountThreshold'])-lamports(self.c['max_tx_cost_sol']))
                high=max(p['high'],net)
                with self.s.db: self.s.db.execute('UPDATE positions SET high=? WHERE mint=?',(high,p['mint']))
                reason=None
                if net<=p['cost']*(1-self.c['stop_loss_fraction']): reason='stop_loss'
                elif net>=p['cost']*(1+self.c['take_profit_fraction']): reason='take_profit'
                elif high>p['cost'] and net<=high*(1-self.c['trailing_fraction']): reason='trailing_exit'
                elif time.time()-p['opened']>=self.c['max_hold_seconds']: reason='time_exit'
                if (self.control_dir/'EXIT_ALL').exists() or (self.control_dir/('CLOSE_'+p['mint'])).exists(): reason='operator_exit'
                if not reason:
                    try: self.dev_check(p['mint'])
                    except Blocked as e:
                        if str(e)=='developer token outflow detected': reason='developer_outflow'
                        else: raise
                if reason: self.trade(p['mint'],'sell',int(p['raw']),reason)
            except (Blocked,KeyError,ValueError,TypeError) as e:
                self.s.audit('exit_blocked',{'mint':p['mint'],'reason':str(e) if isinstance(e,Blocked) else type(e).__name__})
                # No fresh entries while an existing position cannot be assessed/exited.
                raise Blocked('position monitoring failed') from None
    def recover_paper(self):
        require(self.c['mode']=='paper','paper recovery cannot handle live orders')
        self.execution_fence()
        for o in self.s.pending():
            require(o['signature'] is None,'signed order in paper ledger; manual review required')
            amount=integer(o['amount']);out=integer(o['min_out']);cap=lamports(self.c['max_tx_cost_sol'])
            self.s.fill(o,-amount-cap if o['side']=='buy' else out-cap,out if o['side']=='buy' else -amount)
            self.s.audit('paper_recovered',{'order_id':o['id']})

    def cycle(self):
        if self.c['mode']=='paper' and self.s.pending(): self.recover_paper()
        if self.c['mode']=='live': self.reconcile()
        require(not self.s.pending(),'pending transaction needs reconciliation')
        if (self.control_dir/'HALT').exists(): return
        self.exits()
        if self.pause_entries() or (self.control_dir/'EXIT_ALL').exists(): return
        candidates=self.scan()
        for mint,buyers in candidates.items():
            previous=self.s.db.execute('SELECT MAX(ts) FROM orders WHERE mint=?',(mint,)).fetchone()[0]
            if previous is not None:
                cooldown=self.c.get('reentry_cooldown_seconds')
                if cooldown is None or time.time()-previous<cooldown:continue
                if self.s.db.execute('SELECT 1 FROM positions WHERE mint=?',(mint,)).fetchone():continue
                last_exit=self.s.db.execute("SELECT MAX(ts) FROM fills WHERE mint=? AND side='sell'",(mint,)).fetchone()[0]
                if last_exit is None:continue
                fresh=self.s.db.execute("SELECT COUNT(DISTINCT wallet) FROM events WHERE mint=? AND side='buy' AND ts>?",(mint,last_exit)).fetchone()[0]
                if fresh<self.c['min_buyers']:continue
            try:
                self.s.entry_limits(self.c)
                self.dev_check(mint)
                evidence=self.p.token_check(mint)
                # Scanning can take time: require still-fresh buyer evidence at decision time.
                n=self.s.db.execute("SELECT COUNT(DISTINCT wallet) FROM events WHERE mint=? AND side='buy' AND ts>=?",
                        (mint,time.time()-self.c['signal_window_seconds'])).fetchone()[0]
                require(n>=self.c['min_buyers'],'buyer signal stale')
                self.s.audit('candidate_passed',{'mint':mint,'buyers':buyers,'checks':evidence})
                self.trade(mint,'buy',lamports(self.c['trade_sol']),'watched_wallet_accumulation')
            except (Blocked,KeyError,ValueError,TypeError) as e:
                self.s.audit('entry_rejected',{'mint':mint,'reason':str(e) if isinstance(e,Blocked) else type(e).__name__})

@contextlib.contextmanager
def worker_lock(path):
    # Cross-platform kernel lock; automatically released on crash, no stale lock deletion.
    f=open(path,'a+b'); f.seek(0); f.write(b'0'); f.flush(); f.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        f.close(); raise Blocked('another worker holds this database lock') from None
    try: yield
    finally: f.close()

def wallet_create(path):
    key=Keypair(); payload=bytes(key)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f: json.dump(list(payload),f)
    print('New dedicated wallet PUBLIC address:',key.pubkey())
    print('Private key saved locally. Do not upload it or put it in chat.')

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['run','once','status','pause','resume','halt','unhalt','exit-all','wallet-create','review'])
    parser.add_argument('--config',default='config.json'); parser.add_argument('--db',default='state.sqlite')
    parser.add_argument('--keyfile',default='trading-wallet.json'); parser.add_argument('--live',action='store_true')
    a=parser.parse_args(); controls=Path(a.db).resolve().parent
    if a.command=='wallet-create': wallet_create(a.keyfile); return
    flags={'pause':('PAUSE',True),'resume':('PAUSE',False),'halt':('HALT',True),'unhalt':('HALT',False),'exit-all':('EXIT_ALL',True)}
    if a.command in flags:
        flag,enabled=flags[a.command]; p=controls/flag
        if enabled: p.touch()
        else: p.unlink(missing_ok=True)
        print(flag, 'enabled' if enabled else 'cleared'); return
    if a.command in ('status','review'):
        require(Path(a.db).exists(),'database not created yet')
        db=sqlite3.connect('file:'+str(Path(a.db).resolve())+'?mode=ro',uri=True); db.row_factory=sqlite3.Row
        tables=['positions','orders','fills'] if a.command=='status' else ['fills','audit']
        for table in tables:
            print(table,json.dumps([dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY rowid DESC LIMIT 50')],indent=2))
        return
    c=load_config(a.config)
    key=None; wallet='paper'
    if c['mode']=='live':
        require(a.live and os.environ.get('ENABLE_LIVE_TRADING')=='YES','live requires --live and ENABLE_LIVE_TRADING=YES')
        require(os.environ.get('JUPITER_API_KEY'),'set JUPITER_API_KEY for live trading')
        path=Path(a.keyfile)
        if os.name!='nt': require(path.stat().st_mode & 0o077==0,'keyfile permissions must be 600')
        key=Keypair.from_bytes(bytes(json.loads(path.read_text())))
        wallet=str(key.pubkey())
        require(wallet==c.get('expected_wallet'),'expected_wallet must match dedicated keyfile')
        require(wallet not in c['watch_wallets'],'cannot watch own trading wallet')
    else: require(not a.live,'--live forbidden with paper config')
    with contextlib.ExitStack() as stack:
        stack.enter_context(worker_lock(str(Path(a.db).resolve())+'.lock'))
        if c['mode']=='live':
            locks=Path.home()/'.sentinel-meme-locks'; locks.mkdir(mode=0o700,exist_ok=True)
            stack.enter_context(worker_lock(locks/(wallet+'.lock')))
        s=Store(a.db,c,wallet); p=Providers(c); bot=Bot(c,s,p,key,controls)
        s.audit('started',{'mode':c['mode'],'wallet':wallet})
        try:
            while True:
                try: bot.cycle()
                except (Blocked,KeyError,ValueError,TypeError) as e:
                    s.audit('cycle_blocked',{'reason':str(e) if isinstance(e,Blocked) else type(e).__name__})
                if a.command=='once': break
                time.sleep(c['poll_seconds'])
        except KeyboardInterrupt: print('Worker stopped. Open positions are NOT automatically sold.')
        finally: s.db.close()

if __name__=='__main__':
    try: main()
    except (Blocked,FileNotFoundError,ValueError,KeyError) as e:
        print('STOP:',str(e) if isinstance(e,(Blocked,FileNotFoundError)) else type(e).__name__)
        raise SystemExit(1)
