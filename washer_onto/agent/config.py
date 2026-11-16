"""에이전트 설정: 사용할 GPT 배포 목록, MCP 서버, 압축 기준.

설정 파일(agent_config.json, 또는 WASHER_AGENT_CONFIG 경로)이 없으면 .env 만으로 동작한다:
  AZURE_OPENAI_MODELS=작은배포,중간배포,큰배포   (성능 오름차순, 쉼표 구분)
  없으면 AZURE_OPENAI_DEPLOYMENT 하나만 사용.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import find_dotenv, load_dotenv


@dataclass
class ModelSpec:
    deployment: str                       # Azure 배포 이름
    tier: int = 1                         # 클수록 고성능·고비용
    input_price: Optional[float] = None   # 100만 토큰당 가격(통화 무관). 없으면 비용 계산 생략
    cached_input_price: Optional[float] = None
    output_price: Optional[float] = None
    note: str = ""

    def cost(self, prompt: int, completion: int, cached: int = 0) -> Optional[float]:
        if self.input_price is None or self.output_price is None:
            return None
        cached_price = self.cached_input_price if self.cached_input_price is not None else self.input_price
        return ((prompt - cached) * self.input_price + cached * cached_price
                + completion * self.output_price) / 1_000_000


@dataclass
class AgentConfig:
    models: list[ModelSpec] = field(default_factory=list)
    mcp_servers: dict = field(default_factory=dict)
    compact_at_tokens: int = 12_000       # 프롬프트가 이 크기를 넘으면 오래된 대화를 요약으로 압축
    keep_recent_turns: int = 3            # 압축해도 원문으로 남길 최근 사용자 턴 수
    tool_output_chars: int = 4_000        # 도구 결과를 모델에 보여줄 최대 길이
    max_steps: int = 8                    # 한 턴에서 모델 호출 최대 횟수
    db_path: str = "data/sessions.db"
    kb_path: str = "data/kb.ttl"

    def __post_init__(self):
        self.models.sort(key=lambda m: m.tier)

    def model(self, deployment: str) -> Optional[ModelSpec]:
        return next((m for m in self.models if m.deployment == deployment), None)

    @property
    def cheapest(self) -> ModelSpec:
        return self.models[0]


def load_config(path: str | Path | None = None) -> AgentConfig:
    load_dotenv(find_dotenv(usecwd=True))
    path = Path(path or os.getenv("WASHER_AGENT_CONFIG") or "agent_config.json")
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    models = [ModelSpec(**m) for m in data.pop("models", [])]
    if not models:
        names = [n.strip() for n in os.getenv("AZURE_OPENAI_MODELS", "").split(",") if n.strip()]
        if not names:
            from ..llm import _DEPLOYMENT, _env
            names = [_env(_DEPLOYMENT)] if _env(_DEPLOYMENT) else []
        models = [ModelSpec(deployment=n, tier=i + 1) for i, n in enumerate(names)]
    cfg = AgentConfig(models=models, **{k: v for k, v in data.items() if k in AgentConfig.__dataclass_fields__})
    cfg.kb_path = os.getenv("WASHER_KB", cfg.kb_path)
    return cfg
