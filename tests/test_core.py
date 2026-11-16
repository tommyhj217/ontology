from pathlib import Path

import yaml

from washer_onto.audit import FAIL, PASS, audit
from washer_onto.cli import _load_records
from washer_onto.extract import ground
from washer_onto.models import Candidate, Criterion, DecisionRecord, Evaluation, Evidence
from washer_onto.okf import export_bundle
from washer_onto.store import KB

EXAMPLES = Path(__file__).parent.parent / "examples" / "decisions.yaml"


def make_kb(tmp_path) -> KB:
    kb = KB(tmp_path / "kb.ttl")
    for rec in _load_records(EXAMPLES):
        kb.upsert(rec)
    kb.save()
    return kb


def codes(report):
    return {f.code for f in report.findings}


def test_roundtrip_through_file(tmp_path):
    original = {r.id: r for r in _load_records(EXAMPLES)}
    make_kb(tmp_path)
    reloaded = KB(tmp_path / "kb.ttl")
    assert set(reloaded.list_ids()) == set(original)
    for did, rec in original.items():
        got = reloaded.get(did)
        assert got.chosen == rec.chosen
        assert [c.name for c in got.criteria] == [c.name for c in rec.criteria]
        assert {c.name for c in got.candidates} == {c.name for c in rec.candidates}
        for e in rec.evaluations:
            g = got.evaluation(e.candidate, e.criterion)
            assert (g.value, g.rating) == (e.value, e.rating)
            assert [x.reference for x in g.evidence] == [x.reference for x in e.evidence]


def test_upsert_replaces_and_delete_removes(tmp_path):
    kb = make_kb(tmp_path)
    n = len(kb.g)
    for rec in _load_records(EXAMPLES):
        kb.upsert(rec)
    assert len(kb.g) == n
    kb.delete("댐퍼-선정-2026")
    assert kb.list_ids() == ["DD-모터-선정-2026"]


def test_good_decision_passes(tmp_path):
    rep = audit(make_kb(tmp_path), "DD-모터-선정-2026")
    assert rep.verdict == PASS, rep.findings
    assert rep.eligible == {"BLDC-A380": True, "BLDC-B410": False, "PMSM-C36": False}


def test_flawed_decision_fails(tmp_path):
    rep = audit(make_kb(tmp_path), "댐퍼-선정-2026")
    assert rep.verdict == FAIL
    assert {"THRESHOLD_FAIL", "MISSING_EVAL", "NOT_BEST", "NO_EVIDENCE", "STRUCTURE"} <= codes(rep)
    assert rep.scores["FD-150S"] > rep.scores["FD-120"]


def test_structure_checks(tmp_path):
    kb = KB(tmp_path / "kb.ttl")
    kb.upsert(DecisionRecord(
        id="x", title="잘못된 기록", chosen="없는후보", candidates=[Candidate(name="A")],
        criteria=[Criterion(name="소음", operator="<=", weight=1)],   # 임계값 누락
        evaluations=[Evaluation(candidate="A", criterion="소음", rating=9)]))
    rep = audit(kb, "x")
    msgs = " ".join(f.message for f in rep.findings)
    assert rep.verdict == FAIL
    assert "후보 목록에 있어야" in msgs and "임계값은 함께" in msgs and "1~5" in msgs and "대안 후보" in msgs


def test_grounding_flags_fabricated_quotes(tmp_path):
    rec = DecisionRecord(title="t", chosen="A", candidates=[Candidate(name="A")],
                         criteria=[Criterion(name="c")],
                         evaluations=[Evaluation(candidate="A", criterion="c", rating=3, evidence=[
                             Evidence(quote="F1은 0.97,  크기는 180KB"), Evidence(quote="문서에 없는 문장")])])
    ground([rec], "1D-CNN 소형 모델의 F1은 0.97, 크기는\n180KB였다.")
    assert [ev.grounded for ev in rec.evaluations[0].evidence] == [True, False]
    kb = KB(tmp_path / "kb.ttl")
    kb.upsert(rec)
    assert "UNGROUNDED_QUOTE" in codes(audit(kb, "t"))


