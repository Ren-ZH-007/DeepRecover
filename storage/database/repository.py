import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

SCHEMA_VERSION = 1

def now():
    return datetime.now(timezone.utc).isoformat()

class Repository:
    """Each operation owns a connection; safe for worker-thread callers."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def migrate(self):
        with closing(self.connect()) as db, db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("数据库版本高于当前程序，请升级 DeepRecover")
            if version < 1:
                db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error_code TEXT)")
                db.execute("CREATE TABLE candidates(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), payload TEXT NOT NULL)")
                db.execute("CREATE INDEX candidate_task ON candidates(task_id)")
                db.execute("PRAGMA user_version=1")

    def create_task(self, kind):
        task_id = str(uuid4())
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,NULL)", (task_id, kind, "queued", now(), now()))
        return task_id

    def update_task(self, task_id, status, error_code=None):
        if status not in {"queued", "running", "completed", "cancelled", "failed", "interrupted"}:
            raise ValueError("Invalid task status")
        with closing(self.connect()) as db, db:
            db.execute("UPDATE tasks SET status=?, updated_at=?, error_code=? WHERE id=?", (status, now(), error_code, task_id))

    def recover_interrupted(self):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE tasks SET status='interrupted',updated_at=? WHERE status IN ('running','queued')", (now(),))

    def tasks(self):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 200")]

    def add_candidate(self, task_id, candidate):
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO candidates VALUES(?,?,?)", (candidate.id, task_id, json.dumps(asdict(candidate), ensure_ascii=False)))

    def add_candidates(self, task_id, candidates):
        with closing(self.connect()) as db, db:
            db.executemany("INSERT INTO candidates VALUES(?,?,?)",
                ((c.id, task_id, json.dumps(asdict(c), ensure_ascii=False)) for c in candidates))

    def candidates(self, task_id):
        with closing(self.connect()) as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM candidates WHERE task_id=?", (task_id,))]
