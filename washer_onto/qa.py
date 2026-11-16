"""자연어 질문 → SPARQL → 답변 (Azure OpenAI)."""
from __future__ import annotations

import re

from . import llm
from .store import KB, ONTOLOGY_PATH, SPARQL_PREFIXES

MAX_ROWS = 200

_SPARQL_SYSTEM = """너는 SPARQL 전문가다. 아래 온톨로지로 구축된 RDF 지식베이스에 대한 질문을 SPARQL SELECT 쿼리 하나로 바꿔라.

데이터 규칙:
- 결정·후보·기준·담당자·프로젝트·분류의 이름은 모두 rdfs:label 에 있다. 이름 비교는 CONTAINS(LCASE(STR(?label)), "...") 로 느슨하게 하라.
- 결정 --wm:hasCandidate--> 후보, 결정 --wm:chosenCandidate--> 선정 후보, 결정 --wm:hasCriterion--> 기준
- 결정 --wm:hasEvaluation--> 평가, 평가 --wm:ofCandidate--> 후보, 평가 --wm:onCriterion--> 기준, 평가 --wm:hasEvidence--> 근거
- 결과에는 IRI 대신 사람이 읽을 수 있는 label 을 SELECT 하라.
- PREFIX 선언은 이미 되어 있으니 쓰지 마라: wm:, kb:, rdfs:, rdf:, xsd:, prov:
- 설명 없이 쿼리만 출력하라.

온톨로지(Turtle):
"""

_ANSWER_SYSTEM = """너는 세탁기 개발 조직의 의사결정 지식베이스 안내자다.
주어진 조회 결과만 근거로 한국어로 간결하게 답하라. 결과에 없는 내용은 지어내지 말고 '기록에 없음'이라고 답하라."""


def _strip(sparql: str) -> str:
    m = re.search(r"```(?:sparql)?\s*(.*?)```", sparql, flags=re.S)
    s = (m.group(1) if m else sparql).strip()
    return re.sub(r"(?im)^\s*PREFIX\s+\S+\s*<[^>]*>\s*$", "", s).strip()


def ask(kb: KB, question: str) -> dict:
    """{'sparql', 'rows', 'answer'} 를 반환한다. 쿼리 오류 시 1회 자동 수정한다."""
    system = _SPARQL_SYSTEM + ONTOLOGY_PATH.read_text(encoding="utf-8")
    sparql = _strip(llm.chat(system, question))
    try:
        result = kb.query(sparql)
    except Exception as err:  # 문법 오류 → 오류 메시지를 주고 한 번 더 시도
        sparql = _strip(llm.chat(system, f"{question}\n\n이전 쿼리:\n{sparql}\n\n오류:\n{err}\n\n수정한 쿼리만 출력하라."))
        result = kb.query(sparql)

    cols = [str(v) for v in (result.vars or [])]
    rows = [{c: (str(r[c]) if r[c] is not None else "") for c in cols} for r in result][:MAX_ROWS]
    table = "\n".join(" | ".join(r[c] for c in cols) for r in rows) or "(결과 없음)"
    answer = llm.chat(_ANSWER_SYSTEM,
                      f"질문: {question}\n\n조회 결과 (열: {' | '.join(cols)}):\n{table}")
    return {"sparql": SPARQL_PREFIXES + sparql, "rows": rows, "answer": answer}