def test_okf_bundle_conforms(tmp_path):
    out = export_bundle(make_kb(tmp_path), tmp_path / "bundle")
    assert 'okf_version: "0.2"' in (out / "index.md").read_text(encoding="utf-8")
    assert (out / "log.md").exists()
    concepts = [p for p in out.rglob("*.md") if p.name not in ("index.md", "log.md")]
    assert len(concepts) == 2 + 7      # 결정 2 + 기준 7 (규칙 없음)
    for p in concepts:
        text = p.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        front = yaml.safe_load(text.split("---\n")[1])
        assert front["type"] and front["generated"]["by"]
    good = yaml.safe_load((out / "decisions" / "DD-모터-선정-2026.md").read_text(encoding="utf-8").split("---\n")[1])
    assert {v["by"] for v in good["verified"]} == {"human:박검증", "process:washer-onto-audit"}
    bad = yaml.safe_load((out / "decisions" / "댐퍼-선정-2026.md").read_text(encoding="utf-8").split("---\n")[1])
    assert bad["status"] == "draft" and "verified" not in bad


def test_extract_with_stubbed_llm(monkeypatch):
    from washer_onto import extract as ex
    from washer_onto.models import ExtractionResult
    text = (Path(__file__).parent.parent / "examples" / "meeting_notes.txt").read_text(encoding="utf-8")
    rec = DecisionRecord(title="AI 모델 선정", chosen="1D-CNN", candidates=[Candidate(name="1D-CNN")],
                         criteria=[Criterion(name="F1", operator=">=", threshold=0.95, mandatory=True)],
                         evaluations=[Evaluation(candidate="1D-CNN", criterion="F1", value=0.97, evidence=[
                             Evidence(type="시험성적서", quote="1D-CNN 소형 모델의 F1은 0.97")])])
    monkeypatch.setattr(ex.llm, "chat_structured", lambda *a, **k: ExtractionResult(decisions=[rec]))
    out = ex.extract(text, source="meeting_notes.txt")
    assert out[0].source == "meeting_notes.txt"
    assert out[0].evaluations[0].evidence[0].grounded is True


# ---------- 규칙 에이전트 ----------

def test_rule_loop_mine_ask_answer_apply(tmp_path):
    from washer_onto import rules as R
    kb = make_kb(tmp_path)
    res = R.mine_rules(kb)
    assert res["proposed"] == 8 and res["open_questions"] == 8
    assert all(r.status == "제안" for r in kb.rules())

    rule, _ = R.answer(kb, "모터-탈수-소음-q1", R.OPT_MANDATORY, "박검증")
    assert (rule.status, rule.mandatory, rule.confirmed_by) == ("확정", True, "박검증")
    assert rule.history[-1].by == "human:박검증"
    assert R.answer(kb, "모터-단가-q1", R.OPT_NOT_RULE, "박검증")[0].status == "폐기"

    # 저장 후 다시 읽어도 유지되고, 재실행해도 사람이 정한 규칙은 덮어쓰지 않는다
    kb.save()
    kb = KB(tmp_path / "kb.ttl")
    assert R.mine_rules(kb) == {"proposed": 0, "kept": 2, "open_questions": 6}
    assert kb.get_rule("모터-단가").status == "폐기"
    assert [c.name for c in R.criteria_for(kb, "모터")] == ["탈수 소음"]

    # 확정 규칙 적용: 소음 기준이 없는 모터 결정은 부적합, 느슨한 기준은 경고
    kb.upsert(DecisionRecord(id="m2", title="신규 모터", category="모터", chosen="A", rationale="r",
                             decided_by=["x"], candidates=[Candidate(name="A"), Candidate(name="B")],
                             criteria=[Criterion(name="효율", operator=">=", threshold=88)],
                             evaluations=[Evaluation(candidate="A", criterion="효율", value=90,
                                                     evidence=[Evidence(reference="T")])]))
    rep = audit(kb, "m2")
    assert rep.verdict == FAIL and "RULE_MISSING" in codes(rep)
    kb.upsert(DecisionRecord(id="m3", title="신규 모터2", category="모터", chosen="A", rationale="r",
                             decided_by=["x"], candidates=[Candidate(name="A"), Candidate(name="B")],
                             criteria=[Criterion(name="탈수소음", operator="<=", threshold=52, unit="dB")],
                             evaluations=[Evaluation(candidate="A", criterion="탈수소음", value=50,
                                                     evidence=[Evidence(reference="T")])]))
    assert {"RULE_DEVIATION", "RULE_VIOLATION"} <= codes(audit(kb, "m3"))
    assert audit(kb, "DD-모터-선정-2026").verdict == PASS


