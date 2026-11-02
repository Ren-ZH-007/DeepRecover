import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from storage.models.classification import CATEGORIES

SCHEMA_VERSION = 3

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
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("数据库版本高于当前程序，请升级 DeepRecover")
            if version < 1:
                db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error_code TEXT)")
                db.execute("CREATE TABLE candidates(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), payload TEXT NOT NULL)")
                db.execute("CREATE INDEX candidate_task ON candidates(task_id)")
                db.execute("PRAGMA user_version=1")
            if version < 2:
                db.execute("CREATE TABLE sources(id TEXT PRIMARY KEY, kind TEXT NOT NULL, locator TEXT NOT NULL, label TEXT NOT NULL, created_at TEXT NOT NULL)")
                db.execute("ALTER TABLE tasks ADD COLUMN source_id TEXT REFERENCES sources(id)")
                db.execute("PRAGMA user_version=2")
            if version < 3:
                db.execute("ALTER TABLE tasks ADD COLUMN result_path TEXT")
                db.execute("CREATE TABLE exports(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), candidate_id TEXT NOT NULL REFERENCES candidates(id), status TEXT NOT NULL, output_path TEXT NOT NULL, source_sha256 TEXT, target_sha256 TEXT, size INTEGER NOT NULL, created_at TEXT NOT NULL, error_code TEXT, metadata TEXT NOT NULL)")
                db.execute("CREATE INDEX export_task ON exports(task_id)")
                db.execute("PRAGMA user_version=3")

    def create_task(self, kind, source=None):
        task_id = str(uuid4())
        with closing(self.connect()) as db, db:
            if source is not None:
                db.execute("INSERT INTO sources VALUES(?,?,?,?,?)", (source.id, source.kind, source.locator, source.label, source.created_at))
            db.execute("INSERT INTO tasks(id,kind,status,created_at,updated_at,source_id) VALUES(?,?,?,?,?,?)",
                       (task_id, kind, "queued", now(), now(), source.id if source else None))
        return task_id

    def update_task(self, task_id, status, error_code=None):
        if status not in {"queued", "running", "completed", "cancelled", "failed", "interrupted"}:
            raise ValueError("Invalid task status")
        with closing(self.connect()) as db, db:
            db.execute("UPDATE tasks SET status=?, updated_at=?, error_code=? WHERE id=?", (status, now(), error_code, task_id))

    def recover_interrupted(self):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE tasks SET status='interrupted',updated_at=? WHERE status IN ('running','queued')", (now(),))
            db.execute("UPDATE exports SET status='interrupted',error_code='Interrupted' WHERE status='running'")

    def tasks(self):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute("SELECT * FROM tasks WHERE kind!='image_preview' ORDER BY created_at DESC LIMIT 200")]

    def add_candidate(self, task_id, candidate):
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO candidates VALUES(?,?,?)", (candidate.id, task_id, json.dumps(asdict(candidate), ensure_ascii=False)))

    def add_candidates(self, task_id, candidates):
        with closing(self.connect()) as db, db:
            db.executemany("INSERT INTO candidates VALUES(?,?,?)",
                ((c.id, task_id, json.dumps(asdict(c), ensure_ascii=False)) for c in candidates))

    def candidates(self, task_id):
        with closing(self.connect()) as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM candidates WHERE task_id=? ORDER BY rowid", (task_id,))]

    @staticmethod
    def candidate_filter(task_id, category='all', status='all'):
        clauses, args = ['task_id=?'], [task_id]
        if category != 'all':
            if category not in (*CATEGORIES, 'other'):
                raise ValueError('Invalid category')
            types = CATEGORIES[category] if category != 'other' else tuple(t for group in CATEGORIES.values() for t in group)
            placeholders = ','.join('?' for _ in types)
            operator = 'NOT IN' if category == 'other' else 'IN'
            clauses.append(f"lower(coalesce(json_extract(payload,'$.file_type'),'')) {operator} ({placeholders})")
            args.extend(types)
        if status != 'all':
            if status not in ('candidate', 'validated', 'partial', 'exported', 'failed'):
                raise ValueError('Invalid candidate status')
            clauses.append("json_extract(payload,'$.status')=?")
            args.append(status)
        return ' AND '.join(clauses), args

    def candidate_page(self, task_id, *, offset=0, limit=100, category='all', status='all'):
        if not isinstance(offset, int) or not isinstance(limit, int) or offset < 0 or not 1 <= limit <= 1000:
            raise ValueError("Invalid page bounds")
        where, args = self.candidate_filter(task_id, category, status)
        with closing(self.connect()) as db:
            return [json.loads(row[0]) for row in db.execute(
                f"SELECT payload FROM candidates WHERE {where} ORDER BY rowid LIMIT ? OFFSET ?", (*args, limit, offset))]

    def candidate_count(self, task_id, *, category='all', status='all'):
        where, args = self.candidate_filter(task_id, category, status)
        with closing(self.connect()) as db:
            return db.execute(f"SELECT count(*) FROM candidates WHERE {where}", args).fetchone()[0]

    def candidate_ids(self, task_id, *, category='all', status='all', limit=10000):
        if not 1 <= limit <= 10000:
            raise ValueError('Invalid selection limit')
        where, args = self.candidate_filter(task_id, category, status)
        with closing(self.connect()) as db:
            ids = [row[0] for row in db.execute(f'SELECT id FROM candidates WHERE {where} ORDER BY rowid LIMIT ?', (*args, limit + 1))]
        if len(ids) > limit:
            raise ValueError('导出筛选结果超过 10,000 项，请进一步筛选或分批选择。')
        return ids

    def candidate(self, candidate_id):
        with closing(self.connect()) as db:
            row = db.execute('SELECT task_id,payload FROM candidates WHERE id=?', (candidate_id,)).fetchone()
            if row is None:
                return None
            return {**json.loads(row['payload']), 'task_id': row['task_id']}

    def set_task_result(self, task_id, path):
        with closing(self.connect()) as db, db:
            db.execute('UPDATE tasks SET result_path=? WHERE id=?', (str(path), task_id))

    def start_export(self, task_id, candidate_id, output_path, metadata):
        export_id = str(uuid4())
        with closing(self.connect()) as db, db:
            db.execute('INSERT INTO exports VALUES(?,?,?,?,?,NULL,NULL,?,?,NULL,?)',
                       (export_id, task_id, candidate_id, 'running', str(output_path), metadata['size'], now(), json.dumps(metadata,ensure_ascii=False)))
        return export_id

    def finish_export(self, export_id, status, *, source_sha256=None, target_sha256=None, error_code=None):
        if status not in ('exported', 'failed', 'cancelled'):
            raise ValueError('Invalid export status')
        with closing(self.connect()) as db, db:
            db.execute('UPDATE exports SET status=?,source_sha256=?,target_sha256=?,error_code=? WHERE id=?',
                       (status, source_sha256, target_sha256, error_code, export_id))

    def exports(self, task_id, *, offset=0, limit=None):
        if offset < 0 or (limit is not None and not 1 <= limit <= 1000):
            raise ValueError('Invalid export page')
        with closing(self.connect()) as db:
            return [{**dict(row), 'metadata': json.loads(row['metadata'])} for row in db.execute(
                'SELECT * FROM exports WHERE task_id=? ORDER BY rowid LIMIT ? OFFSET ?', (task_id, limit if limit is not None else -1, offset))]

    def export_count(self, task_id):
        with closing(self.connect()) as db:
            return db.execute('SELECT count(*) FROM exports WHERE task_id=?', (task_id,)).fetchone()[0]

    def task(self, task_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def source(self, source_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            return dict(row) if row else None
