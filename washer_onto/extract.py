"""문서(회의록·검토보고서 등)에서 선정 의사결정을 추출한다 (Azure OpenAI 구조화 출력).

추출 후 인용문이 원문에 실제로 있는지 대조(grounding)해서 LLM 환각을 표시한다.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import llm
from .models import DecisionRecord, ExtractionResult

CHUNK_CHARS = 40_000
CHUNK_OVERLAP = 2_000

SYSTEM = """너는 세탁기 개발 조직의 문서에서 '모델 선정 의사결정'을 구조화하는 분석가다.
여기서 '모델'은 부품 모델(모터, 댐퍼, 베어링, 인버터 등), 제품 모델, AI/제어 알고리즘 모델 등 여러 후보 중 선택한 대상을 모두 포함한다.

규칙:
- 문서에 실제로 적힌 내용만 추출한다. 추측으로 값을 만들지 않는다. 없는 값은 null 또는 빈 문자열로 둔다.
- 결정 1건마다: 후보 전부(탈락 후보 포함), 선정 기준 전부, 후보×기준별 평가값을 추출한다.
- 정량 기준은 operator(>=, <=, >, <, ==)와 threshold, unit을 채운다. 예: "48dB 이하" → operator "<=", threshold 48, unit "dB".
- 기준에 "필수", "must", "규격", "법규" 같은 표현이 있으면 mandatory=true.
- 가중치가 문서에 있으면 weight에 넣고, 없으면 1.
- 측정값이 숫자면 value에, 상/중/하·우수/보통 같은 정성 평가는 rating(1~5)에 넣는다 (상=5, 중=3, 하=1).
- evaluations의 candidate, criterion 값은 candidates, criteria의 name과 글자까지 일치해야 한다.
- 각 평가의 evidence.quote 에는 그 값의 근거가 되는 원문 문장을 한 글자도 바꾸지 말고 그대로 복사한다.
  evidence.type 은 시험성적서/시뮬레이션/사양서/회의록/견적서/기타 중에서 고르고, 문서번호가 있으면 reference에 넣는다.
- grounded 필드는 항상 null로 둔다.
- 날짜는 YYYY-MM-DD.
- 선정 의사결정이 없는 문서면 decisions를 빈 배열로 반환한다."""


def read_document(path: str | Path) -> str:
    """txt/md/pdf/docx 문서를 텍스트로 읽는다."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(str(p)).pages)
    if ext == ".docx":
        import docx
        d = docx.Document(str(p))
        parts = [para.text for para in d.paragraphs]
        for table in d.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text.strip() for cell in row.cells))
        return "\n".join(parts)
    return p.read_text(encoding="utf-8")


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def ground(records: list[DecisionRecord], text: str) -> None:
    """각 인용문이 원문에 있는지(공백 무시) 확인해 evidence.grounded 를 채운다."""
    haystack = _norm(text)
    for rec in records:
        for e in rec.evaluations:
            for ev in e.evidence:
                ev.grounded = bool(ev.quote.strip()) and _norm(ev.quote) in haystack


def _chunks(text: str) -> list[str]:
    if len(text) <= CHUNK_CHARS:
        return [text]
    step = CHUNK_CHARS - CHUNK_OVERLAP
    return [text[i:i + CHUNK_CHARS] for i in range(0, len(text), step)]


def extract(text: str, source: str = "") -> list[DecisionRecord]:
    """문서 텍스트 → 결정 기록 목록. 저장 전에 사람이 검토하는 것을 전제로 한다."""
    merged: dict[str, DecisionRecord] = {}
    for chunk in _chunks(text):
        result = llm.chat_structured(SYSTEM, f"문서명: {source or '(미상)'}\n\n--- 문서 시작 ---\n{chunk}\n--- 문서 끝 ---",
                                     ExtractionResult)
        for rec in result.decisions:
            # 긴 문서가 나뉘어 같은 결정이 두 번 나오면 평가가 더 많은 쪽을 남긴다.
            old = merged.get(rec.title)
            if old is None or len(rec.evaluations) > len(old.evaluations):
                merged[rec.title] = rec
    records = list(merged.values())
    for rec in records:
        rec.source = rec.source or source
    ground(records, text)
    return records
