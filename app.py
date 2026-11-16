"""웹 화면:  streamlit run app.py

사내 구성원이 선정 결정을 등록·검증·조회하는 화면.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

from washer_onto import llm
from washer_onto.audit import ERROR, FAIL, NEEDS_WORK, PASS, WARNING, audit
from washer_onto.models import Candidate, Criterion, DecisionRecord, Evaluation, Evidence
from washer_onto.store import KB

KB_PATH = os.getenv("WASHER_KB", "data/kb.ttl")
BADGE = {PASS: "🟢 적합", NEEDS_WORK: "🟡 보완필요", FAIL: "🔴 부적합"}
OPS = [">=", "<=", ">", "<", "=="]

st.set_page_config(page_title="세탁기 모델 선정 온톨로지", layout="wide")
kb = KB(KB_PATH)   # 매 실행마다 파일에서 다시 읽어 여러 사용자의 변경을 반영한다.


def _clean(v):
    return None if v is None or (isinstance(v, float) and pd.isna(v)) or v == "" else v


def _s(v) -> str:
    v = _clean(v)
    return "" if v is None else str(v).strip()


def save_record(rec: DecisionRecord) -> None:
    did = kb.upsert(rec)
    kb.save()
    rep = audit(kb, did)
    st.success(f"저장했습니다: {rec.title} → 검증 결과 {BADGE[rep.verdict]}")
    show_report(rep, kb.get(did))


def show_report(rep, rec: DecisionRecord) -> None:
    st.subheader(f"{BADGE[rep.verdict]} · {rec.title}")
    st.caption(f"선정: **{rec.chosen}** · 결정자: {', '.join(rec.decided_by) or '–'} · "
               f"검토자: {', '.join(rec.reviewed_by) or '–'} · 결정일: {rec.decided_on or '–'}")
    if rec.rationale:
        st.write(rec.rationale)
    for f in rep.findings:
        (st.error if f.severity == ERROR else st.warning if f.severity == WARNING else st.info)(f.message)
    if not rep.findings:
        st.success("지적 사항이 없습니다.")

    if rec.candidates and rec.criteria:
        rows = []
        for c in rec.criteria:
            spec = f"{c.operator} {c.threshold:g}{c.unit}" if c.operator and c.threshold is not None else "정성"
            row = {"기준": c.name, "조건": spec, "가중치": c.weight, "필수": "예" if c.mandatory else ""}
            for cand in rec.candidates:
                e = rec.evaluation(cand.name, c.name)
                if e is None or (e.value is None and e.rating is None):
                    txt = "–"
                elif e.value is not None:
                    txt = f"{e.value:g}" + (" ✗" if c.passes(e.value) is False else "")
                else:
                    txt = f"평점 {e.rating:g}"
                row[cand.name + (" ✔" if cand.name == rec.chosen else "")] = txt
            rows.append(row)
        score = {"기준": "가중 점수(100점)", "조건": "", "가중치": None, "필수": ""}
        for cand in rec.candidates:
            score[cand.name + (" ✔" if cand.name == rec.chosen else "")] = (
                f"{rep.scores.get(cand.name, 0):g}" + ("" if rep.eligible.get(cand.name, True) else " (필수 미달)"))
        st.dataframe(pd.DataFrame(rows + [score]), hide_index=True, width="stretch")

    ev_rows = [{"후보": e.candidate, "기준": e.criterion, "유형": ev.type, "문서": ev.reference,
                "인용": ev.quote, "원문 확인": {True: "확인", False: "미확인", None: ""}[ev.grounded]}
               for e in rec.evaluations for ev in e.evidence]
    if ev_rows:
        with st.expander(f"근거 자료 {len(ev_rows)}건"):
            st.dataframe(pd.DataFrame(ev_rows), hide_index=True, width="stretch")


def graph_dot(rec: DecisionRecord) -> str:
    q = lambda s: '"' + str(s).replace('"', "'") + '"'
    lines = ["digraph G { rankdir=LR; node [shape=box, style=rounded, fontname=sans];",
             f"D [label={q(rec.title)}, style=\"rounded,filled\", fillcolor=\"#dbeafe\"];"]
    for i, c in enumerate(rec.candidates):
        chosen = c.name == rec.chosen
        lines.append(f"C{i} [label={q(c.name)}" + (", style=\"rounded,filled\", fillcolor=\"#bbf7d0\"" if chosen else "") + "];")
        lines.append(f"D -> C{i} [label={q('선정' if chosen else '후보')}];")
    for i, c in enumerate(rec.criteria):
        spec = f"\\n{c.operator} {c.threshold:g}{c.unit}" if c.operator and c.threshold is not None else ""
        lines.append(f"K{i} [label=\"{c.name}{spec}\", shape=ellipse];")
        lines.append(f"D -> K{i} [label=\"기준\"];")
    for p in rec.decided_by:
        lines.append(f"{q('P' + p)} [label={q(p)}, shape=plaintext]; {q('P' + p)} -> D [label=\"결정\"];")
    for p in rec.reviewed_by:
        lines.append(f"{q('P' + p)} [label={q(p)}, shape=plaintext]; {q('P' + p)} -> D [label=\"검토\"];")
    return "\n".join(lines) + "}"


st.title("세탁기 개발 모델 선정 온톨로지")
st.caption("어떤 기준과 근거로 모델을 골랐는지 기록하고, 기준대로 골랐는지 검증합니다.")
tab_chat, tab_list, tab_new, tab_doc, tab_rule, tab_ask, tab_out = st.tabs(
    ["대화", "결정 목록·검증", "결정 등록", "문서에서 추출", "규칙·질문", "질의", "내보내기"])


# ---------------- 대화 (에이전트) ----------------
@st.cache_resource
def get_agent():
    """에이전트는 MCP 연결을 유지하므로 프로세스당 하나만 만든다."""
    from washer_onto.agent.repl import make_agent
    return make_agent()


with tab_chat:
    NEW = "(새 대화)"
    ss = st.session_state
    try:
        agent = get_agent()
    except Exception as err:
        agent = None
        st.warning(f"에이전트를 시작할 수 없습니다: {err}")
    if agent:
        if "_goto" in ss:                     # 새 세션을 만든 직후 선택 상자를 그 세션으로 옮긴다
            ss["chat_sid"] = ss.pop("_goto")
        c1, c2, c3 = st.columns([2, 4, 2])
        chat_user = c1.text_input("사용자 이름 *", key="chat_user", help="대화 이력과 기억이 이름별로 관리됩니다.")
        sessions = agent.store.list_sessions(chat_user) if chat_user.strip() else []
        titles = {s["id"]: f"{s['title'] or '제목 없음'} · {s['updated_at'][:16].replace('T', ' ')}" for s in sessions}
        if ss.get("chat_sid") not in titles:
            ss["chat_sid"] = NEW
        sid = c2.selectbox("대화", [NEW, *titles], format_func=lambda i: titles.get(i, i), key="chat_sid")
        sid = None if sid == NEW else sid
        names = [m.deployment for m in agent.cfg.models]
        pinned = agent.store.session(sid)["pinned_model"] if sid else ss.get("new_pin", "")
        choice = c3.selectbox("모델", ["자동 선택", *names], index=names.index(pinned) + 1 if pinned in names else 0,
                              key=f"model-{sid}", help="자동 선택은 질문 난이도에 따라 고르고, 필요하면 상위 모델로 바꿉니다.")
        choice = "" if choice == "자동 선택" else choice
        if sid and choice != pinned:
            agent.store.update_session(sid, pinned_model=choice)
            agent.store.log(sid, "command", text=f"/model {choice or 'auto'}")
        elif not sid:
            ss["new_pin"] = choice

        if not chat_user.strip():
            st.info("사용자 이름을 입력하면 대화를 시작할 수 있습니다.")
        else:
            msgs = agent.store.messages(sid) if sid else []
            results = {m["tool_call_id"]: m["content"] for m in msgs if m["role"] == "tool"}
            upto = agent.store.session(sid)["summary_upto"] if sid else 0
            for m in msgs:
                if m["role"] == "tool":
                    continue
                with st.chat_message(m["role"]):
                    for tc in m["tool_calls"]:
                        with st.expander(f"도구 호출: {tc['name']}"):
                            st.code(tc["arguments"], "json")
                            st.text(results.get(tc["id"], "(승인 대기 또는 미실행)")[:3000])
                    if m["content"]:
                        st.markdown(m["content"])
                    if m["role"] == "assistant":
                        st.caption(f"{m['model']} · 입력 {m['prompt_tokens']} (캐시 {m['cached_tokens']}) · 출력 {m['completion_tokens']} 토큰"
                                   + (" · 압축됨(요약으로 대체)" if m["id"] <= upto else ""))
            for note in ss.pop("chat_notes", []):
                st.caption(f"· {note}")

            pending = agent._pending(sid) if sid else []
            for p in pending:
                with st.container(border=True):
                    st.markdown(f"**승인 필요** — {p['source']} 도구 `{p['name']}`")
                    st.code(p["arguments"], "json")
                    b1, b2, _ = st.columns([1, 1, 6])
                    decision = True if b1.button("승인", key=f"ok-{p['id']}", type="primary") else \
                        False if b2.button("거절", key=f"no-{p['id']}") else None
                    if decision is not None:
                        try:
                            with st.spinner("진행 중…"):
                                ss["chat_notes"] = agent.resume(sid, {p["id"]: decision}).notes
                        except Exception as err:
                            ss["chat_notes"] = [f"오류: {err}"]
                        st.rerun()

            prompt = st.chat_input("질문을 입력하세요 (/help 로 명령어 보기)", key="chat_input")
            if prompt:
                if not sid:
                    sid = agent.start(chat_user.strip())
                    if ss.get("new_pin"):
                        agent.store.update_session(sid, pinned_model=ss.pop("new_pin"))
                    ss["_goto"] = sid
                try:
                    with st.spinner("생각 중…"):
                        res = agent.send(sid, prompt)
                    ss["chat_notes"] = res.notes + ([res.reply] if res.status in ("command", "max_steps") else [])
                except Exception as err:
                    ss["chat_notes"] = [f"오류: {type(err).__name__}: {err}"]
                st.rerun()

            if sid:
                with st.expander("사용량 · 이력 · 기억"):
                    for model, u in agent.store.usage(sid).items():
                        spec = agent.cfg.model(model)
                        cost = spec.cost(u["prompt"], u["completion"], u["cached"]) if spec else None
                        st.write(f"**{model}** — 호출 {u['calls']}회 · 입력 {u['prompt']} (캐시 {u['cached']}) · 출력 {u['completion']} 토큰"
                                 + (f" · 비용 약 {cost:.4f}" if cost is not None else ""))
                    summary = agent.store.session(sid)["summary"]
                    if summary:
                        st.markdown("**압축된 이전 대화 요약**")
                        st.text(summary)
                    events = agent.store.events(sid)
                    if events:
                        st.markdown("**동작 이력**")
                        st.dataframe(pd.DataFrame([{"시각": e["at"], "종류": e["kind"],
                                                    "내용": json.dumps(e["detail"], ensure_ascii=False)} for e in events]),
                                     hide_index=True, width="stretch")
                    mems = agent.store.memories(chat_user.strip())
                    if mems:
                        st.markdown("**내 지시·선호 (모든 대화에 적용)**")
                        for mem in mems:
                            m1, m2 = st.columns([8, 1])
                            m1.write(mem["text"])
                            if m2.button("삭제", key=f"mem-{mem['id']}"):
                                agent.store.forget(chat_user.strip(), mem["id"])
                                st.rerun()
                    if agent.mcp:
                        st.markdown("**MCP 서버**")
                        for name, status in agent.mcp.status().items():
                            st.write(f"{name}: {status}")
                    d1, d2, _ = st.columns([2, 2, 6])
                    if d1.button("지금 압축"):
                        info = agent.compact_now(sid)
                        ss["chat_notes"] = [f"메시지 {info['messages_compacted']}개를 요약으로 압축했습니다." if info
                                            else "압축할 이전 대화가 없습니다."]
                        st.rerun()
                    if d2.button("이 대화 삭제"):
                        agent.store.delete_session(sid)
                        st.rerun()

# ---------------- 결정 목록·검증 ----------------
with tab_list:
    records = kb.all()
    if not records:
        st.info("등록된 결정이 없습니다. '결정 등록' 또는 '문서에서 추출' 탭에서 추가하세요.")
    else:
        reports = {r.id: audit(kb, r.id) for r in records}
        cols = st.columns(3)
        for col, v in zip(cols, (PASS, NEEDS_WORK, FAIL)):
            col.metric(BADGE[v], sum(1 for r in reports.values() if r.verdict == v))
        st.dataframe(pd.DataFrame([{
            "검증": BADGE[reports[r.id].verdict], "결정": r.title, "분류": r.category, "프로젝트": r.project,
            "선정 모델": r.chosen, "결정자": ", ".join(r.decided_by), "결정일": r.decided_on or "",
            "지적": len([f for f in reports[r.id].findings if f.severity != "참고"])} for r in records]),
            hide_index=True, width="stretch")
        pick = st.selectbox("상세 보기", [r.id for r in records],
                            format_func=lambda i: next(r.title for r in records if r.id == i))
        rec = kb.get(pick)
        show_report(reports[pick], rec)
        with st.expander("관계 그래프"):
            st.graphviz_chart(graph_dot(rec))
        with st.expander("YAML 원본 / 삭제"):
            st.code(yaml.safe_dump(rec.model_dump(exclude_none=True), allow_unicode=True, sort_keys=False), "yaml")
            if st.button("이 결정 삭제", type="secondary"):
                kb.delete(pick)
                kb.save()
                st.rerun()

# ---------------- 결정 등록 ----------------
with tab_new:
    st.markdown("후보·기준·평가를 표에 입력하세요. 같은 제목으로 다시 저장하면 기존 기록을 교체합니다.")
    c1, c2, c3 = st.columns(3)
    title = c1.text_input("결정 제목 *", placeholder="드럼세탁기 24kg DD 모터 선정")
    project = c2.text_input("프로젝트")
    category = c3.text_input("분류", placeholder="모터 / 댐퍼 / AI 모델 …")
    c1, c2, c3 = st.columns(3)
    decided_by = c1.text_input("결정자 (쉼표로 구분)")
    reviewed_by = c2.text_input("검토자 (쉼표로 구분)")
    decided_on = c3.date_input("결정일", value=None)
    rationale = st.text_area("선정 사유")

    st.markdown("**후보 모델** (탈락한 후보도 모두)")
    cand_df = st.data_editor(pd.DataFrame({"모델명": pd.Series(dtype=str), "제조사": pd.Series(dtype=str)}),
                             num_rows="dynamic", width="stretch", key="cand")
    names = [_s(n) for n in cand_df["모델명"] if _s(n)]
    chosen = st.selectbox("최종 선정 모델 *", names) if names else None

    st.markdown("**선정 기준** (정량 기준은 연산자·임계값 입력, 정성 기준은 비워두고 평점으로 평가)")
    from washer_onto.rules import criteria_for
    preset = criteria_for(kb, category.strip()) if category.strip() else []
    if preset:
        st.info(f"'{category.strip()}' 분류의 확정 규칙 {len(preset)}건을 기준에 미리 채웠습니다. 필요하면 수정하세요.")
    crit_df = st.data_editor(
        pd.DataFrame({"기준": pd.Series([c.name for c in preset], dtype=str),
                      "연산자": pd.Series([c.operator for c in preset], dtype=str),
                      "임계값": pd.Series([c.threshold for c in preset], dtype=float),
                      "단위": pd.Series([c.unit for c in preset], dtype=str),
                      "가중치": pd.Series([c.weight for c in preset], dtype=float),
                      "필수": pd.Series([c.mandatory for c in preset], dtype=bool)}),
        num_rows="dynamic", width="stretch", key=f"crit-{category.strip() if preset else ''}",
        column_config={"연산자": st.column_config.SelectboxColumn(options=OPS),
                       "가중치": st.column_config.NumberColumn(default=1.0, min_value=0.1),
                       "필수": st.column_config.CheckboxColumn(default=False)})
    crit_names = [_s(n) for n in crit_df["기준"] if _s(n)]

    st.markdown("**평가** (후보 × 기준마다 한 줄)")
    eval_df = st.data_editor(
        pd.DataFrame({"후보": pd.Series(dtype=str), "기준": pd.Series(dtype=str), "측정값": pd.Series(dtype=float),
                      "평점(1~5)": pd.Series(dtype=float), "근거 유형": pd.Series(dtype=str),
                      "근거 문서번호": pd.Series(dtype=str), "비고": pd.Series(dtype=str)}),
        num_rows="dynamic", width="stretch", key="eval",
        column_config={"후보": st.column_config.SelectboxColumn(options=names),
                       "기준": st.column_config.SelectboxColumn(options=crit_names),
                       "근거 유형": st.column_config.SelectboxColumn(
                           options=["시험성적서", "시뮬레이션", "사양서", "회의록", "견적서", "기타"])})

    if st.button("저장하고 검증", type="primary"):
        if not title or not chosen:
            st.error("결정 제목과 최종 선정 모델은 필수입니다.")
        else:
            split = lambda s: [x.strip() for x in s.split(",") if x.strip()]
            save_record(DecisionRecord(
                title=title, project=project, category=category, chosen=chosen, rationale=rationale,
                decided_by=split(decided_by), reviewed_by=split(reviewed_by),
                decided_on=decided_on.isoformat() if decided_on else None,
                candidates=[Candidate(name=_s(r["모델명"]), maker=_s(r["제조사"]))
                            for _, r in cand_df.iterrows() if _s(r["모델명"])],
                criteria=[Criterion(name=_s(r["기준"]), operator=_clean(r["연산자"]), threshold=_clean(r["임계값"]),
                                    unit=_s(r["단위"]), weight=_clean(r["가중치"]) or 1.0, mandatory=bool(_clean(r["필수"])))
                          for _, r in crit_df.iterrows() if _s(r["기준"])],
                evaluations=[Evaluation(
                    candidate=_s(r["후보"]), criterion=_s(r["기준"]), value=_clean(r["측정값"]),
                    rating=_clean(r["평점(1~5)"]), note=_s(r["비고"]),
                    evidence=[Evidence(type=_s(r["근거 유형"]) or "기타", reference=_s(r["근거 문서번호"]))]
                    if _s(r["근거 유형"]) or _s(r["근거 문서번호"]) else [])
                    for _, r in eval_df.iterrows() if _s(r["후보"]) and _s(r["기준"])]))

# ---------------- 문서에서 추출 ----------------
with tab_doc:
    st.markdown("회의록·검토보고서를 올리면 Azure OpenAI가 결정 내용을 추출합니다. "
                "**추출 결과는 초안입니다.** 내용을 확인·수정한 뒤 저장하세요.")
    if not llm.is_configured():
        st.warning("Azure OpenAI 설정이 없습니다. `.env` 에 키·엔드포인트·배포명을 넣어주세요 (`.env.example` 참고).")
    up = st.file_uploader("문서 (txt / md / pdf / docx)", type=["txt", "md", "pdf", "docx"])
    pasted = st.text_area("또는 본문 붙여넣기", height=160)
    if st.button("추출", disabled=not llm.is_configured()):
        from washer_onto.extract import extract, read_document
        text, source = pasted, "붙여넣은 본문"
        if up is not None:
            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / ("doc" + Path(up.name).suffix.lower())
                p.write_bytes(up.getvalue())
                text, source = read_document(p), up.name
        if not text.strip():
            st.error("문서 내용이 비어 있습니다.")
        else:
            try:
                with st.spinner("추출 중…"):
                    recs = extract(text, source=source)
                st.session_state["draft"] = yaml.safe_dump(
                    {"decisions": [r.model_dump(exclude_none=True) for r in recs]}, allow_unicode=True, sort_keys=False)
                if not recs:
                    st.info("문서에서 선정 의사결정을 찾지 못했습니다.")
            except Exception as err:
                st.error(f"추출 실패: {err}")
    if st.session_state.get("draft"):
        draft = st.text_area("추출 결과 (수정 가능한 YAML) — `grounded: false` 는 인용문이 원문에서 확인되지 않았다는 뜻입니다.",
                             st.session_state["draft"], height=420)
        if st.button("검토 완료, 저장", type="primary"):
            try:
                for d in (yaml.safe_load(draft) or {}).get("decisions", []):
                    save_record(DecisionRecord.model_validate(d))
                st.session_state["draft"] = ""
            except Exception as err:
                st.error(f"형식 오류: {err}")

# ---------------- 규칙 ----------------
with tab_rule:
    from washer_onto import rules as R
    st.markdown("에이전트가 결정과 문서에서 **규칙 후보**를 찾아 질문합니다. 담당 개발자가 답하면 규칙이 확정·수정·폐기되고, "
                "확정된 규칙은 이후 모든 결정의 검증과 등록 폼에 적용됩니다.")
    if st.session_state.get("rule_msg"):
        st.success(st.session_state.pop("rule_msg"))
    c1, c2 = st.columns(2)
    with c1:
        if st.button("기존 결정에서 규칙 찾기"):
            res = R.mine_rules(kb)
            kb.save()
            st.session_state["rule_msg"] = f"규칙 후보 {res['proposed']}건을 제안/갱신했습니다. 대기 질문 {res['open_questions']}건."
            st.rerun()
    with c2:
        rdoc = st.file_uploader("설계 기준서·회의록에서 규칙 찾기 (Azure OpenAI)", type=["txt", "md", "pdf", "docx"], key="ruledoc")
        if st.button("문서에서 규칙 추출", disabled=rdoc is None or not llm.is_configured()):
            from washer_onto.extract import read_document
            try:
                with tempfile.TemporaryDirectory() as tmp, st.spinner("추출 중…"):
                    pth = Path(tmp) / ("doc" + Path(rdoc.name).suffix.lower())
                    pth.write_bytes(rdoc.getvalue())
                    res = R.extract_rules(kb, read_document(pth), source=rdoc.name)
                kb.save()
                st.session_state["rule_msg"] = f"문서에서 규칙 후보 {res['proposed']}건을 제안했습니다. 대기 질문 {res['open_questions']}건."
                st.rerun()
            except Exception as err:
                st.error(f"추출 실패: {err}")

    open_qs = kb.questions(status="open")
    st.subheader(f"답변을 기다리는 질문 {len(open_qs)}건")
    who = st.text_input("답변자 이름 *", key="answerer")
    FREE = "직접 입력"
    for q in open_qs:
        rule = kb.get_rule(q.rule_id)
        with st.container(border=True):
            st.markdown(f"**{rule.category} · {rule.criterion}** — 현재 후보: `{rule.spec()}` · {'필수' if rule.mandatory else '권장'}")
            st.write(q.text)
            choice = st.radio("답변", q.options + [FREE], key=f"opt-{q.id}", index=None, label_visibility="collapsed") if q.options else FREE
            free = ""
            if choice == FREE:
                free = st.text_area("답변 내용 (수치·조건·이유를 자유롭게 적으면 에이전트가 규칙에 반영합니다)", key=f"free-{q.id}")
                if not llm.is_configured():
                    st.caption("자유 서술 답변을 해석하려면 Azure OpenAI 설정(.env)이 필요합니다.")
            if st.button("답변 제출", key=f"btn-{q.id}"):
                if not who.strip():
                    st.error("답변자 이름을 입력하세요.")
                elif choice is None:
                    st.error("답변을 선택하세요.")
                else:
                    try:
                        with st.spinner("규칙 갱신 중…"):
                            new_rule, summary = R.answer(kb, q.id, free if choice == FREE else choice, who)
                        kb.save()
                        st.session_state["rule_msg"] = f"[{new_rule.status}] {new_rule.statement} — 변경: {summary}"
                        st.rerun()
                    except Exception as err:
                        st.error(f"반영 실패: {err}")

    all_rules = kb.rules()
    st.subheader(f"규칙 {len(all_rules)}건")
    if all_rules:
        mark = {"확정": "🟢 확정", "제안": "🟡 제안", "폐기": "⚪ 폐기"}
        st.dataframe(pd.DataFrame([{
            "상태": mark[r.status], "분류": r.category, "기준": r.criterion, "조건": r.spec(),
            "구분": "필수" if r.mandatory else "권장", "규칙": r.statement, "이유": r.rationale,
            "확정자": r.confirmed_by, "확정일": r.confirmed_on or ""} for r in all_rules]),
            hide_index=True, width="stretch")
        rid = st.selectbox("변경 이력 보기", [r.id for r in all_rules],
                           format_func=lambda i: next(f"{r.category} · {r.criterion}" for r in all_rules if r.id == i))
        sel = kb.get_rule(rid)
        for rev in sel.history:
            st.markdown(f"- `{rev.at}` **{rev.by}** — {rev.note}")
        c1, c2 = st.columns(2)
        if sel.status != "제안" and c1.button("다시 검토 대상으로 되돌리기 (제안 상태)"):
            from washer_onto.models import Revision
            sel.status, sel.confirmed_by, sel.confirmed_on = "제안", "", None
            sel.history.append(Revision(at=R._now(), by=f"human:{who.strip() or '미상'}", note="제안 상태로 되돌림"))
            kb.upsert_rule(sel)
            kb.save()
            st.rerun()
        if c2.button("이 규칙 삭제"):
            kb.delete_rule(rid)
            kb.save()
            st.rerun()

# ---------------- 질의 ----------------
with tab_ask:
    st.markdown("예: *소음 기준이 사용된 결정은?*, *김모터가 결정한 건에서 탈락한 후보와 그 측정값은?*")
    question = st.text_input("질문")
    if st.button("질문하기", disabled=not llm.is_configured()) and question:
        from washer_onto.qa import ask
        try:
            with st.spinner("조회 중…"):
                res = ask(kb, question)
            st.write(res["answer"])
            if res["rows"]:
                st.dataframe(pd.DataFrame(res["rows"]), hide_index=True, width="stretch")
            with st.expander("사용된 SPARQL"):
                st.code(res["sparql"], "sparql")
        except Exception as err:
            st.error(f"질의 실패: {err}")
    with st.expander("SPARQL 직접 실행 (LLM 불필요)"):
        sparql = st.text_area("쿼리", "SELECT ?결정 ?선정모델 WHERE {\n  ?d a wm:SelectionDecision ; rdfs:label ?결정 ;\n"
                                      "     wm:chosenCandidate/rdfs:label ?선정모델 .\n}", height=140)
        if st.button("실행"):
            try:
                result = kb.query(sparql)
                st.dataframe(pd.DataFrame([[("" if v is None else str(v)) for v in row] for row in result],
                                          columns=[str(v) for v in result.vars or []]), hide_index=True, width="stretch")
            except Exception as err:
                st.error(f"쿼리 오류: {err}")

# ---------------- 내보내기 ----------------
with tab_out:
    st.markdown("- **RDF(Turtle)**: Protégé, GraphDB, Fuseki 등 표준 온톨로지 도구에서 열 수 있습니다.\n"
                "- **OKF 번들**: Google Open Knowledge Format(마크다운+YAML). 사람이 읽거나 사내 AI 에이전트의 지식으로 씁니다.")
    st.download_button("RDF(Turtle) 내려받기", kb.g.serialize(format="turtle"), "washer_kb.ttl", "text/turtle")
    if st.button("OKF 번들 만들기"):
        from washer_onto.okf import export_bundle
        with tempfile.TemporaryDirectory() as tmp:
            out = export_bundle(kb, Path(tmp) / "okf_bundle")
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for f in sorted(out.rglob("*.md")):
                    z.write(f, f.relative_to(out.parent))
        st.download_button("OKF 번들(zip) 내려받기", buf.getvalue(), "okf_bundle.zip", "application/zip")
