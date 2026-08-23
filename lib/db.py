import json
import sqlite3
import time

from lib import paths

DDL = """
CREATE TABLE IF NOT EXISTS memory (
  id             TEXT PRIMARY KEY,
  title          TEXT NOT NULL,
  body           TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK (kind IN
                   ('gotcha','decision','preference','fact','procedure','env')),
  scope_type     TEXT NOT NULL CHECK (scope_type IN
                   ('global','project','path','command','tool')),
  scope_value    TEXT DEFAULT '',
  project        TEXT NOT NULL DEFAULT '',
  tags           TEXT NOT NULL DEFAULT '',
  channel        TEXT NOT NULL DEFAULT 'agent' CHECK (channel IN
                   ('user_witnessed','observer_witnessed','agent','external')),
  authority      TEXT NOT NULL DEFAULT 'pending' CHECK (authority IN
                   ('full','standard','pending','quarantined')),
  status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN
                   ('active','superseded','expired','refuted')),
  hazard         INTEGER NOT NULL DEFAULT 0,
  hazard_mode    TEXT NOT NULL DEFAULT 'deny' CHECK (hazard_mode IN ('deny','ask')),
  pinned         INTEGER NOT NULL DEFAULT 0,
  supersedes     TEXT REFERENCES memory(id),
  valid_from     INTEGER,
  invalidated_at INTEGER,
  ttl_days       INTEGER,
  prior          REAL NOT NULL DEFAULT 1.0,
  corroborations INTEGER NOT NULL DEFAULT 0,
  source_session TEXT, source_event TEXT,
  created_at     INTEGER NOT NULL,
  last_access_at INTEGER,
  access_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mem_proj   ON memory(project, status);
CREATE INDEX IF NOT EXISTS idx_mem_scope  ON memory(scope_type, scope_value, status);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  title, body, tags,
  content='memory', content_rowid='rowid',
  tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memory BEGIN
  INSERT INTO memory_fts(rowid,title,body,tags)
  VALUES (new.rowid,new.title,new.body,new.tags);
END;
CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memory BEGIN
  INSERT INTO memory_fts(memory_fts,rowid,title,body,tags)
  VALUES ('delete',old.rowid,old.title,old.body,old.tags);
END;
CREATE TRIGGER IF NOT EXISTS mem_au AFTER UPDATE ON memory BEGIN
  INSERT INTO memory_fts(memory_fts,rowid,title,body,tags)
  VALUES ('delete',old.rowid,old.title,old.body,old.tags);
  INSERT INTO memory_fts(rowid,title,body,tags)
  VALUES (new.rowid,new.title,new.body,new.tags);
END;

CREATE TABLE IF NOT EXISTS access_log (
  memory_id TEXT NOT NULL,
  session_id TEXT, agent_id TEXT,
  ts INTEGER NOT NULL,
  event TEXT NOT NULL CHECK (event IN
    ('injected','reminded','fetched','denied','cited','synthetic')),
  weight REAL NOT NULL DEFAULT 1.0,
  query TEXT
);
CREATE INDEX IF NOT EXISTS idx_acc_mem ON access_log(memory_id, ts DESC);

CREATE TABLE IF NOT EXISTS candidate (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL, agent_id TEXT,
  ts INTEGER NOT NULL,
  priority TEXT NOT NULL CHECK (priority IN ('P0','P1','P2','P3')),
  signal TEXT NOT NULL,
  payload TEXT NOT NULL,
  draft_cmd TEXT,
  near_match TEXT,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN
    ('open','saved','dropped','expired','carried'))
);

CREATE TABLE IF NOT EXISTS journal (
  session_id TEXT, agent_id TEXT,
  ts INTEGER NOT NULL,
  event TEXT NOT NULL,
  data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jrn ON journal(session_id, ts);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _apply_pragmas(conn):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=250")


def connect(readonly: bool = False) -> sqlite3.Connection:
    path = paths.db_path()
    conn = None
    if readonly:
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            conn = None
    if conn is None:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        _apply_pragmas(conn)
    except sqlite3.OperationalError:
        pass
    return conn


def ensure_schema(conn) -> None:
    conn.executescript(DDL)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')")
    conn.commit()


def _status_sql(statuses):
    return ",".join("?" for _ in statuses)


def search(conn, q: str, project: str, k: int = 12,
           statuses: tuple = ("active",)) -> list[dict]:
    if not q:
        return []
    sql = (
        "SELECT m.*, bm25(memory_fts) AS bm25 "
        "FROM memory_fts JOIN memory m ON m.rowid = memory_fts.rowid "
        f"WHERE memory_fts MATCH ? AND m.status IN ({_status_sql(statuses)}) "
        "AND m.project IN (?, '') "
        "ORDER BY bm25(memory_fts) ASC LIMIT ?"
    )
    try:
        rows = conn.execute(sql, (q, *statuses, project, k)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


def by_scope(conn, scope_type: str, project: str,
             statuses: tuple = ("active",)) -> list[dict]:
    sql = (
        f"SELECT * FROM memory WHERE scope_type = ? "
        f"AND status IN ({_status_sql(statuses)}) AND project IN (?, '')"
    )
    rows = conn.execute(sql, (scope_type, *statuses, project)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["bm25"] = 0.0
        out.append(d)
    return out


def get_memory(conn, mem_id: str) -> dict | None:
    if not mem_id:
        return None
    row = conn.execute("SELECT * FROM memory WHERE id = ?", (mem_id,)).fetchone()
    if row:
        return dict(row)
    if len(mem_id) >= 6:
        rows = conn.execute(
            "SELECT * FROM memory WHERE id LIKE ? LIMIT 2",
            (mem_id + "%",)).fetchall()
        if len(rows) == 1:
            return dict(rows[0])
    return None


def log_access(conn, memory_id: str, session_id: str, agent_id: str,
               event: str, weight: float = 1.0, query: str | None = None) -> None:
    now = int(time.time())
    try:
        conn.execute(
            "INSERT INTO access_log(memory_id, session_id, agent_id, ts, event, weight, query) "
            "VALUES (?,?,?,?,?,?,?)",
            (memory_id, session_id, agent_id, now, event, weight, query))
        conn.execute(
            "UPDATE memory SET last_access_at = ?, access_count = access_count + 1 "
            "WHERE id = ?", (now, memory_id))
        conn.commit()
    except sqlite3.OperationalError:
        pass


def journal(conn, session_id: str, agent_id: str, event: str, data: dict) -> None:
    try:
        conn.execute(
            "INSERT INTO journal(session_id, agent_id, ts, event, data) "
            "VALUES (?,?,?,?,?)",
            (session_id, agent_id, int(time.time()), event, json.dumps(data)))
        conn.commit()
    except sqlite3.OperationalError:
        pass


def activation_events(conn, memory_id: str, window: int) -> list[tuple[int, float]]:
    rows = conn.execute(
        "SELECT ts, weight FROM access_log WHERE memory_id = ? "
        "ORDER BY ts DESC LIMIT ?", (memory_id, window)).fetchall()
    return [(r["ts"], r["weight"]) for r in rows]
