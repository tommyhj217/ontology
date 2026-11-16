"""에이전트 도구 등록부: 내장 지식베이스 도구 + MCP 도구를 같은 형태로 다룬다."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..models import DecisionRecord
from . import kbtools


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict                    # JSON Schema
    run: Callable[[dict], str]
    needs_approval: bool = False        # True면 사용자가 승인해야 실행
    source: str = "내장"

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


_S = {"type": "string"}


def builtin_tools(kb_path: str) -> list[Tool]:
    k = kb_path
    return [
        Tool("list_decisions", "등록된 모델 선정 결정 목록과 검증 결과(적합/보완필요/부적합)를 조회한다.",
             _obj({"category": {**_S, "description": "분류 필터 (예: 모터)"},
                   "verdict": {**_S, "enum": ["적합", "보완필요", "부적합"]}}),
             lambda a: kbtools.list_decisions(k, a.get("category", ""), a.get("verdict", ""))),
        Tool("get_decision", "결정 1건의 후보·기준·평가·근거 전체와 검증 지적 사항을 조회한다.",
             _obj({"decision_id": _S}, ["decision_id"]),
             lambda a: kbtools.get_decision(k, a["decision_id"])),
        Tool("list_rules", "선정 규칙을 조회한다. 상태: 제안(미확정)/확정/폐기.",
             _obj({"status": {**_S, "enum": ["제안", "확정", "폐기"]}, "category": _S}),
             lambda a: kbtools.list_rules(k, a.get("status", ""), a.get("category", ""))),
        Tool("list_open_questions", "규칙 확정을 위해 개발자 답변을 기다리는 질문을 조회한다.", _obj({}),
             lambda a: kbtools.list_open_questions(k)),
        Tool("sparql_query",
             "지식베이스(RDF)에 SPARQL SELECT/ASK를 실행한다. 다른 도구로 안 되는 집계·교차 조회에 쓴다. "
             "PREFIX wm:, kb:, rdfs:, rdf:, xsd: 는 이미 선언돼 있다. 이름은 rdfs:label 에 있다. 주요 관계: "
             "wm:SelectionDecision -wm:hasCandidate/wm:chosenCandidate/wm:hasCriterion/wm:hasEvaluation/wm:decidedBy/wm:category->, "
             "wm:Evaluation -wm:ofCandidate/wm:onCriterion/wm:measuredValue/wm:rating/wm:hasEvidence->, "
             "wm:Criterion -wm:operator/wm:threshold/wm:unit/wm:weight/wm:mandatory->, "
             "wm:SelectionRule -wm:ruleCategory/wm:criterionName/wm:ruleStatus/wm:statement->.",
             _obj({"query": _S}, ["query"]), lambda a: kbtools.sparql_query(k, a["query"])),
        Tool("answer_rule_question", "개발자의 답변을 규칙 질문에 반영해 규칙을 확정·수정·폐기한다. "
             "사용자가 대화에서 명확히 답한 경우에만 쓴다.",
             _obj({"question_id": _S, "answer": {**_S, "description": "선택지 문구 그대로, 또는 자유 서술"},
                   "by": {**_S, "description": "답변자 이름"}}, ["question_id", "answer", "by"]),
             lambda a: kbtools.answer_rule_question(k, a["question_id"], a["answer"], a["by"]), needs_approval=True),
        Tool("mine_rules", "등록된 결정에서 규칙 후보와 확인 질문을 새로 만든다.", _obj({}),
             lambda a: kbtools.mine_rules(k), needs_approval=True),
        Tool("save_decision", "사용자가 대화로 설명한 선정 결정을 지식베이스에 등록(같은 ID는 교체)하고 검증한다. "
             "사용자가 말하지 않은 수치를 채우지 않는다.",
             _obj({"record": DecisionRecord.model_json_schema()}, ["record"]),
             lambda a: kbtools.save_decision(k, a["record"]), needs_approval=True),
    ]
