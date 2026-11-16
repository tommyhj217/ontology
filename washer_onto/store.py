"""지식베이스: rdflib 그래프(Turtle 파일)에 의사결정 기록을 저장/조회한다."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Optional

from rdflib import RDF, RDFS, XSD, Graph, Literal, Namespace, URIRef
from rdflib.namespace import DCTERMS, PROV, SKOS

from .models import Candidate, Criterion, DecisionRecord, Evaluation, Evidence

WM = Namespace("http://example.org/washer/onto#")
KBNS = Namespace("http://example.org/washer/kb/")

SCHEMA_DIR = Path(__file__).parent / "schema"
ONTOLOGY_PATH = SCHEMA_DIR / "washer.ttl"
SHAPES_PATH = SCHEMA_DIR / "shapes.ttl"

SPARQL_PREFIXES = """PREFIX wm: <http://example.org/washer/onto#>
PREFIX kb: <http://example.org/washer/kb/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX prov: <http://www.w3.org/ns/prov#>
"""


def slug(text: str) -> str:
    """IRI·파일명에 쓸 수 있는 식별자(한글 유지)."""
    s = re.sub(r"[^\w\-.]+", "-", text.strip(), flags=re.UNICODE).strip("-")
    return s or "unnamed"


def load_ontology() -> Graph:
    return Graph().parse(ONTOLOGY_PATH, format="turtle")


class KB:
    def __init__(self, path: str | Path = "data/kb.ttl"):
        self.path = Path(path)
        self.g = Graph()
        for prefix, ns in (("wm", WM), ("kb", KBNS), ("prov", PROV),
                           ("skos", SKOS), ("dcterms", DCTERMS)):
            self.g.bind(prefix, ns)
        if self.path.exists():
            self.g.parse(self.path, format="turtle")

    # ---------- 저장 ----------

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        self.g.serialize(tmp, format="turtle")
        tmp.replace(self.path)

    @staticmethod
    def decision_iri(decision_id: str) -> URIRef:
        return KBNS[f"decision/{decision_id}"]

    def _owned(self, decision_id: str):
        """해당 결정에 속한(IRI가 결정 IRI 아래에 있는) 모든 트리플."""
        base = str(self.decision_iri(decision_id))
        for s, p, o in self.g:
            if str(s) == base or str(s).startswith(base + "/"):
                yield s, p, o

    def delete(self, decision_id: str) -> None:
        for t in list(self._owned(decision_id)):
            self.g.remove(t)

    def _shared(self, kind: str, cls: URIRef, name: str) -> URIRef:
        node = KBNS[f"{kind}/{slug(name)}"]
        self.g.add((node, RDF.type, cls))
        self.g.add((node, RDFS.label, Literal(name)))
        return node

    def upsert(self, rec: DecisionRecord) -> str:
        """결정 기록을 그래프에 넣는다(같은 ID가 있으면 교체). 결정 ID를 반환."""
        did = slug(rec.id or rec.title)
        rec.id = did
        self.delete(did)
        g, d = self.g, self.decision_iri(did)

        g.add((d, RDF.type, WM.SelectionDecision))
        g.add((d, RDFS.label, Literal(rec.title)))
        if rec.rationale:
            g.add((d, WM.rationale, Literal(rec.rationale)))
        if rec.decided_on:
            try:
                g.add((d, WM.decidedOn, Literal(date.fromisoformat(rec.decided_on))))
            except ValueError:
                pass
        if rec.source:
            g.add((d, WM.sourceDocument, Literal(rec.source)))
        if rec.project:
            g.add((d, WM.forProject, self._shared("project", WM.Project, rec.project)))
        if rec.category:
            g.add((d, WM.category, self._shared("category", WM.Category, rec.category)))
        for name in rec.decided_by:
            g.add((d, WM.decidedBy, self._shared("person", WM.Person, name)))
        for name in rec.reviewed_by:
            g.add((d, WM.reviewedBy, self._shared("person", WM.Person, name)))

        cand_iri = lambda n: URIRef(f"{d}/candidate/{slug(n)}")
        crit_iri = lambda n: URIRef(f"{d}/criterion/{slug(n)}")

        for c in rec.candidates:
            n = cand_iri(c.name)
            g.add((n, RDF.type, WM.Candidate))
            g.add((n, RDFS.label, Literal(c.name)))
            if c.maker:
                g.add((n, WM.maker, Literal(c.maker)))
            if c.description:
                g.add((n, RDFS.comment, Literal(c.description)))
            g.add((d, WM.hasCandidate, n))
        if rec.chosen:
            # 후보 목록에 없으면 타입 없는 노드가 되어 SHACL 검증에서 잡힌다.
            g.add((d, WM.chosenCandidate, cand_iri(rec.chosen)))

        for i, c in enumerate(rec.criteria):
            n = crit_iri(c.name)
            g.add((n, RDF.type, WM.Criterion))
            g.add((n, RDFS.label, Literal(c.name)))
            g.add((n, WM["order"], Literal(i)))
            if c.description:
                g.add((n, RDFS.comment, Literal(c.description)))
            if c.operator:
                g.add((n, WM.operator, Literal(c.operator)))
            if c.threshold is not None:
                g.add((n, WM.threshold, Literal(float(c.threshold))))
            if c.unit:
                g.add((n, WM.unit, Literal(c.unit)))
            g.add((n, WM.weight, Literal(float(c.weight))))
            g.add((n, WM.mandatory, Literal(bool(c.mandatory))))
            if c.higher_is_better is not None:
                g.add((n, WM.higherIsBetter, Literal(bool(c.higher_is_better))))
            g.add((d, WM.hasCriterion, n))

        for i, e in enumerate(rec.evaluations):
            n = URIRef(f"{d}/evaluation/{i}")
            g.add((n, RDF.type, WM.Evaluation))
            g.add((n, WM.ofCandidate, cand_iri(e.candidate)))
            g.add((n, WM.onCriterion, crit_iri(e.criterion)))
            if e.value is not None:
                g.add((n, WM.measuredValue, Literal(float(e.value))))
            if e.rating is not None:
                g.add((n, WM.rating, Literal(float(e.rating))))
            if e.note:
                g.add((n, WM.note, Literal(e.note)))
            for j, ev in enumerate(e.evidence):
                en = URIRef(f"{n}/evidence/{j}")
                g.add((en, RDF.type, WM.Evidence))
                g.add((en, WM.evidenceType, Literal(ev.type or "기타")))
                if ev.reference:
                    g.add((en, WM.reference, Literal(ev.reference)))
                if ev.quote:
                    g.add((en, WM.quote, Literal(ev.quote)))
                if ev.grounded is not None:
                    g.add((en, WM.quoteGrounded, Literal(bool(ev.grounded))))
                g.add((n, WM.hasEvidence, en))
            g.add((d, WM.hasEvaluation, n))
        return did

    # ---------- 조회 ----------

    def list_ids(self) -> list[str]:
        base = str(KBNS) + "decision/"
        return sorted(str(s)[len(base):] for s in self.g.subjects(RDF.type, WM.SelectionDecision))

    def _val(self, s, p, default=None):
        v = self.g.value(s, p)
        return v.toPython() if v is not None else default

    def _label(self, node) -> str:
        v = self.g.value(node, RDFS.label)
        return str(v) if v is not None else str(node).rsplit("/", 1)[-1]

    def get(self, decision_id: str) -> Optional[DecisionRecord]:
        g, d = self.g, self.decision_iri(decision_id)
        if (d, RDF.type, WM.SelectionDecision) not in g:
            return None
        label_of = lambda p: self._label(g.value(d, p)) if g.value(d, p) is not None else ""
        decided_on = self._val(d, WM.decidedOn)

        candidates = sorted(
            (Candidate(name=self._label(n), maker=self._val(n, WM.maker, ""),
                       description=self._val(n, RDFS.comment, ""))
             for n in g.objects(d, WM.hasCandidate) if (n, RDF.type, WM.Candidate) in g),
            key=lambda c: c.name)
        criteria = [
            Criterion(name=self._label(n), description=self._val(n, RDFS.comment, ""),
                      operator=self._val(n, WM.operator), threshold=self._val(n, WM.threshold),
                      unit=self._val(n, WM.unit, ""), weight=self._val(n, WM.weight, 1.0),
                      mandatory=self._val(n, WM.mandatory, False),
                      higher_is_better=self._val(n, WM.higherIsBetter))
            for n in sorted(g.objects(d, WM.hasCriterion),
                            key=lambda n: self._val(n, WM["order"], 0))]
        evaluations = []
        for n in sorted(g.objects(d, WM.hasEvaluation),
                        key=lambda n: int(str(n).rsplit("/", 1)[-1])):
            evidence = [
                Evidence(type=self._val(en, WM.evidenceType, "기타"),
                         reference=self._val(en, WM.reference, ""),
                         quote=self._val(en, WM.quote, ""),
                         grounded=self._val(en, WM.quoteGrounded))
                for en in sorted(g.objects(n, WM.hasEvidence))]
            evaluations.append(Evaluation(
                candidate=self._label(g.value(n, WM.ofCandidate)),
                criterion=self._label(g.value(n, WM.onCriterion)),
                value=self._val(n, WM.measuredValue), rating=self._val(n, WM.rating),
                note=self._val(n, WM.note, ""), evidence=evidence))

        return DecisionRecord(
            id=decision_id, title=self._label(d), project=label_of(WM.forProject),
            category=label_of(WM.category), chosen=label_of(WM.chosenCandidate),
            rationale=self._val(d, WM.rationale, ""),
            decided_by=sorted(self._label(p) for p in g.objects(d, WM.decidedBy)),
            reviewed_by=sorted(self._label(p) for p in g.objects(d, WM.reviewedBy)),
            decided_on=decided_on.isoformat() if decided_on else None,
            source=self._val(d, WM.sourceDocument, ""),
            candidates=candidates, criteria=criteria, evaluations=evaluations)

    def all(self) -> list[DecisionRecord]:
        return [self.get(i) for i in self.list_ids()]

    def decision_graph(self, decision_id: str) -> Graph:
        """결정 1건에 속한 트리플만 담은 부분 그래프(SHACL 검증용)."""
        sub = Graph()
        for t in self._owned(decision_id):
            sub.add(t)
        return sub

    def query(self, sparql: str):
        """SPARQL SELECT/ASK 실행. 표준 prefix를 자동으로 붙인다."""
        return self.g.query(SPARQL_PREFIXES + sparql)


# ---------- 선정 규칙·질문 저장 (KB 메서드로 붙인다) ----------

from .models import Question, Revision, Rule  # noqa: E402


def _remove_under(kb: KB, base: URIRef) -> None:
    b = str(base)
    for t in [t for t in kb.g if str(t[0]) == b or str(t[0]).startswith(b + "/")]:
        kb.g.remove(t)


def _ordered(kb: KB, node: URIRef, pred: URIRef) -> list[str]:
    """'순번|값' 리터럴로 저장한 목록을 순서대로 복원."""
    items = sorted((str(o).split("|", 1) for o in kb.g.objects(node, pred)), key=lambda x: int(x[0]))
    return [v for _, v in items]


def upsert_rule(self: KB, rule: Rule) -> str:
    rule.id = slug(rule.id or f"{rule.category}-{rule.criterion}")
    n, g = KBNS[f"rule/{rule.id}"], self.g
    _remove_under(self, n)
    g.add((n, RDF.type, WM.SelectionRule))
    g.add((n, RDFS.label, Literal(f"{rule.category} · {rule.criterion}")))
    g.add((n, WM.ruleCategory, self._shared("category", WM.Category, rule.category)))
    g.add((n, WM.criterionName, Literal(rule.criterion)))
    g.add((n, WM.ruleStatus, Literal(rule.status)))
    g.add((n, WM.mandatory, Literal(bool(rule.mandatory))))
    g.add((n, WM.weight, Literal(float(rule.weight))))
    for pred, val in ((WM.operator, rule.operator), (WM.unit, rule.unit), (WM.statement, rule.statement),
                      (WM.ruleRationale, rule.rationale), (WM.confirmedBy, rule.confirmed_by)):
        if val:
            g.add((n, pred, Literal(val)))
    if rule.threshold is not None:
        g.add((n, WM.threshold, Literal(float(rule.threshold))))
    if rule.confirmed_on:
        g.add((n, WM.confirmedOn, Literal(date.fromisoformat(rule.confirmed_on))))
    for a in rule.aliases:
        g.add((n, WM.alias, Literal(a)))
    for s in rule.sources:
        g.add((n, WM.ruleSource, Literal(s)))
    for i, rev in enumerate(rule.history):
        r = URIRef(f"{n}/rev/{i}")
        g.add((r, RDF.type, WM.RuleRevision))
        g.add((r, WM.revisedAt, Literal(rev.at)))
        g.add((r, WM.revisedBy, Literal(rev.by)))
        g.add((r, WM.revisionNote, Literal(rev.note)))
        g.add((n, WM.hasRevision, r))
    return rule.id


def get_rule(self: KB, rule_id: str) -> Optional[Rule]:
    n, g = KBNS[f"rule/{rule_id}"], self.g
    if (n, RDF.type, WM.SelectionRule) not in g:
        return None
    confirmed_on = self._val(n, WM.confirmedOn)
    revs = sorted(g.objects(n, WM.hasRevision), key=lambda r: int(str(r).rsplit("/", 1)[-1]))
    return Rule(
        id=rule_id, category=self._label(g.value(n, WM.ruleCategory)), criterion=self._val(n, WM.criterionName),
        aliases=sorted(str(a) for a in g.objects(n, WM.alias)),
        operator=self._val(n, WM.operator), threshold=self._val(n, WM.threshold), unit=self._val(n, WM.unit, ""),
        mandatory=self._val(n, WM.mandatory, False), weight=self._val(n, WM.weight, 1.0),
        statement=self._val(n, WM.statement, ""), rationale=self._val(n, WM.ruleRationale, ""),
        status=self._val(n, WM.ruleStatus, "제안"), confirmed_by=self._val(n, WM.confirmedBy, ""),
        confirmed_on=confirmed_on.isoformat() if confirmed_on else None,
        sources=sorted(str(s) for s in g.objects(n, WM.ruleSource)),
        history=[Revision(at=self._val(r, WM.revisedAt, ""), by=self._val(r, WM.revisedBy, ""),
                          note=self._val(r, WM.revisionNote, "")) for r in revs])


def rules(self: KB, status: Optional[str] = None, category: Optional[str] = None) -> list[Rule]:
    base = str(KBNS) + "rule/"
    out = [self.get_rule(str(s)[len(base):]) for s in self.g.subjects(RDF.type, WM.SelectionRule)]
    return sorted((r for r in out if (status is None or r.status == status)
                   and (category is None or r.category == category)), key=lambda r: r.id)


def delete_rule(self: KB, rule_id: str) -> None:
    _remove_under(self, KBNS[f"rule/{rule_id}"])
    for q in self.questions(rule_id=rule_id):
        _remove_under(self, KBNS[f"question/{q.id}"])


def upsert_question(self: KB, q: Question) -> str:
    if not q.id:
        i = 1
        while (KBNS[f"question/{q.rule_id}-q{i}"], RDF.type, WM.Question) in self.g:
            i += 1
        q.id = f"{q.rule_id}-q{i}"
    n, g = KBNS[f"question/{q.id}"], self.g
    _remove_under(self, n)
    g.add((n, RDF.type, WM.Question))
    g.add((n, WM.aboutRule, KBNS[f"rule/{q.rule_id}"]))
    g.add((n, WM.questionKind, Literal(q.kind)))
    g.add((n, WM.questionText, Literal(q.text)))
    g.add((n, WM.questionStatus, Literal(q.status)))
    for i, o in enumerate(q.options):
        g.add((n, WM.option, Literal(f"{i}|{o}")))
    for pred, val in ((WM.answer, q.answer), (WM.answeredBy, q.answered_by), (WM.answeredAt, q.answered_at)):
        if val:
            g.add((n, pred, Literal(val)))
    return q.id


def get_question(self: KB, qid: str) -> Optional[Question]:
    n, g = KBNS[f"question/{qid}"], self.g
    if (n, RDF.type, WM.Question) not in g:
        return None
    return Question(id=qid, rule_id=str(g.value(n, WM.aboutRule))[len(str(KBNS) + "rule/"):],
                    kind=self._val(n, WM.questionKind, "open"), text=self._val(n, WM.questionText, ""),
                    options=_ordered(self, n, WM.option), status=self._val(n, WM.questionStatus, "open"),
                    answer=self._val(n, WM.answer, ""), answered_by=self._val(n, WM.answeredBy, ""),
                    answered_at=self._val(n, WM.answeredAt, ""))


def questions(self: KB, status: Optional[str] = None, rule_id: Optional[str] = None) -> list[Question]:
    base = str(KBNS) + "question/"
    out = [self.get_question(str(s)[len(base):]) for s in self.g.subjects(RDF.type, WM.Question)]
    return sorted((q for q in out if (status is None or q.status == status)
                   and (rule_id is None or q.rule_id == rule_id)), key=lambda q: q.id)


def delete_question(self: KB, qid: str) -> None:
    _remove_under(self, KBNS[f"question/{qid}"])


for _f in (upsert_rule, get_rule, rules, delete_rule, upsert_question, get_question, questions, delete_question):
    setattr(KB, _f.__name__, _f)
