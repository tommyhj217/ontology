"""터미널 대화:  python -m washer_onto chat --user <이름>"""
from __future__ import annotations

from .config import load_config
from .core import Agent, TurnResult
from .mcp_client import MCPManager


def make_agent(config_path: str | None = None) -> Agent:
    cfg = load_config(config_path)
    return Agent(cfg, mcp=MCPManager(cfg.mcp_servers) if cfg.mcp_servers else None)


def _approve(agent: Agent, sid: str, res: TurnResult) -> TurnResult:
    while res.status == "needs_approval":
        decisions = {}
        for p in res.pending:
            print(f"\n[승인 필요] {p['source']} 도구 {p['name']}\n  인자: {p['arguments']}")
            decisions[p["id"]] = input("  실행할까요? (y/N) ").strip().lower() in ("y", "yes", "ㅛ")
        res = agent.resume(sid, decisions)
    return res


def run(user: str, session_id: str | None = None, config_path: str | None = None) -> None:
    agent = make_agent(config_path)
    if agent.mcp:
        for name, status in agent.mcp.status().items():
            print(f"MCP {name}: {status}")
    sid = session_id or agent.start(user)
    if session_id and not agent.store.session(session_id):
        raise SystemExit(f"세션 '{session_id}' 이(가) 없습니다.")
    print(f"세션 {sid} · 모델 {', '.join(m.deployment for m in agent.cfg.models)} · /help 로 명령어, 빈 줄로 종료")
    try:
        while True:
            text = input("\n> ").strip()
            if not text:
                break
            try:
                res = _approve(agent, sid, agent.send(sid, text))
            except Exception as err:
                print(f"오류: {type(err).__name__}: {err}")
                continue
            for note in res.notes:
                print(f"  · {note}")
            print(res.reply + (f"\n  [{res.model}]" if res.model else ""))
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        if agent.mcp:
            agent.mcp.close()


def list_sessions(user: str, config_path: str | None = None) -> None:
    from .sessions import SessionStore
    store = SessionStore(load_config(config_path).db_path)
    for s in store.list_sessions(user):
        print(f"{s['id']}\t{s['updated_at']}\t{s['title']}")
