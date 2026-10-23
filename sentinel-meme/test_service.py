import http.client, json, os, tempfile, threading, time, unittest
from pathlib import Path
from unittest.mock import Mock, patch
from service import Dashboard
from coordination import Coordinator
from bot import Blocked, Providers

class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.server=Dashboard(('127.0.0.1',0),self.root,'p'*32,'https://sentinel.test',Mock(is_alive=lambda:True))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.cookie=''
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
    def request(self,path,method='GET',body=None,origin='https://sentinel.test'):
        conn=http.client.HTTPConnection(*self.server.server_address,timeout=2)
        conn.request(method,path,body,{'Origin':origin,'Cookie':self.cookie})
        r=conn.getresponse();data=r.read();headers=dict(r.getheaders());conn.close()
        return r.status,headers,data
    def login(self):
        code,headers,_=self.request('/login','POST','password='+'p'*32)
        self.assertEqual(code,303);self.cookie=headers['Set-Cookie'].split(';')[0];return headers
    def test_private_api_requires_auth(self):self.assertEqual(self.request('/api/status')[0],401)
    def test_secure_session_and_locked_live_mode(self):
        headers=self.login()
        for flag in ['Secure','HttpOnly','SameSite=Strict']:self.assertIn(flag,headers['Set-Cookie'])
        code,_,body=self.request('/api/status');self.assertEqual(code,200);self.assertFalse(json.loads(body)['live_enabled']);self.assertNotIn(b'p'*32,body)
    def test_cross_origin_requests_rejected(self):
        self.assertEqual(self.request('/login','POST','password='+'p'*32,origin='https://evil.test')[0],403)
        self.login();self.assertEqual(self.request('/api/pause','POST','{}',origin='https://evil.test')[0],403)
        self.assertFalse((self.root/'PAUSE').exists())
    def test_pause_resume_requires_fresh_worker(self):
        self.login();self.assertEqual(self.request('/api/pause','POST','{}')[0],200)
        self.assertTrue((self.root/'PAUSE').exists());self.assertEqual(self.request('/api/resume','POST','{}')[0],409)
        (self.root/'health.json').write_text(json.dumps({'at':time.time(),'status':'ready'}))
        self.assertEqual(self.request('/api/resume','POST','{}')[0],200);self.assertFalse((self.root/'PAUSE').exists())
    def test_logout_revokes_session(self):
        self.login();self.request('/api/logout','POST','{}');self.assertEqual(self.request('/api/status')[0],401)
    def test_expired_session_rejected(self):
        self.login();self.server.sessions[self.cookie.split('=')[1]]=0;self.assertEqual(self.request('/api/status')[0],401)
    def test_login_rate_limit(self):
        self.assertEqual(self.request('/login','POST','password=wrong')[0],401);self.assertEqual(self.request('/login','POST','password=wrong')[0],429)
    def test_dead_worker_fails_health(self):
        self.server.worker=Mock(is_alive=lambda:False);self.assertEqual(self.request('/healthz')[0],503)
    def test_invalid_watchlist_does_not_replace_config(self):
        from test_core import config
        c=config();(self.root/'config.json').write_text(json.dumps(c));self.login()
        self.assertEqual(self.request('/api/watchlist','POST',json.dumps({'watch_wallets':['bad'],'developer_wallets':{}}))[0],400)
        self.assertEqual(json.loads((self.root/'config.json').read_text()),c)
    def test_persistent_ledger_visible_after_restart(self):
        from bot import Store
        from test_core import config
        s=Store(self.root/'paper.sqlite',config(),'paper');s.audit('recovery_test',{'ok':True});s.db.close()
        self.login();data=json.loads(self.request('/api/status')[2]);self.assertEqual(data['audit'][0]['event'],'recovery_test')

class FakeDatabase:
    def __init__(self):self.owner=None;self.lock=None
    def connect(self,*args,**kwargs):return FakeConnection(self)
class FakeConnection:
    def __init__(self,state):self.state=state;self.value=None;self.failed=False
    def execute(self,sql,args=None):
        if self.failed:raise OSError('secret-url-must-not-leak')
        if sql.startswith('INSERT'):self.state.owner=self.state.owner or args[0]
        elif sql.startswith('SELECT owner'):self.value=(self.state.owner,)
        elif 'pg_try_advisory_lock' in sql:
            self.value=(self.state.lock in (None,self),)
            if self.value[0]:self.state.lock=self
        else:self.value=(1,)
        return self
    def fetchone(self):return self.value
    def close(self):
        if self.state.lock is self:self.state.lock=None

class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.db=FakeDatabase()
        self.env=patch.dict(os.environ,{'COORDINATOR_DATABASE_URL':'postgresql://fixture'});self.env.start();self.addCleanup(self.env.stop)
    def coordinator(self,name):
        root=self.root/name;root.mkdir(exist_ok=True);return Coordinator(root,self.db.connect)
    def test_two_hosts_only_first_owns_even_after_disconnect(self):
        a=self.coordinator('render');b=self.coordinator('pc');a.check()
        with self.assertRaisesRegex(Blocked,'Standby'):b.check()
        a.close()
        with self.assertRaisesRegex(Blocked,'Standby'):b.check()
        a.check();a.close()
    def test_same_ledger_clone_cannot_acquire_second_session(self):
        a=self.coordinator('render');b=self.coordinator('render');a.check()
        with self.assertRaisesRegex(Blocked,'session already active'):b.check()
        a.close()
    def test_connection_loss_blocks_and_sanitizes_error(self):
        a=self.coordinator('render');a.check();a.db.failed=True
        with self.assertRaisesRegex(Blocked,'Coordinator unavailable') as exc:a.check()
        self.assertNotIn('secret',str(exc.exception));self.assertIsNone(a.db)
    def test_identity_survives_restart(self):self.assertEqual(self.coordinator('pc').owner,self.coordinator('pc').owner)
    def test_missing_coordinator_blocks(self):
        with patch.dict(os.environ,{'COORDINATOR_DATABASE_URL':''}):
            with self.assertRaises(Blocked):self.coordinator('pc').check()
    def test_real_execution_transport_is_disabled(self):
        p=object.__new__(Providers)
        with self.assertRaisesRegex(Blocked,'Live execution disabled'):p.execute({},'signed')


class RecoveryTests(unittest.TestCase):
    def test_crash_after_intent_recovers_once_with_same_cash(self):
        from bot import Bot, Store
        from test_core import config, Fake, MINT
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);c=config();path=root/'paper.sqlite'
            s=Store(path,c,'paper')
            with s.db:s.db.execute("INSERT INTO orders(mint,side,amount,min_out,status,ts,reason) VALUES (?,'buy','5000000','100','pending',?,'crash fixture')",(MINT,time.time()))
            s.db.close()
            s=Store(path,c,'paper');bot=Bot(c,s,Fake(),control_dir=root)
            bot.recover_paper();cash=s.get('cash');bot.recover_paper()
            self.assertEqual(len(s.rows('SELECT * FROM fills')),1);self.assertEqual(len(s.positions()),1);self.assertEqual(s.get('cash'),cash);self.assertFalse(s.pending());s.db.close()
    def test_failed_coordinator_prevents_recovery(self):
        from bot import Store
        from cloud import CloudBot
        from test_core import config, Fake
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);c=config();s=Store(root/'paper.sqlite',c,'paper')
            guard=Mock();guard.check.side_effect=Blocked('offline')
            bot=CloudBot(c,s,Fake(),control_dir=root,coordinator=guard)
            with self.assertRaises(Blocked):bot.recover_paper()
            s.db.close()

if __name__=='__main__':unittest.main()
