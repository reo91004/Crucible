"""Small durable journal; every mutation commits before an external action."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, project):
        self.root = Path(project).resolve() / '.crucible'
        if self.root.is_symlink():
            raise ValueError('State directory must not be a symlink')
        if not (self.root / 'config.json').is_file() and not (self.root / 'state.sqlite').is_file():
            raise ValueError('Project is not initialized; run crucible init first')
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / 'state.sqlite', timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY, attempt INTEGER, stage TEXT, status TEXT, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY, attempt INTEGER, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time TEXT, kind TEXT, data TEXT, acknowledged INTEGER DEFAULT 0);
        ''')
        self.db.commit()

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, json.dumps(value)))

    def event(self, kind, data):
        with self.db:
            return self.db.execute('INSERT INTO events(time,kind,data) VALUES (?,?,?)', (now(), kind, json.dumps(data))).lastrowid

    def attempts(self):
        return [json.loads(r[0]) for r in self.db.execute('SELECT data FROM attempts ORDER BY id')]

    def save_attempt(self, data):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO attempts VALUES (?,?)', (data['id'], json.dumps(data)))

    def call(self, attempt, stage):
        row = self.db.execute('SELECT id,status,data FROM calls WHERE attempt=? AND stage=? ORDER BY id DESC LIMIT 1', (attempt, stage)).fetchone()
        return dict(row) | {'data': json.loads(row['data'])} if row else None

    def new_call(self, attempt, stage, data):
        with self.db:
            return self.db.execute('INSERT INTO calls(attempt,stage,status,data) VALUES (?,?,?,?)', (attempt, stage, 'running', json.dumps(data))).lastrowid

    def save_call(self, identifier, status, data):
        with self.db:
            self.db.execute('UPDATE calls SET status=?,data=? WHERE id=?', (status, json.dumps(data), identifier))

    def complete(self, attempt):
        decision = attempt['decision']
        with self.db:
            self.db.execute('UPDATE attempts SET data=? WHERE id=?', (json.dumps(attempt), attempt['id']))
            self.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', ('next_decision', json.dumps(decision)))
            self.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', ('last_error', 'null'))
            self.db.execute('INSERT INTO decisions(attempt,data) VALUES (?,?)', (attempt['id'], json.dumps(decision)))
            self.db.execute('INSERT INTO events(time,kind,data) VALUES (?,?,?)', (now(), 'decision', json.dumps({'attempt': attempt['id'], 'decision': decision})))
