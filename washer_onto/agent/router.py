"""모델 라우터: 주어진 GPT 배포들 중에서 질문에 맞는 것을 고르고, 필요하면 바꾼다.

- 사용자가 세션에 모델을 고정하면 그것만 쓴다.
- 고정하지 않으면 질문 난이도를 추정해 시작 모델을 고른다 (추가 LLM 호출 없음).
- 진행 중 승급(escalate): 모델이 스스로 어렵다고 판단하거나 도구 호출 오류가 반복되면 상위 모델로 넘긴다.
- 장애 대체(fallback): 배포 호출이 실패하면 가까운 등급의 다른 배포로 넘어간다.
- 요약·압축 같은 부수 작업은 항상 가장 저렴한 모델을 쓴다.
"""
from __future__ import annotations

import re

from .config import AgentConfig, ModelSpec

_HARD = ("왜", "원인", "분석", "비교", "검토", "판단", "추천", "평가해", "타당", "근거", "설계", "전략",
         "trade", "장단점", "개선", "문제점", "어떻게 해야")
_EASY = ("목록", "보여", "알려", "몇 건", "몇건", "몇 개", "리스트", "조회", "뭐야", "무엇", "누가", "언제")


def complexity(text: str) -> int:
    """0=단순 조회, 1=보통, 2=분석·판단."""
    t = text.lower()
    hard = sum(k in t for k in _HARD)
    if hard >= 2 or len(t) > 600 or (hard and len(t) > 200):
        return 2
    if hard or len(re.findall(r"[?？]", t)) > 1 or len(t) > 200:
        return 1
    return 0 if any(k in t for k in _EASY) or len(t) < 40 else 1


class Router:
    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg

    def pick(self, text: str, pinned: str = "") -> ModelSpec:
        if pinned and self.cfg.model(pinned):
            return self.cfg.model(pinned)
        models = self.cfg.models
        return models[round(complexity(text) / 2 * (len(models) - 1))]

    def higher(self, deployment: str) -> ModelSpec | None:
        current = self.cfg.model(deployment)
        return next((m for m in self.cfg.models if current and m.tier > current.tier), None)

    def fallbacks(self, deployment: str) -> list[ModelSpec]:
        """실패 시 시도할 순서: 등급이 가까운 것부터, 같은 거리면 상위 우선."""
        cur = self.cfg.model(deployment)
        others = [m for m in self.cfg.models if m.deployment != deployment]
        return sorted(others, key=lambda m: (abs(m.tier - cur.tier), -m.tier)) if cur else others
