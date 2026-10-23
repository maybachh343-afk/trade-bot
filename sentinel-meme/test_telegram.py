import json, os, sqlite3, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch
from bot import Store, Keypair, Blocked
from telegram_control import Control
from cloud import provision_wallet, CloudBot
from stream import WalletStream
from test_core import config, MINT, WALLETS, Fake

class FakeTelegram:
    def __init__(self):self.sent=[];self.updates=[];self.fail=False
    def send(self,chat,text):
        if self.fail:raise Blocked('network failed')
        self.sent.append((chat,text));return {'message_id':len(self.sent)}
    def call(self,method,data):return self.updates if method=='getUpdates' else True

class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.api=FakeTelegram();self.c=config()
        (self.root/'config.json').write_text(json.dumps(self.c));(self.root/'PAUSE').touch()
        (self.root/'wallet-public.json').write_text(json.dumps({'address':WALLETS[0]}))
        (self.root/'health.json').write_text(json.dumps({'status':'ready','at':time.time()}))
        self.ctl=Control(self.root,self.api,'x'*32);self.addCleanup(self.ctl.db.close);self.seq=0
    def send(self,text,user=42,age=0,kind='private',update_id=None):
        self.seq+=1
        u={'update_id':self.seq if update_id is None else update_id,'message':{'message_id':self.seq,'date':int(time.time())-age,'text':text,'chat':{'id':user,'type':kind},'from':{'id':user,'is_bot':False}}}
        self.ctl.handle(u);return u
    def pair(self):self.send('/pair '+'x'*32)
    def confirm(self):
        t=self.ctl.db.execute('SELECT token FROM challenges ORDER BY rowid DESC LIMIT 1').fetchone()[0]
        self.send('/confirm '+t);return t
    def test_pair_owner_persisted(self):
        self.pair();self.assertEqual(self.ctl.get('owner'),'42')
        other=Control(self.root,self.api,'x'*32);self.assertEqual(other.get('owner'),'42');other.db.close()
    def test_bad_pair_denied(self):self.send('/pair wrong');self.assertIsNone(self.ctl.get('owner'))
    def test_pair_cannot_be_taken_over(self):
        self.pair();self.send('/pair '+'x'*32,user=43);self.send('/halt',user=43)
        self.assertEqual(self.ctl.get('owner'),'42');self.assertFalse((self.root/'HALT').exists())
    def test_group_commands_rejected(self):
        self.pair();self.send('/halt',kind='group');self.assertFalse((self.root/'HALT').exists())
    def test_stale_resume_rejected(self):
        self.pair();self.send('/resume',age=600);self.assertEqual(self.ctl.db.execute('SELECT COUNT(*) FROM challenges').fetchone()[0],0)
    def test_resume_requires_fresh_single_use_confirmation(self):
        self.pair();self.send('/resume');self.assertTrue((self.root/'PAUSE').exists())
        t=self.confirm();self.assertFalse((self.root/'PAUSE').exists());self.send('/pause');self.send('/confirm '+t)
        self.assertTrue((self.root/'PAUSE').exists())
    def test_expired_confirmation_cannot_enable_entries(self):
        self.pair();self.send('/resume')
        with self.ctl.db:self.ctl.db.execute('UPDATE challenges SET expires=0')
        self.confirm();self.assertTrue((self.root/'PAUSE').exists())
    def test_update_replay_is_ignored(self):
        self.pair();self.send('/resume');u=self.send('/pause');self.ctl.handle(u)
        self.assertEqual(self.ctl.db.execute('SELECT COUNT(*) FROM outbox WHERE k=?',(f'command:{u["update_id"]}:42',)).fetchone()[0],1)
    def test_exitall_is_confirmed_and_pause_sticky(self):
        self.pair();self.send('/exit_all');self.assertFalse((self.root/'EXIT_ALL').exists())
        self.confirm();self.assertTrue((self.root/'EXIT_ALL').exists());self.assertTrue((self.root/'PAUSE').exists())
    def test_unhalt_keeps_entries_paused(self):
        self.pair();self.send('/halt');self.send('/unhalt')
        self.assertFalse((self.root/'HALT').exists());self.assertTrue((self.root/'PAUSE').exists())
    def test_watched_wallets_validated(self):
        self.pair();self.send('/watch garbage');self.assertEqual(self.ctl.config()['watch_wallets'],WALLETS)
        w=str(Keypair().pubkey());self.send('/watch '+w);self.assertIn(w,self.ctl.config()['watch_wallets'])
    def test_notification_retry_and_durable_dedup(self):
        self.pair();self.ctl.queue('fill:paper:1','PAPER BUY #1');self.api.fail=True
        with self.assertRaises(Blocked):self.ctl.flush()
        self.assertEqual(self.ctl.db.execute("SELECT sent FROM outbox WHERE k='fill:paper:1'").fetchone()[0],0)
        self.api.fail=False;self.ctl.flush();n=len(self.api.sent);self.ctl.flush();self.assertEqual(len(self.api.sent),n)
    def test_unknown_text_not_stored(self):
        self.pair();secret='accidental-key-123';self.send(secret)
        out=''.join(r[0] for r in self.ctl.db.execute('SELECT text FROM outbox'))
        self.assertNotIn(secret,out)
    def test_fill_alert_net_pnl_and_mode(self):
        s=Store(self.root/'paper.sqlite',self.c,'paper')
        with s.db:
            s.db.execute("INSERT INTO fills(order_id,mint,side,sol,raw,pnl,ts) VALUES (1,?,'sell',10000000,'10',2000000,?)",(MINT,time.time()))
        s.db.close();self.pair();self.ctl.collect();self.ctl.collect();self.ctl.flush()
        alerts=[t for _,t in self.api.sent if 'SELL #1' in t]
        self.assertEqual(len(alerts),1);self.assertIn('+0.002000',alerts[0]);self.assertIn('PAPER',alerts[0])
    def test_pending_blocks_resume(self):
        s=Store(self.root/'paper.sqlite',self.c,'paper')
        with s.db:s.db.execute("INSERT INTO orders(mint,status) VALUES (?,'pending')",(MINT,))
        s.db.close();self.pair();self.send('/resume');self.confirm();self.assertTrue((self.root/'PAUSE').exists())

class CloudTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
    def test_wallet_vault_survives_restart(self):
        with patch.dict(os.environ,{'BOT_MODE':'paper'}):
            one=provision_wallet(self.root,'k'*32);two=provision_wallet(self.root,'k'*32)
        self.assertEqual(one.pubkey(),two.pubkey());self.assertNotIn(bytes(one),(self.root/'wallet.enc').read_bytes())
    def test_wrong_vault_secret_fails(self):
        with patch.dict(os.environ,{'BOT_MODE':'paper'}):
            provision_wallet(self.root,'k'*32)
            with self.assertRaises(Exception):provision_wallet(self.root,'z'*32)
    def test_live_missing_vault_never_creates_replacement(self):
        with patch.dict(os.environ,{'BOT_MODE':'live'}):
            with self.assertRaises(Blocked):provision_wallet(self.root,'k'*32)
        self.assertFalse((self.root/'wallet.enc').exists())
    def test_telegram_failure_pauses_entries(self):
        c=config();s=Store(self.root/'paper.sqlite',c,'paper');self.addCleanup(s.db.close)
        b=CloudBot(c,s,Fake(),control_dir=self.root)
        self.assertTrue(b.pause_entries());(self.root/'telegram-alive').write_text(str(time.time()));self.assertFalse(b.pause_entries())
    def test_stream_needs_all_subscriptions_and_warmup(self):
        stream=WalletStream(config(),self.root,self.root/'db','wss://example.invalid')
        self.assertFalse(stream.ready());stream.connected=True;stream.pong=time.time()
        stream.subscriptions={i:w for i,w in enumerate(WALLETS)};self.assertTrue(stream.ready())
        stream.mark_failure();self.assertFalse(stream.ready())
    def test_stream_rejects_unrecognized_notification(self):
        stream=WalletStream(config(),self.root,self.root/'db','wss://example.invalid')
        class WS:
            closed=False
            def close(self):self.closed=True
        ws=WS();stream.on_message(ws,json.dumps({'method':'logsNotification','params':{'subscription':999,'result':{'value':{'signature':'s','err':None}}}}))
        self.assertTrue(ws.closed);self.assertFalse(stream.ready())
    def test_daily_entry_attempt_limit(self):
        c=config();c['max_daily_entries']=1;s=Store(self.root/'paper.sqlite',c,'paper');self.addCleanup(s.db.close)
        with s.db:s.db.execute("INSERT INTO orders(mint,side,status,ts) VALUES (?,'buy','failed',?)",(MINT,time.time()))
        class Ready:
            def ready(self):return True
        b=CloudBot(c,s,Fake(),control_dir=self.root,stream=Ready())
        with self.assertRaisesRegex(Blocked,'daily entry attempt'):b.trade(MINT,'buy',5000000,'test')

if __name__=='__main__':unittest.main()
