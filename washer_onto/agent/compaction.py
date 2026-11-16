"""컨텍스트 구성과 압축 (비용 절감).

1) 도구 결과 자르기  : 긴 도구 결과는 앞부분만 모델에 보여준다 (원문은 DB에 남는다).
2) 대화 요약 압축    : 프롬프트가 기준 토큰을 넘으면, 최근 몇 턴을 뺀 앞부분을 가장 저렴한 모델로 요약해
                      '요약 + 최근 대화'만 보낸다. 요약은 누적 갱신된다.
3) 캐시 친화적 순서  : [고정 시스템 프롬프트 + 도구 정의] → [기억·요약] → [대화] 순으로 보내 앞부분이 바뀌지 않게 한다.
                      Azure OpenAI는 동일한 앞부분을 자동 캐시해 입력 단가를 낮춘다.
"""
from __future__ import annotations

import json
from typing import Callable

from .config import AgentConfig
from .sessions import SessionStore

SUMMARY_SYSTEM = """너는 대화 기록을 압축하는 도구다. 이후 대화를 이어가는 데 필요한 정보만 남겨 한국어로 요약하라.
반드시 보존할 것:
- 사용자의 목적과 지시, 선호 (의도)
- 확인된 사실과 수치, 결정 ID·규칙 ID·질문 ID 같은 식별자
- 이미 수행한 작업과 그 결과 (등록·수정·승인/거절 포함)
- 아직 해결되지 않은 질문이나 다음에 할 일
버릴 것: 인사, 중복, 도구 결과의 긴 원문.
'이전 요약'이 주어지면 새 대화 내용을 합쳐 하나의 갱신된 요약으로 다시 써라. 600자 안팎의 글머리표로 쓴다."""


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…(전체 {len(text)}자 중 {limit}자만 표시. 더 필요하면 조건을 좁혀 다시 조회)"


def to_api(m: dict, cfg: AgentConfig) -> dict:
    if m["role"] == "assistant":
        out = {"role": "assistant", "content": m["content"] or None}
        if m["tool_calls"]:
            out["tool_calls"] = [{"id": tc["id"], "type": "function",
                                  "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                                 for tc in m["tool_calls"]]
        return out
    if m["role"] == "tool":
        return {"role": "tool", "tool_call_id": m["tool_call_id"], "content": clip(m["content"], cfg.tool_output_chars)}
    return {"role": "user", "content": m["content"]}


def build_context(store: SessionStore, cfg: AgentConfig, sid: str, system: str) -> list[dict]:
    s = store.session(sid)
    notes = []
    memories = store.memories(s["user"])
    if memories:
        notes.append("이 사용자가 계속 지켜 달라고 한 사항:\n" + "\n".join(f"- {m['text']}" for m in memories))
    if s["summary"]:
        notes.append("이전 대화 요약 (원문은 압축됨):\n" + s["summary"])
    ctx = [{"role": "system", "content": system}]
    ctx.append({"role": "system", "content": f"현재 사용자: {s['user']}\n\n" + "\n\n".join(notes)})
    ctx += [to_api(m, cfg) for m in store.messages(sid, after_id=s["summary_upto"])]
    return ctx


def estimate_tokens(messages: list[dict]) -> int:
    """API 사용량을 모를 때의 근사치 (한글 기준 보수적으로 2자 = 1토큰)."""
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) // 2


def _transcript(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        if m["role"] == "user":
            lines.append(f"[사용자] {m['content']}")
        elif m["role"] == "assistant":
            for tc in m["tool_calls"]:
                lines.append(f"[에이전트 도구 호출] {tc['name']}({clip(tc['arguments'], 300)})")
            if m["content"]:
                lines.append(f"[에이전트] {m['content']}")
        else:
            lines.append(f"[도구 결과 {m['name']}] {clip(m['content'], 600)}")
    return "\n".join(lines)


def compact(store: SessionStore, cfg: AgentConfig, sid: str, llm_call: Callable, force: bool = False) -> dict | None:
    """필요하면 오래된 대화를 요약으로 바꾼다. 압축했으면 결과 정보를, 아니면 None."""
    s = store.session(sid)
    live = store.messages(sid, after_id=s["summary_upto"])
    size = max(s["last_prompt_tokens"], estimate_tokens([to_api(m, cfg) for m in live]))
    if not force and size <= cfg.compact_at_tokens:
        return None
    user_idx = [i for i, m in enumerate(live) if m["role"] == "user"]
    keep = 1 if force else cfg.keep_recent_turns
    if len(user_idx) <= keep:
        return None                       # 최근 턴만 남아 있어 줄일 것이 없다
    cut = user_idx[-keep]                 # 사용자 메시지 경계에서 자르므로 도구 호출/결과 쌍이 갈라지지 않는다
    old = live[:cut]
    model = cfg.cheapest.deployment
    prompt = (f"이전 요약:\n{s['summary']}\n\n" if s["summary"] else "") + "새 대화 내용:\n" + _transcript(old)
    resp = llm_call(model, [{"role": "system", "content": SUMMARY_SYSTEM}, {"role": "user", "content": prompt}], None)
    summary = resp["content"].strip()
    if not summary:
        return None
    store.update_session(sid, summary=summary, summary_upto=old[-1]["id"], last_prompt_tokens=0)
    info = {"model": model, "messages_compacted": len(old), "tokens_before": size, "usage": resp["usage"]}
    store.log(sid, "compaction", **info)
    return info
