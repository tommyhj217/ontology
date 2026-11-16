"""MCP 클라이언트: 외부 MCP 서버의 도구를 에이전트 도구로 붙인다.

설정(agent_config.json 의 mcp_servers):
  "이름": {"command": "npx", "args": ["-y", "@some/mcp-server"], "env": {...}}     ← stdio
  "이름": {"url": "https://host/mcp", "headers": {"Authorization": "Bearer ..."}}  ← streamable HTTP
  공통 옵션: "auto_approve": ["도구이름", ...] 또는 true  (없으면 모든 호출에 사용자 승인 필요)
            "enabled": false

MCP SDK는 비동기라서 전용 스레드의 이벤트 루프에서 연결을 유지하고, 동기 함수로 감싼다.
"""
from __future__ import annotations

import asyncio
import os
import threading
from contextlib import AsyncExitStack

from .tools import Tool

CONNECT_TIMEOUT = 30
CALL_TIMEOUT = 120


def _result_text(result) -> str:
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else f"[{getattr(block, 'type', '비텍스트')} 콘텐츠 생략]")
    text = "\n".join(parts) or str(getattr(result, "structured_content", None) or "(빈 결과)")
    return ("오류: " + text) if getattr(result, "is_error", False) or getattr(result, "isError", False) else text


class MCPManager:
    def __init__(self, servers: dict):
        self.servers = {n: c for n, c in (servers or {}).items() if c.get("enabled", True)}
        self.errors: dict[str, str] = {}
        self._sessions: dict = {}
        self._tools: dict[str, list] = {}
        self._loop = None
        self._stop = None
        if self.servers:
            self._loop = asyncio.new_event_loop()
            threading.Thread(target=self._loop.run_forever, daemon=True, name="mcp-loop").start()
            self._stop = self._call(self._make_event())
            for name, cfg in self.servers.items():
                ready = self._call(self._make_event())
                asyncio.run_coroutine_threadsafe(self._serve(name, cfg, ready), self._loop)
                try:
                    self._call(asyncio.wait_for(ready.wait(), CONNECT_TIMEOUT), CONNECT_TIMEOUT + 5)
                except Exception:
                    self.errors.setdefault(name, f"{CONNECT_TIMEOUT}초 안에 연결되지 않았습니다.")

    async def _make_event(self):
        return asyncio.Event()

    def _call(self, coro, timeout: float = CALL_TIMEOUT):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    async def _serve(self, name: str, cfg: dict, ready: asyncio.Event) -> None:
        """서버 하나의 연결을 한 태스크 안에서 열고 유지한다."""
        from mcp import ClientSession, StdioServerParameters
        try:
            async with AsyncExitStack() as stack:
                if cfg.get("url"):
                    import httpx2
                    from mcp.client.streamable_http import streamable_http_client
                    http = await stack.enter_async_context(httpx2.AsyncClient(headers=cfg.get("headers") or {}, timeout=60))
                    streams = await stack.enter_async_context(streamable_http_client(cfg["url"], http_client=http))
                else:
                    from mcp.client.stdio import stdio_client
                    params = StdioServerParameters(command=cfg["command"], args=cfg.get("args", []),
                                                   env={**os.environ, **(cfg.get("env") or {})}, cwd=cfg.get("cwd"))
                    streams = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
                await session.initialize()
                self._tools[name] = list((await session.list_tools()).tools)
                self._sessions[name] = session
                ready.set()
                await self._stop.wait()
        except BaseException as err:  # 연결 실패는 에이전트를 멈추지 않고 기록만 한다
            self._sessions.pop(name, None)
            self.errors[name] = f"{type(err).__name__}: {err}"
            ready.set()

    def _run_tool(self, server: str, tool: str, args: dict) -> str:
        session = self._sessions.get(server)
        if session is None:
            return f"오류: MCP 서버 '{server}' 에 연결돼 있지 않습니다. {self.errors.get(server, '')}"
        try:
            return _result_text(self._call(session.call_tool(tool, args)))
        except Exception as err:
            return f"오류: MCP 도구 호출 실패 - {type(err).__name__}: {err}"

    def tools(self) -> list[Tool]:
        out = []
        for server, tools in self._tools.items():
            auto = self.servers[server].get("auto_approve", [])
            for t in tools:
                schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
                out.append(Tool(
                    name=f"mcp__{server}__{t.name}"[:64], description=(t.description or t.name)[:1000],
                    parameters=schema, source=f"MCP:{server}",
                    needs_approval=not (auto is True or t.name in (auto or [])),
                    run=lambda a, s=server, n=t.name: self._run_tool(s, n, a)))
        return out

    def status(self) -> dict[str, str]:
        return {n: (f"연결됨 (도구 {len(self._tools.get(n, []))}개)" if n in self._sessions
                    else f"연결 실패: {self.errors.get(n, '알 수 없음')}") for n in self.servers}

    def close(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._stop.set)
