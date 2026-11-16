"""세션·이력 저장소 (SQLite).

- sessions : 사용자별 대화 세션 (요약, 고정 모델 포함)
- messages : 모든 메시지 원문 (압축돼도 지우지 않는다) + 어떤 모델이 몇 토큰을 썼는지
- events   : 모델 전환, 압축, 도구 승인/거절 등 에이전트 동작 이력
- memories : 사용자가 세션을 넘어 유지하길 원하는 지시·선호 (사용자 의도)
- approvals: 승인이 필요한 도구 호출에 대한 사용자 결정
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, user TEXT NOT NULL, title TEXT DEFAULT '', created_at TEXT, updated_at TEXT,
  pinned_model TEXT DEFAULT '', turn_model TEXT DEFAULT '', summary TEXT DEFAULT '',
  summary_upto INTEGER DEFAULT 0, last_prompt_tokens INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT DEFAULT '',
  tool_calls TEXT DEFAULT '', tool_call_id TEXT DEFAULT '', name TEXT DEFAULT '', model TEXT DEFAULT '',
  prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0, cached_tokens INTEGER DEFAULT 0,
  created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, at TEXT, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS memories(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT, session_id TEXT);
CREATE TABLE IF NOT EXISTS approvals(
  tool_call_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, approved INTEGER NOT NULL, at TEXT);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SessionStore:
    def __init__(self, path: str | Path = "data/sessions.db"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._mem = sqlite3.connect(":memory:", check_same_thread=False) if self.path == ":memory:" else None
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        c = self._mem or sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            if self._mem is None:
                c.close()

    # ---- 세션
    def create_session(self, user: str, title: str = "") -> str:
        sid = uuid.uuid4().hex[:12]
        with self._conn() as c:
            c.execute("INSERT INTO sessions(id,user,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                      (sid, user, title, now(), now()))
        return sid

    def session(self, sid: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        return dict(row) if row else None

    def list_sessions(self, user: str) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM sessions WHERE user=? ORDER BY updated_at DESC, rowid DESC", (user,))]

    def update_session(self, sid: str, **fields) -> None:
        fields["updated_at"] = now()
        with self._conn() as c:
            c.execute(f"UPDATE sessions SET {', '.join(k + '=?' for k in fields)} WHERE id=?", (*fields.values(), sid))

    def delete_session(self, sid: str) -> None:
        with self._conn() as c:
            for table in ("messages", "events", "approvals"):
                c.execute(f"DELETE FROM {table} WHERE session_id=?", (sid,))
            c.execute("DELETE FROM sessions WHERE id=?", (sid,))

    # ---- 메시지
    def add_message(self, sid: str, role: str, content: str = "", *, tool_calls: list | None = None,
                    tool_call_id: str = "", name: str = "", model: str = "", usage: dict | None = None) -> int:
        u = usage or {}
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO messages(session_id,role,content,tool_calls,tool_call_id,name,model,"
                "prompt_tokens,completion_tokens,cached_tokens,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (sid, role, content or "", json.dumps(tool_calls, ensure_ascii=False) if tool_calls else "",
                 tool_call_id, name, model, u.get("prompt", 0), u.get("completion", 0), u.get("cached", 0), now()))
            c.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now(), sid))
            return cur.lastrowid

    def messages(self, sid: str, after_id: int = 0) -> list[dict]:
        with self._conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM messages WHERE session_id=? AND id>? ORDER BY id", (sid, after_id))]
        for r in rows:
            r["tool_calls"] = json.loads(r["tool_calls"]) if r["tool_calls"] else []
        return rows

    # ---- 이력
    def log(self, sid: str, kind: str, **detail) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO events(session_id,at,kind,detail) VALUES(?,?,?,?)",
                      (sid, now(), kind, json.dumps(detail, ensure_ascii=False)))

    def events(self, sid: str, kind: str | None = None) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM events WHERE session_id=? ORDER BY id", (sid,)).fetchall()
        out = [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]
        return [e for e in out if kind is None or e["kind"] == kind]

    def usage(self, sid: str) -> dict:
        """모델별 누적 토큰 (대화 + 압축 요약 호출 포함)."""
        totals: dict[str, dict] = {}
        def add(model, p, c, cached):
            t = totals.setdefault(model, {"calls": 0, "prompt": 0, "completion": 0, "cached": 0})
            t["calls"] += 1; t["prompt"] += p; t["completion"] += c; t["cached"] += cached
        for m in self.messages(sid):
            if m["role"] == "assistant" and m["model"]:
                add(m["model"], m["prompt_tokens"], m["completion_tokens"], m["cached_tokens"])
        for e in self.events(sid, "compaction"):
            u = e["detail"].get("usage", {})
            add(e["detail"].get("model", "?"), u.get("prompt", 0), u.get("completion", 0), u.get("cached", 0))
        return totals

    # ---- 사용자 기억
    def remember(self, user: str, text: str, sid: str = "") -> int:
        with self._conn() as c:
            return c.execute("INSERT INTO memories(user,text,created_at,session_id) VALUES(?,?,?,?)",
                             (user, text.strip(), now(), sid)).lastrowid

    def memories(self, user: str) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM memories WHERE user=? ORDER BY id", (user,))]

    def forget(self, user: str, memory_id: int) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM memories WHERE user=? AND id=?", (user, memory_id)).rowcount > 0

    # ---- 승인
    def set_approval(self, sid: str, tool_call_id: str, approved: bool) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO approvals VALUES(?,?,?,?)", (tool_call_id, sid, int(approved), now()))

    def approval(self, tool_call_id: str) -> Optional[bool]:
        with self._conn() as c:
            row = c.execute("SELECT approved FROM approvals WHERE tool_call_id=?", (tool_call_id,)).fetchone()
        return None if row is None else bool(row["approved"])
