"""Fail-closed, pinned ledger ownership across hosts. Never auto-promotes standby."""
import os
import uuid
from pathlib import Path
from bot import Blocked

class Coordinator:
    def __init__(self, root, connect=None):
        path = Path(root) / 'deployment-id'
        if not path.exists():
            try:
                with path.open('x') as f: f.write(str(uuid.uuid4()))
            except FileExistsError: pass
        self.owner = path.read_text().strip()
        uuid.UUID(self.owner)
        self.connect = connect
        self.db = None

    def close(self):
        if self.db:
            try: self.db.close()
            finally: self.db = None

    def check(self):
        """Connection-level lock plus permanent owner pin; outages never grant ownership."""
        try:
            if self.db is None:
                url = os.environ.get('COORDINATOR_DATABASE_URL', '')
                if not url: raise Blocked('Shared coordinator is required; execution blocked')
                connect = self.connect
                if connect is None:
                    import psycopg
                    connect = psycopg.connect
                self.db = connect(url, autocommit=True, connect_timeout=5,
                                  options='-c statement_timeout=5000')
                self.db.execute('CREATE TABLE IF NOT EXISTS sentinel_owner (id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT NOT NULL)')
                self.db.execute('INSERT INTO sentinel_owner VALUES (1,%s) ON CONFLICT DO NOTHING', (self.owner,))
                if self.db.execute('SELECT owner FROM sentinel_owner WHERE id=1').fetchone()[0] != self.owner:
                    raise Blocked('Standby: another deployment owns the ledger; explicit migration required')
                if not self.db.execute('SELECT pg_try_advisory_lock(731946,1)').fetchone()[0]:
                    raise Blocked('Standby: execution session already active')
            # A dead connection is not silently reconnected in the same check.
            self.db.execute('SELECT 1').fetchone()
        except Exception as exc:
            self.close()
            if isinstance(exc, Blocked): raise
            raise Blocked('Coordinator unavailable; execution blocked') from None
