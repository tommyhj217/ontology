"""지식베이스 도구: 에이전트와 MCP 서버가 함께 쓰는 기능. 모두 문자열(주로 YAML)을 돌려준다."""
from __future__ import annotations

import yaml

from ..audit import audit, render
from ..models import DecisionRecord
from ..store import KB


def _yaml(obj) -> str:
    return yaml.safe_dump(obj, allow_unicode=True, sort_keys=False, width=200)


def list_decisions(kb_path: str, category: str = "", verdict: str = "") -> str:
    kb = KB(kb_path)
    rows = []
    for rec in kb.all():
        rep = audit(kb, rec.id)
        if (category and category not in rec.category) or (verdict and verdict != rep.verdict):
            continue
        rows.append({"id": rec.id, "제목": rec.title, "분류": rec.category, "프로젝트": rec.project,
                     "선정": rec.chosen, "결정자": rec.decided_by, "결정일": rec.decided_on, "검증": rep.verdict})
    return _yaml(rows) if rows else "조건에 맞는 결정이 없습니다."


def get_decision(kb_path: str, decision_id: str) -> str:
    kb = KB(kb_path)
    rec = kb.get(decision_id)
    if rec is None:
        return f"오류: 결정 '{decision_id}' 이(가) 없습니다. list_decisions 로 ID를 확인하세요."
    return _yaml(rec.model_dump(exclude_none=True)) + "\n--- 검증 결과 ---\n" + render(audit(kb, decision_id))


def list_rules(kb_path: str, status: str = "", category: str = "") -> str:
    rows = [{"id": r.id, "상태": r.status, "분류": r.category, "기준": r.criterion, "조건": r.spec(),
             "구분": "필수" if r.mandatory else "권장", "규칙": r.statement, "이유": r.rationale,
             "확정자": r.confirmed_by, "확정일": r.confirmed_on}
            for r in KB(kb_path).rules(status=status or None, category=category or None)]
    return _yaml(rows) if rows else "조건에 맞는 규칙이 없습니다."


def list_open_questions(kb_path: str) -> str:
    rows = [{"id": q.id, "규칙": q.rule_id, "질문": q.text, "선택지": q.options}
            for q in KB(kb_path).questions(status="open")]
    return _yaml(rows) if rows else "답변을 기다리는 질문이 없습니다."


def sparql_query(kb_path: str, query: str) -> str:
    """읽기 전용 SPARQL SELECT/ASK."""
    try:
        result = KB(kb_path).query(query)
    except Exception as err:
        return f"오류: SPARQL 실행 실패 - {err}"
    if result.type == "ASK":
        return str(bool(result.askAnswer))
    cols = [str(v) for v in result.vars or []]
    lines = [" | ".join(cols)] + [" | ".join("" if v is None else str(v) for v in row) for row in result]
    return "\n".join(lines) if len(lines) > 1 else "결과 없음"


def answer_rule_question(kb_path: str, question_id: str, answer: str, by: str) -> str:
    from .. import rules as R
    kb = KB(kb_path)
    try:
        rule, summary = R.answer(kb, question_id, answer, by)
    except Exception as err:
        return f"오류: {err}"
    kb.save()
    return f"[{rule.status}] {rule.statement}\n변경: {summary}"


def mine_rules(kb_path: str) -> str:
    from .. import rules as R
    kb = KB(kb_path)
    res = R.mine_rules(kb)
    kb.save()
    return f"규칙 후보 {res['proposed']}건 제안/갱신, 사람이 정한 규칙 {res['kept']}건 유지, 대기 질문 {res['open_questions']}건"


def save_decision(kb_path: str, record: dict) -> str:
    try:
        rec = DecisionRecord.model_validate(record)
    except Exception as err:
        return f"오류: 결정 기록 형식이 맞지 않습니다 - {err}"
    kb = KB(kb_path)
    did = kb.upsert(rec)
    kb.save()
    return f"저장: {did}\n" + render(audit(kb, did))
