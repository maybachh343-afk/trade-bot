import base64, json, tempfile, time, unittest
from pathlib import Path
from solders.keypair import Keypair
from solders.hash import Hash
from solders.message import MessageV0
from solders.instruction import Instruction,AccountMeta
from solders.transaction import VersionedTransaction
from solders.pubkey import Pubkey
from bot import *

MINT=str(Keypair().pubkey()); WALLETS=[str(Keypair().pubkey()) for _ in range(3)]
def config():
    c=json.loads(Path('config.example.json').read_text()); c['watch_wallets']=WALLETS; c['trade_sol']=0.005
    c['developer_wallets']={MINT:[str(Keypair().pubkey())]}
    return c

def quote(i=SOL,o=MINT,n=5_000_000):
    return {'inputMint':i,'outputMint':o,'inAmount':str(n),'outAmount':'1000000','otherAmountThreshold':'990000',
            'swapMode':'ExactIn','slippageBps':100,'priceImpact':0.1,'router':'metis','_received':time.time()}

def tx(wallet=WALLETS[0],raw=100,native=-10000,program=JUP,err=None):
    return {'blockTime':int(time.time()),'transaction':{'message':{'accountKeys':[{'pubkey':wallet,'signer':True}],
             'instructions':[{'programId':program}]}},'meta':{'err':err,'fee':5000,'preBalances':[100000],
             'postBalances':[100000+native], 'preTokenBalances':[],
             'postTokenBalances':[{'owner':wallet,'mint':MINT,'uiTokenAmount':{'amount':str(raw)}}]}}

class Fake:
    def __init__(self): self.executions=0; self.quotes=0; self.tx=None; self.reverse_amount=4_900_000
    def quote(self,i,o,n,wallet=None):
        self.quotes+=1; q=quote(i,o,n)
        if o==SOL: q.update(outAmount=str(self.reverse_amount),otherAmountThreshold=str(self.reverse_amount))
        return q
    def balance(self,w): return 1_000_000_000
    def execute(self,*args): self.executions+=1; raise Blocked('timeout')
    def transaction(self,sig): return self.tx

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.c=config(); self.s=Store(self.root/'db',self.c,'paper')
        self.addCleanup(self.s.db.close); self.p=Fake(); self.b=Bot(self.c,self.s,self.p,control_dir=self.root)
    def test_config_rejects_empty_wallets(self):
        p=self.root/'c.json'; p.write_text(Path('config.example.json').read_text())
        with self.assertRaises(Blocked): load_config(p)
    def test_config_valid(self):
        p=self.root/'c.json'; p.write_text(json.dumps(self.c)); self.assertEqual(load_config(p),self.c)
    def test_quote_rejects_changed_mint(self):
        q=quote(); q['outputMint']=SOL
        with self.assertRaises(Blocked): validate_quote(q,SOL,MINT,5_000_000,self.c)
    def test_quote_rejects_nan_and_absent_impact(self):
        for value in [None,float('nan'),float('inf'),-5]:
            q=quote(); q['priceImpact']=value
            with self.assertRaises(Blocked): validate_quote(q,SOL,MINT,5_000_000,self.c)
    def test_quote_rejects_excess_slippage(self):
        q=quote(); q['otherAmountThreshold']='1'
        with self.assertRaises(Blocked): validate_quote(q,SOL,MINT,5_000_000,self.c)
    def test_transfer_is_not_a_buy(self): self.assertIsNone(buyer_event(tx(program=SYSTEM),WALLETS[0]))
    def test_failed_tx_not_a_buy(self): self.assertIsNone(buyer_event(tx(err='failed'),WALLETS[0]))
    def test_swap_classification(self): self.assertEqual(buyer_event(tx(),WALLETS[0])['side'],'buy')
    def test_buy_and_exit_accounting(self):
        self.b.trade(MINT,'buy',5_000_000,'test')
        self.assertEqual(self.s.positions()[0]['cost'],8_000_000)
        self.assertEqual(self.s.get('cash'),'992000000')
        self.p.reverse_amount=12_000_000
        self.b.trade(MINT,'sell',990000,'test')
        self.assertEqual(self.s.positions(),[]); self.assertEqual(self.s.get('cash'),'1001000000')
        self.assertEqual(self.s.rows('SELECT pnl FROM fills WHERE side="sell"')[0]['pnl'],1_000_000)
        self.assertEqual(self.p.executions,0)
    def test_no_duplicate_fill(self):
        self.b.trade(MINT,'buy',5_000_000,'test'); o=self.s.rows('SELECT * FROM orders')[0]
        with self.assertRaises(Blocked): self.s.fill(o,-5_000_000,100)
        self.assertEqual(len(self.s.positions()),1)
    def test_pause_blocks_buys_not_exits(self):
        self.b.trade(MINT,'buy',5_000_000,'test'); (self.root/'PAUSE').touch()
        with self.assertRaises(Blocked): self.b.trade(str(Keypair().pubkey()),'buy',5_000_000,'test')
        self.p.reverse_amount=12_000_000; self.b.trade(MINT,'sell',990000,'test')
    def test_halt_blocks_all(self):
        (self.root/'HALT').touch()
        with self.assertRaises(Blocked): self.b.trade(MINT,'buy',5_000_000,'test')
        self.assertEqual(self.p.quotes,0)
    def test_daily_risk_includes_open_principal(self):
        self.b.trade(MINT,'buy',5_000_000,'test'); self.c['max_daily_risk_sol']=0.015
        with self.assertRaises(Blocked): self.s.entry_limits(self.c)
    def test_lifetime_budget(self):
        self.c['max_total_buy_sol']=0.007
        with self.assertRaises(Blocked): self.s.entry_limits(self.c)
    def test_pending_blocks_duplicate_submission(self):
        with self.s.db: self.s.db.execute("INSERT INTO orders(mint,side,amount,min_out,signature,status,ts) VALUES (?,?,?,?,?,?,?)",(MINT,'buy','5000000','990000','sig','pending',time.time()))
        with self.assertRaises(Blocked): self.b.trade(MINT,'buy',5_000_000,'test')
        self.b.reconcile(); self.assertEqual(len(self.s.pending()),1); self.assertEqual(self.p.executions,0)
    def test_finalized_reconciliation(self):
        with self.s.db: self.s.db.execute("INSERT INTO orders(mint,side,amount,min_out,signature,status,ts) VALUES (?,?,?,?,?,?,?)",(MINT,'buy','5000000','990000','sig','pending',time.time()))
        self.p.tx=tx(wallet='paper',raw=1000000,native=-5005000)
        self.b.reconcile(); self.b.reconcile()
        self.assertEqual(len(self.s.rows('SELECT * FROM fills')),1); self.assertFalse(self.s.pending())
    def test_failed_transaction_fee_recorded(self):
        with self.s.db: self.s.db.execute("INSERT INTO orders(mint,side,amount,min_out,signature,status,ts) VALUES (?,?,?,?,?,?,?)",(MINT,'buy','5000000','990000','sig','pending',time.time()))
        self.p.tx=tx(err='failed'); self.b.reconcile()
        self.assertEqual(self.s.rows('SELECT pnl FROM fills')[0]['pnl'],-5000); self.assertFalse(self.s.positions())
    def test_unknown_developer_blocks(self):
        with self.assertRaises(Blocked): self.b.dev_check(str(Keypair().pubkey()))
    def test_changed_config_rejected(self):
        altered=dict(self.c,trade_sol=0.01)
        with self.assertRaises(Blocked): Store(self.root/'db',altered,'paper')
    def test_lock_excludes_other_workers(self):
        with worker_lock(self.root/'lock'):
            with self.assertRaises(Blocked):
                with worker_lock(self.root/'lock'): pass
    def test_time_exit(self):
        self.b.trade(MINT,'buy',5_000_000,'test'); self.p.reverse_amount=11_000_000
        with self.s.db: self.s.db.execute('UPDATE positions SET opened=?',(time.time()-7200,))
        self.b.exits(); self.assertFalse(self.s.positions())

class SigningTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.c=config(); self.c['mode']='live'; self.key=Keypair(); self.wallet=str(self.key.pubkey())
        self.s=Store(Path(self.tmp.name)/'db',self.c,self.wallet); self.addCleanup(self.s.db.close)
        self.p=Fake(); self.b=Bot(self.c,self.s,self.p,self.key,Path(self.tmp.name))
        self.target=str(Pubkey.find_program_address([bytes(self.key.pubkey()),bytes(Pubkey.from_string(TOKEN)),bytes(Pubkey.from_string(MINT))],Pubkey.from_string(ATA))[0])
        ix=Instruction(Pubkey.from_string(JUP),b'fixture',[AccountMeta(self.key.pubkey(),True,True)])
        msg=MessageV0.try_compile(self.key.pubkey(),[ix],[],Hash.default())
        self.q=quote(); self.q.update(transaction=base64.b64encode(bytes(VersionedTransaction(msg,[self.key]))).decode(),taker=self.wallet,gasless=False,requestId='r',lastValidBlockHeight='1000')
        self.p.token_accounts=lambda w: []
        def rpc(method,params):
            if method=='getBlockHeight': return 100
            if method=='simulateTransaction':
                return {'value':{'err':None,'accounts':[{'owner':SYSTEM,'lamports':994995000},{'owner':TOKEN,'data':{'parsed':{'info':{'owner':self.wallet,'mint':MINT,'state':'initialized','tokenAmount':{'amount':'1000000'}}}}}]}}
            raise AssertionError(method)
        self.p.rpc=rpc
    def test_real_signature_verifies(self):
        encoded,sig=self.b.sign_checked(self.q,MINT,'buy',5_000_000)
        t=VersionedTransaction.from_bytes(base64.b64decode(encoded)); self.assertTrue(all(t.verify_with_results()))
        self.assertEqual(str(t.signatures[0]),sig)
    def test_wrong_taker_rejected(self):
        self.q['taker']=WALLETS[0]
        with self.assertRaises(Blocked): self.b.sign_checked(self.q,MINT,'buy',5_000_000)
    def test_excess_native_spend_rejected(self):
        orig=self.p.rpc
        def rpc(m,p):
            r=orig(m,p)
            if m=='simulateTransaction': r['value']['accounts'][0]['lamports']=1
            return r
        self.p.rpc=rpc
        with self.assertRaises(Blocked): self.b.sign_checked(self.q,MINT,'buy',5_000_000)
    def test_missing_output_rejected(self):
        orig=self.p.rpc
        def rpc(m,p):
            r=orig(m,p)
            if m=='simulateTransaction': r['value']['accounts'][1]=None
            return r
        self.p.rpc=rpc
        with self.assertRaises(Blocked): self.b.sign_checked(self.q,MINT,'buy',5_000_000)
    def test_timeout_persists_intent_then_blocks_retry(self):
        self.p.quote=lambda *args: self.q if args[0]==SOL else dict(quote(MINT,SOL,990000),otherAmountThreshold='4900000')
        self.b.trade(MINT,'buy',5_000_000,'fixture')
        self.assertEqual(self.p.executions,1); self.assertEqual(len(self.s.pending()),1)
        with self.assertRaises(Blocked): self.b.trade(MINT,'buy',5_000_000,'fixture')
        self.assertEqual(self.p.executions,1)


