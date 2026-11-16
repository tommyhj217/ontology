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
    assert len(concepts) == 2 + 7
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
