"""Browser-independent worker and authenticated dashboard in one supervised process."""
import hashlib
import hmac
import json
import os
import secrets
import signal
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
import cloud

class Dashboard(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, root, password, origin, worker):
        if len(password) < 32: raise ValueError('DASHBOARD_PASSWORD must contain at least 32 characters')
        if not origin.startswith('https://') or origin.endswith('/'): raise ValueError('DASHBOARD_ORIGIN must be the exact HTTPS origin without a trailing slash')
        self.root, self.origin, self.worker = Path(root), origin, worker
        self.started=time.time()
        self.password = hashlib.sha256(password.encode()).digest()
        self.sessions = {}
        self.login_lock = threading.Lock()
        self.next_login = 0
        super().__init__(address, Handler)

class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup(); self.connection.settimeout(10)
    def log_message(self, *_): pass
    def reply(self, code, value, kind='application/json', cookie=None):
        data = value.encode() if isinstance(value, str) else json.dumps(value).encode()
        self.send_response(code)
        for key, val in {'Content-Type':kind, 'Content-Length':str(len(data)), 'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff', 'X-Frame-Options':'DENY', 'Referrer-Policy':'no-referrer', 'Content-Security-Policy':"default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; form-action 'self'"}.items(): self.send_header(key,val)
        if cookie: self.send_header('Set-Cookie',cookie)
        self.end_headers(); self.wfile.write(data)
    def session(self):
        from http.cookies import SimpleCookie
        try:
            cookies=SimpleCookie(self.headers.get('Cookie',''))
            token=cookies['sentinel'].value
            expiry=self.server.sessions.get(token,0)
            return token if expiry>time.time() else None
        except Exception: return None
    def do_GET(self):
        if self.path=='/healthz':
            alive=self.server.worker.is_alive()
            try: alive=alive and time.time()-json.loads((self.server.root/'health.json').read_text())['at']<600
            except (OSError,ValueError,KeyError): alive=alive and time.time()-self.server.started<90
            return self.reply(200 if alive else 503, {'worker_alive':alive})
        if self.path=='/':
            page='dashboard.html' if self.session() else 'login.html'
            return self.reply(200,Path(__file__).with_name(page).read_text(),'text/html; charset=utf-8')
        if not self.session(): return self.reply(401,{'error':'Sign in required'})
        if self.path=='/app.js': return self.reply(200,Path(__file__).with_name('app.js').read_text(),'text/javascript')
        if self.path!='/api/status': return self.reply(404,{})
        root=self.server.root
        result={'mode':'paper','live_enabled':False,'paused':(root/'PAUSE').exists(),'health':{'status':'starting'},'positions':[],'fills':[],'audit':[]}
        try: result['health']=json.loads((root/'health.json').read_text())
        except (OSError,ValueError): pass
        result['health']['stale']=time.time()-result['health'].get('at',0)>120
        for name in ['market','config']:
            try: result[name]=json.loads((root/(name+'.json')).read_text())
            except (OSError,ValueError): result[name]={}
        ledger=root/'paper.sqlite'
        if ledger.exists():
            try:
                with sqlite3.connect(f'{ledger.as_uri()}?mode=ro',uri=True,timeout=2) as db:
                    db.row_factory=sqlite3.Row
                    for table in ['positions','fills','audit']:
                        result[table]=[dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY rowid DESC LIMIT 100')]
                    result['cash_lamports']=db.execute("SELECT v FROM meta WHERE k='cash'").fetchone()[0]
            except sqlite3.Error: result['ledger_error']='Ledger unavailable'
        return self.reply(200,result)
    def do_POST(self):
        if self.headers.get('Origin')!=self.server.origin: return self.reply(403,{'error':'Origin rejected'})
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=8192: return self.reply(413,{})
            body=self.rfile.read(size)
        except (ValueError,TimeoutError): return self.reply(400,{})
        if self.path=='/login':
            with self.server.login_lock:
                now=time.time()
                if now<self.server.next_login: return self.reply(429,{'error':'Wait before retrying'})
                self.server.next_login=now+2
                password=parse_qs(body.decode()).get('password',[''])[0]
                if not hmac.compare_digest(hashlib.sha256(password.encode()).digest(),self.server.password): return self.reply(401,{'error':'Invalid password'})
                self.server.sessions={k:v for k,v in self.server.sessions.items() if v>now}
                token=secrets.token_urlsafe(32); self.server.sessions[token]=now+8*3600
            self.send_response(303); self.send_header('Location','/'); self.send_header('Set-Cookie',f'sentinel={token}; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800'); self.end_headers(); return
        token=self.session()
        if not token: return self.reply(401,{})
        root=self.server.root
        if self.path=='/api/logout':
            self.server.sessions.pop(token,None); return self.reply(200,{'ok':True},cookie='sentinel=; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
        if self.path=='/api/watchlist':
            try:
                data=json.loads(body)
                c=json.loads((root/'config.json').read_text())
                c['watch_wallets']=data['watch_wallets'];c['developer_wallets']=data['developer_wallets']
                import tempfile
                from bot import load_config, Blocked
                with tempfile.NamedTemporaryFile(mode='w',dir=root,delete=False) as f:
                    json.dump(c,f); tmp=Path(f.name)
                try: load_config(tmp)
                finally: tmp.unlink(missing_ok=True)
                cloud.atomic_json(root/'config.json',c)
            except Exception: return self.reply(400,{'error':'Invalid watchlist or developer mapping; use valid Solana addresses and minimum buyer count'})
        elif self.path=='/api/pause': (root/'PAUSE').touch()
        elif self.path=='/api/resume':
            try:
                health=json.loads((root/'health.json').read_text())
                if health['status']!='ready' or time.time()-health['at']>30: return self.reply(409,{'error':'Worker is not ready'})
            except (OSError,ValueError,KeyError): return self.reply(409,{'error':'Worker is not ready'})
            (root/'PAUSE').unlink(missing_ok=True)
        else: return self.reply(404,{})
        return self.reply(200,{'ok':True})

def main():
    root=Path(os.environ.get('DATA_DIR','/var/data')).resolve(); root.mkdir(parents=True,exist_ok=True)
    worker=threading.Thread(target=cloud.serve,args=(root,),daemon=True)
    server=Dashboard((os.environ.get('BIND_HOST','127.0.0.1'),int(os.environ.get('PORT','8765'))),root,
                     os.environ.get('DASHBOARD_PASSWORD',''),os.environ.get('DASHBOARD_ORIGIN') or os.environ.get('RENDER_EXTERNAL_URL',''),worker)
    for sig in (signal.SIGTERM,signal.SIGINT):
        signal.signal(sig,lambda *_:(cloud.STOP.set(),threading.Thread(target=server.shutdown,daemon=True).start()))
    from market import MarketFeeds
    feeds=MarketFeeds(root,cloud.STOP);feeds.start()
    worker.start()
    try: server.serve_forever()
    finally: cloud.STOP.set(); server.server_close(); feeds.close(); worker.join(timeout=25)

if __name__=='__main__': main()
