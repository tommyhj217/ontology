import json
import sys
from pathlib import Path

import pytest

from washer_onto.agent import Agent, AgentConfig, ModelSpec
from washer_onto.agent.core import SYSTEM
from washer_onto.agent.router import complexity
from washer_onto.cli import _load_records
from washer_onto.store import KB

EXAMPLES = Path(__file__).parent.parent / "examples" / "decisions.yaml"
U = {"prompt": 100, "completion": 20, "cached": 0}


def say(text, usage=U):
    return {"content": text, "tool_calls": [], "usage": dict(usage)}


def call(name, args, id="c1", usage=U):
    arguments = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
    return {"content": "", "usage": dict(usage), "tool_calls": [{"id": id, "name": name, "arguments": arguments}]}


class FakeLLM:
    """대본대로 응답하고, 받은 호출을 기록한다. 대본 항목이 예외면 던진다."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def __call__(self, model, messages, tools):
        self.calls.append({"model": model, "messages": messages,
                           "tools": [t["function"]["name"] for t in tools or []]})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def env(tmp_path):
    kb = KB(tmp_path / "kb.ttl")
    for rec in _load_records(EXAMPLES):
        kb.upsert(rec)
    kb.save()

    def make(*script, models=("small", "mid", "big"), **kw):
        cfg = AgentConfig(models=[ModelSpec(m, tier=i + 1) for i, m in enumerate(models)],
                          kb_path=str(tmp_path / "kb.ttl"), db_path=str(tmp_path / "s.db"), **kw)
        llm = FakeLLM(*script)
        return Agent(cfg, llm_call=llm), llm
    return make


def test_tool_loop_answers_from_kb(env):
    agent, llm = env(call("list_decisions", {"verdict": "부적합"}), say("부적합은 댐퍼-선정-2026 1건입니다."))
    sid = agent.start("홍길동")
    res = agent.send(sid, "부적합 결정 목록 보여줘")
    assert res.status == "done" and "댐퍼" in res.reply
    tool_msg = llm.calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and "서스펜션 댐퍼 선정" in tool_msg["content"]
    assert llm.calls[0]["messages"][0] == {"role": "system", "content": SYSTEM}     # 고정 프롬프트가 맨 앞
    assert agent.store.session(sid)["title"] == "부적합 결정 목록 보여줘"


def test_write_tool_waits_for_approval_then_runs(env, tmp_path):
    from washer_onto import rules as R
    kb = KB(tmp_path / "kb.ttl")
    R.mine_rules(kb)
    kb.save()
    agent, llm = env(call("answer_rule_question",
                          {"question_id": "모터-효율-q1", "answer": R.OPT_MANDATORY, "by": "김모터"}),
                     say("확정했습니다."))
    sid = agent.start("김모터")
    res = agent.send(sid, "모터 효율 질문은 필수 규칙이 맞아")
    assert res.status == "needs_approval" and res.pending[0]["name"] == "answer_rule_question"
    assert KB(tmp_path / "kb.ttl").get_rule("모터-효율").status == "제안"      # 아직 실행 안 됨
    assert len(llm.calls) == 1

    res = Agent(agent.cfg, llm_call=llm).resume(sid, {res.pending[0]["id"]: True})   # 새 프로세스에서 이어가도 됨
    assert res.status == "done"
    assert KB(tmp_path / "kb.ttl").get_rule("모터-효율").status == "확정"
    assert [e["detail"]["approved"] for e in agent.store.events(sid, "approval")] == [True]


def test_denied_tool_is_not_run(env, tmp_path):
    agent, llm = env(call("mine_rules", {}), say("알겠습니다. 실행하지 않았습니다."))
    sid = agent.start("u")
    res = agent.send(sid, "규칙 후보 만들어줘")
    res = agent.resume(sid, {res.pending[0]["id"]: False})
    assert res.status == "done" and not KB(tmp_path / "kb.ttl").rules()
    assert "거절" in llm.calls[1]["messages"][-1]["content"]


def test_new_message_while_pending_cancels_the_call(env, tmp_path):
    agent, llm = env(call("mine_rules", {}), say("네, 다른 걸 도와드릴게요."))
    sid = agent.start("u")
    agent.send(sid, "규칙 후보 만들어줘")
    assert agent.send(sid, "아니 됐어").status == "done"
    roles = [m["role"] for m in llm.calls[1]["messages"]]
    assert roles[-3:] == ["assistant", "tool", "user"] and not KB(tmp_path / "kb.ttl").rules()


def test_router_picks_by_difficulty_and_respects_pin(env):
    hard = "왜 FD-120을 골랐는지 근거를 분석하고 다른 후보와 비교해줘"
    assert (complexity("결정 목록 보여줘"), complexity(hard)) == (0, 2)
    agent, llm = env(say("a"), say("b"), say("c"), say("d"))
    sid = agent.start("u")
    agent.send(sid, "결정 목록 보여줘")
    agent.send(sid, hard)
    assert agent.send(sid, "/model mid").status == "command"
    agent.send(sid, "결정 목록 보여줘")
    agent.send(sid, "/model auto")
    agent.send(sid, "결정 목록 보여줘")
    assert [c["model"] for c in llm.calls] == ["small", "big", "mid", "small"]
    assert "없습니다" in agent.send(sid, "/model nope").reply


def test_model_can_escalate_itself(env):
    agent, llm = env(call("escalate", {"reason": "여러 결정 교차 분석"}), say("분석 결과입니다."))
    sid = agent.start("u")
    res = agent.send(sid, "목록 보여줘")
    assert [c["model"] for c in llm.calls] == ["small", "mid"] and res.model == "mid"
    assert "escalate" in llm.calls[0]["tools"]
    assert agent.store.events(sid, "model_switch")[0]["detail"]["to"] == "mid"
    assert [m["model"] for m in agent.store.messages(sid) if m["role"] == "assistant"] == ["small", "mid"]


def test_repeated_tool_errors_escalate(env):
    agent, llm = env(call("get_decision", "{bad json", id="a"), call("no_such_tool", {}, id="b"), say("완료"))
    sid = agent.start("u")
    agent.send(sid, "목록 보여줘")
    assert [c["model"] for c in llm.calls] == ["small", "small", "mid"]
    assert "오류 반복" in agent.store.events(sid, "model_switch")[0]["detail"]["reason"]


def test_fallback_when_deployment_fails(env):
    agent, llm = env(RuntimeError("404 DeploymentNotFound"), say("대체 모델 답변"))
    sid = agent.start("u")
    res = agent.send(sid, "목록 보여줘")
    assert res.reply == "대체 모델 답변" and [c["model"] for c in llm.calls] == ["small", "mid"]
    assert any("대체" in n for n in res.notes)
    agent2, _ = env(RuntimeError("down"), models=("only",))
    with pytest.raises(RuntimeError):
        agent2.send(agent2.start("u"), "목록 보여줘")


def test_compaction_replaces_old_turns_with_summary(env):
    big = {"prompt": 5000, "completion": 20, "cached": 0}
    agent, llm = env(say("답1"), say("답2"), say("답3", usage=big),
                     say("요약: 사용자는 모터 결정을 검토 중. 결정 ID DD-모터-선정-2026 확인함."), say("답4"),
                     compact_at_tokens=1000, keep_recent_turns=2)
    sid = agent.start("u")
    for q in ("질문1", "질문2", "질문3"):
        agent.send(sid, q)
    res = agent.send(sid, "질문4")
    assert any("압축" in n for n in res.notes)
    summary_call, final_call = llm.calls[3], llm.calls[4]
    assert summary_call["model"] == "small" and "질문1" in summary_call["messages"][1]["content"]
    assert "질문3" not in summary_call["messages"][1]["content"]           # 최근 2턴은 요약 대상이 아님
    sent = json.dumps(final_call["messages"], ensure_ascii=False)
    assert "이전 대화 요약" in sent and "DD-모터-선정-2026" in sent
    assert "질문1" not in sent and "질문3" in sent and "질문4" in sent
    assert len(agent.store.messages(sid)) == 8                              # 원문 이력은 그대로 남는다
    assert "small" in agent.store.usage(sid) and agent.store.events(sid, "compaction")
    llm.script += [say("갱신된 요약")]                                       # 요약은 누적 갱신된다
    assert agent.send(sid, "/compact").reply.startswith("메시지")
    assert "이전 요약" in llm.calls[5]["messages"][1]["content"]


def test_long_tool_output_is_clipped_in_context_but_kept_in_history(env):
    agent, llm = env(call("get_decision", {"decision_id": "DD-모터-선정-2026"}), say("ok"), tool_output_chars=200)
    sid = agent.start("u")
    agent.send(sid, "모터 결정 보여줘")
    shown = llm.calls[1]["messages"][-1]["content"]
    stored = [m for m in agent.store.messages(sid) if m["role"] == "tool"][0]["content"]
    assert len(shown) < 300 and "200자만 표시" in shown and len(stored) > 1000


def test_memory_persists_across_sessions_per_user(env):
    agent, llm = env(call("remember", {"note": "항상 표로 답하기"}), say("앞으로 표로 답하겠습니다."),
                     say("…"), say("…"))
    agent.send(agent.start("홍길동"), "앞으로 항상 표로 답해줘")
    s2, other = agent.start("홍길동"), agent.start("다른사람")
    agent.send(s2, "목록 보여줘")
    agent.send(other, "목록 보여줘")
    assert "항상 표로 답하기" in llm.calls[2]["messages"][1]["content"]
    assert "항상 표로 답하기" not in llm.calls[3]["messages"][1]["content"]
    mem_id = agent.store.memories("홍길동")[0]["id"]
    assert agent.send(s2, f"/forget {mem_id}").reply == "삭제했습니다." and not agent.store.memories("홍길동")
    assert len(agent.store.list_sessions("홍길동")) == 2


def test_max_steps_stops_runaway_loop(env):
    agent, llm = env(*[call("list_rules", {}, id=f"c{i}") for i in range(3)], max_steps=3)
    assert agent.send(agent.start("u"), "목록").status == "max_steps"


def test_mcp_tools_join_the_agent_and_need_approval_unless_allowed(env, monkeypatch):
    from washer_onto.agent.mcp_client import MCPManager
    agent, llm = env(call("mcp__washer__list_decisions", {"verdict": "적합"}, id="a"),
                     call("mcp__washer__get_decision", {"decision_id": "댐퍼-선정-2026"}, id="b"), say("끝"))
    monkeypatch.setenv("WASHER_KB", agent.cfg.kb_path)
    agent.mcp = MCPManager({"washer": {"command": sys.executable, "args": ["-m", "washer_onto.mcp_server"],
                                       "auto_approve": ["list_decisions"]},
                            "broken": {"command": "no-such-binary-xyz"}})
    try:
        assert "연결됨" in agent.mcp.status()["washer"] and "연결 실패" in agent.mcp.status()["broken"]
        sid = agent.start("u")
        res = agent.send(sid, "목록")
        assert "DD-모터-선정-2026" in llm.calls[1]["messages"][-1]["content"]        # 허용 목록 도구는 바로 실행
        assert res.status == "needs_approval" and res.pending[0]["source"] == "MCP:washer"
        assert agent.resume(sid, {"b": True}).status == "done"
        assert "FD-120" in llm.calls[2]["messages"][-1]["content"]
    finally:
        agent.mcp.close()


def test_approval_is_not_reused_when_tool_call_ids_collide(env, tmp_path):
    agent, llm = env(call("mine_rules", {}, id="same"), say("만들었습니다."),
                     call("mine_rules", {}, id="same"))
    sid = agent.start("u")
    res = agent.send(sid, "규칙 후보 만들어줘")
    agent.resume(sid, {res.pending[0]["id"]: True})
    res = agent.send(sid, "한 번 더")
    assert res.status == "needs_approval"          # 같은 ID라도 새 호출은 다시 승인받아야 한다
