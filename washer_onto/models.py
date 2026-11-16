"""의사결정 기록의 입력/출력 모델.

사람이 YAML·웹 폼으로 작성하는 형식이자, LLM 추출의 구조화 출력 스키마로도 쓰인다.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Operator = Literal[">=", "<=", ">", "<", "=="]


class Evidence(BaseModel):
    type: str = Field("기타", description="근거 유형: 시험성적서, 시뮬레이션, 사양서, 회의록, 견적서, 기타 등")
    reference: str = Field("", description="문서번호, 파일명 또는 링크")
    quote: str = Field("", description="근거가 되는 원문 문장을 그대로 인용")
    grounded: Optional[bool] = Field(None, description="인용이 원문에서 확인되었는지(시스템이 채움)")


class Candidate(BaseModel):
    name: str = Field(description="후보 모델명")
    maker: str = Field("", description="제조사/공급사/출처")
    description: str = ""


class Criterion(BaseModel):
    name: str = Field(description="기준 이름 (예: 소음, 효율, 단가)")
    description: str = ""
    operator: Optional[Operator] = Field(None, description="정량 기준의 비교 연산자")
    threshold: Optional[float] = Field(None, description="정량 기준의 임계값")
    unit: str = ""
    weight: float = Field(1.0, description="상대 가중치(클수록 중요)")
    mandatory: bool = Field(False, description="반드시 만족해야 하는 필수 기준인지")
    higher_is_better: Optional[bool] = Field(
        None, description="값이 클수록 좋은지. 비우면 연산자에서 추론(>= 는 클수록, <= 는 작을수록)")

    def direction(self) -> Optional[bool]:
        """True=클수록 우수, False=작을수록 우수, None=우열 방향 없음."""
        if self.higher_is_better is not None:
            return self.higher_is_better
        if self.operator in (">=", ">"):
            return True
        if self.operator in ("<=", "<"):
            return False
        return None

    def passes(self, value: float) -> Optional[bool]:
        if self.operator is None or self.threshold is None:
            return None
        t = self.threshold
        return {
            ">=": value >= t, "<=": value <= t, ">": value > t, "<": value < t,
            "==": abs(value - t) < 1e-9,
        }[self.operator]


class Evaluation(BaseModel):
    candidate: str = Field(description="후보 모델명 (candidates의 name과 일치)")
    criterion: str = Field(description="기준 이름 (criteria의 name과 일치)")
    value: Optional[float] = Field(None, description="정량 측정값(기준 단위)")
    rating: Optional[float] = Field(None, description="정성 평가 평점 1~5")
    note: str = ""
    evidence: list[Evidence] = Field(default_factory=list)


class DecisionRecord(BaseModel):
    id: str = Field("", description="결정 ID. 비우면 제목에서 생성")
    title: str = Field(description="결정 제목 (예: 드럼세탁기 DD 모터 선정)")
    project: str = Field("", description="개발 프로젝트/과제명")
    category: str = Field("", description="선정 대상 분류 (예: 모터, 댐퍼, AI 모델)")
    chosen: str = Field(description="최종 선정된 후보 모델명")
    rationale: str = Field("", description="선정 사유 서술")
    decided_by: list[str] = Field(default_factory=list)
    reviewed_by: list[str] = Field(default_factory=list)
    decided_on: Optional[str] = Field(None, description="결정일 YYYY-MM-DD")
    source: str = Field("", description="이 기록의 출처 문서명")
    candidates: list[Candidate] = Field(default_factory=list)
    criteria: list[Criterion] = Field(default_factory=list)
    evaluations: list[Evaluation] = Field(default_factory=list)

    def evaluation(self, candidate: str, criterion: str) -> Optional[Evaluation]:
        for e in self.evaluations:
            if e.candidate == candidate and e.criterion == criterion:
                return e
        return None


class ExtractionResult(BaseModel):
    decisions: list[DecisionRecord] = Field(default_factory=list)