class ScanTests(unittest.TestCase):
    setUp = Tests.setUp
    def history(self,actions):
        records={}; histories={w:[] for w in WALLETS}
        for idx,(w,side) in enumerate(actions):
            sig='sig'+str(idx); t=tx(wallet=w)
            if side=='sell':
                t['meta']['preTokenBalances']=t['meta']['postTokenBalances']; t['meta']['postTokenBalances']=[]
                t['meta']['postBalances']=[110000]
            records[sig]=t; histories[w].insert(0,{'signature':sig,'blockTime':int(time.time())-100+idx,'err':None})
        self.p.wallet_history=lambda w: histories.get(w,[])
        self.p.transaction=lambda sig: records[sig]
    def test_three_wallet_signal_and_deduplication(self):
        self.history([(w,'buy') for w in WALLETS]); self.assertIn(MINT,self.b.scan()); self.b.scan()
        self.assertEqual(len(self.s.rows('SELECT * FROM events')),3)
    def test_repeated_same_wallet_not_three_buyers(self):
        self.history([(WALLETS[0],'buy')]*3); self.assertEqual(self.b.scan(),{})
    def test_latest_sell_removes_buyer(self):
        self.history([(w,'buy') for w in WALLETS]+[(WALLETS[0],'sell')]); self.assertEqual(self.b.scan(),{})
    def test_full_paper_cycle_and_restart_no_duplicate(self):
        self.history([(w,'buy') for w in WALLETS]); self.p.token_check=lambda mint: {'test_fixture':True}
        self.b.cycle(); self.assertEqual(len(self.s.positions()),1)
        # Leave the position open and healthy on the next cycle.
        self.p.reverse_amount=11_000_000
        Bot(self.c,self.s,self.p,control_dir=self.root).cycle()
        self.assertEqual(len(self.s.rows('SELECT * FROM orders')),1)
    def test_developer_outflow_blocks(self):
        dev=self.c['developer_wallets'][MINT][0]; t=tx(wallet=dev)
        t['meta']['preTokenBalances']=t['meta']['postTokenBalances']; t['meta']['postTokenBalances']=[]
        self.p.wallet_history=lambda w:[{'signature':'devsig','blockTime':time.time(),'err':None}]
        self.p.transaction=lambda sig:t
        with self.assertRaisesRegex(Blocked,'developer token outflow'): self.b.dev_check(MINT)

class TokenCheckTests(unittest.TestCase):
    def setUp(self):
        self.p=object.__new__(Providers); self.p.c=config()
    def test_token2022_rejected(self):
        self.p.account=lambda m:{'owner':'Token2022'}
        with self.assertRaises(Blocked): self.p.token_check(MINT)
    def test_active_authority_rejected(self):
        self.p.account=lambda m:{'owner':TOKEN,'data':{'parsed':{'type':'mint','info':{'isInitialized':True,'mintAuthority':WALLETS[0],'freezeAuthority':None}}}}
        with self.assertRaisesRegex(Blocked,'authority'): self.p.token_check(MINT)

if __name__=='__main__': unittest.main()
