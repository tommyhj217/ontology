"""이 지식베이스를 MCP 서버로 제공한다. 다른 에이전트(사내 Copilot, Claude 등)가 온톨로지를 도구로 쓸 수 있다.

  python -m washer_onto.mcp_server                      # stdio (MCP 클라이언트가 실행)
  python -m washer_onto.mcp_server --http               # streamable HTTP
  지식베이스 경로: WASHER_KB 환경변수 (기본 data/kb.ttl)
"""
from __future__ import annotations

import os
import sys

from mcp.server.mcpserver import MCPServer

from .agent import kbtools

KB_PATH = os.getenv("WASHER_KB", "data/kb.ttl")
server = MCPServer("washer-onto", instructions="세탁기 개발 모델 선정 결정과 선정 규칙을 조회·검증하는 지식베이스.")


@server.tool()
def list_decisions(category: str = "", verdict: str = "") -> str:
    """등록된 모델 선정 결정 목록과 검증 결과(적합/보완필요/부적합)를 조회한다."""
    return kbtools.list_decisions(KB_PATH, category, verdict)


@server.tool()
def get_decision(decision_id: str) -> str:
    """결정 1건의 후보·기준·평가·근거 전체와 검증 지적 사항을 조회한다."""
    return kbtools.get_decision(KB_PATH, decision_id)


@server.tool()
def list_rules(status: str = "", category: str = "") -> str:
    """선정 규칙을 조회한다. status 는 제안/확정/폐기 중 하나 또는 빈 값."""
    return kbtools.list_rules(KB_PATH, status, category)


@server.tool()
def list_open_questions() -> str:
    """규칙 확정을 위해 개발자 답변을 기다리는 질문을 조회한다."""
    return kbtools.list_open_questions(KB_PATH)


@server.tool()
def sparql_query(query: str) -> str:
    """지식베이스에 읽기 전용 SPARQL SELECT/ASK를 실행한다. PREFIX wm:, kb:, rdfs: 등은 선언돼 있다."""
    return kbtools.sparql_query(KB_PATH, query)


@server.tool()
def answer_rule_question(question_id: str, answer: str, by: str) -> str:
    """개발자의 답변을 규칙 질문에 반영해 규칙을 확정·수정·폐기한다 (지식베이스를 변경함)."""
    return kbtools.answer_rule_question(KB_PATH, question_id, answer, by)


if __name__ == "__main__":
    server.run("streamable-http" if "--http" in sys.argv else "stdio")