def test_conflicting_decisions_ask_which_is_standard(tmp_path):
    from washer_onto import rules as R
    kb = make_kb(tmp_path)
    rec = kb.get("DD-모터-선정-2026")
    rec.id, rec.title = "m-old", "구형 모터 선정"
    next(c for c in rec.criteria if c.name == "탈수 소음").threshold = 50
    kb.upsert(rec)
    R.mine_rules(kb)
    q = kb.questions(status="open", rule_id="모터-탈수-소음")[0]
    assert q.kind == "conflict" and q.options == ["<= 48dB", "<= 50dB", R.OPT_NOT_RULE]
    rule, _ = R.answer(kb, q.id, "<= 50dB", "김모터")
    assert (rule.status, rule.threshold) == ("확정", 50)


def test_free_text_answer_is_interpreted_and_can_follow_up(tmp_path, monkeypatch):
    from washer_onto import rules as R
    kb = make_kb(tmp_path)
    R.mine_rules(kb)
    replies = iter([
        R.RuleUpdate(operator="<=", threshold=48, unit="dB", mandatory=True, status="제안",
                     followup_question="46dB는 어떤 용량부터 적용됩니까?"),
        R.RuleUpdate(operator="<=", threshold=46, unit="dB", mandatory=True, status="확정",
                     statement="24kg 이상 모터는 탈수 소음 46dB 이하", rationale="프리미엄 라인 정숙성 목표"),
    ])
    monkeypatch.setattr(R.llm, "chat_structured", lambda *a, **k: next(replies))
    rule, _ = R.answer(kb, "모터-탈수-소음-q1", "대용량은 46으로 강화됐어요", "김모터")
    assert rule.status == "제안"
    follow = kb.questions(status="open", rule_id=rule.id)
    assert [q.text for q in follow] == ["46dB는 어떤 용량부터 적용됩니까?"]
    rule, summary = R.answer(kb, follow[0].id, "24kg 이상. 프리미엄 라인 정숙성 때문", "김모터")
    assert (rule.status, rule.threshold, rule.rationale) == ("확정", 46, "프리미엄 라인 정숙성 목표")
    assert "임계값: 48.0 → 46.0" in summary and not kb.questions(status="open", rule_id=rule.id)


def test_extract_rules_with_stubbed_llm_and_okf_rules(tmp_path, monkeypatch):
    from washer_onto import rules as R
    kb = make_kb(tmp_path)
    text = "베어링은 내구 수명 1만 시간 이상인 모델만 선정한다."
    drafts = R.RuleDrafts(rules=[
        R.RuleDraft(category="베어링", criterion="내구 수명", operator=">=", threshold=10000, unit="h", mandatory=True,
                    statement="베어링은 내구 수명 1만 시간 이상", quote="내구 수명 1만 시간 이상인 모델만 선정한다",
                    questions=["1만 시간은 어떤 부하 조건 기준입니까?"]),
        R.RuleDraft(category="베어링", criterion="소음", statement="지어낸 규칙", quote="문서에 없는 문장")])
    monkeypatch.setattr(R.llm, "chat_structured", lambda *a, **k: drafts)
    assert R.extract_rules(kb, text, source="설계기준.txt")["proposed"] == 2
    assert [q.kind for q in kb.questions(rule_id="베어링-내구-수명")] == ["open"]
    assert "확인하지 못했습니다" in kb.questions(rule_id="베어링-소음")[0].text

    R.answer(kb, kb.questions(rule_id="베어링-소음")[0].id, R.OPT_RECOMMENDED, "한품질")
    out = export_bundle(kb, tmp_path / "bundle")
    front = lambda name: yaml.safe_load((out / "rules" / name).read_text(encoding="utf-8").split("---\n")[1])
    assert front("베어링-소음.md")["verified"][0]["by"] == "human:한품질"
    assert front("베어링-내구-수명.md")["status"] == "draft"
    assert "rules/index.md" in (out / "index.md").read_text(encoding="utf-8")
