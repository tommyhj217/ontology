"""선정 결정 검증: "기준대로, 근거를 갖고, 가장 적합한 후보를 골랐는가".

1) 구조 검증  - SHACL(pyshacl)로 기록 누락/불일치 확인
2) 정량 검증  - 선정 후보의 측정값을 기준 임계값과 대조
3) 대안 비교  - 가중 점수로 다른 후보가 더 우수하지 않았는지 확인
4) 근거 검증  - 평가에 근거 자료가 있는지, LLM 인용이 원문에 실재하는지
5) 규칙 검증  - 개발자가 확정한 선정 규칙(rules.py)을 지켰는지

LLM을 쓰지 않는 결정적(deterministic) 검사이므로 같은 입력에 항상 같은 판정이 나온다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from pyshacl import validate
from rdflib import Graph
from rdflib.namespace import SH

from .models import DecisionRecord
from .store import KB, SHAPES_PATH, load_ontology

ERROR, WARNING, INFO = "오류", "경고", "참고"
PASS, NEEDS_WORK, FAIL = "적합", "보완필요", "부적합"

# 다른 후보의 점수가 이 값(100점 만점) 넘게 높을 때만 "더 우수한 대안"으로 본다.
SCORE_MARGIN = 3.0


@dataclass
class Finding:
    severity: str
    code: str
    message: str


@dataclass
class AuditReport:
    decision_id: str
    title: str
    chosen: str
    verdict: str = PASS
    findings: list[Finding] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)       # 후보별 가중 점수(0~100)
    eligible: dict[str, bool] = field(default_factory=dict)      # 후보별 필수 기준 통과 여부

    def add(self, severity: str, code: str, message: str) -> None:
        self.findings.append(Finding(severity, code, message))

    def finalize(self) -> "AuditReport":
        order = {ERROR: 0, WARNING: 1, INFO: 2}
        self.findings.sort(key=lambda f: order[f.severity])
        sev = {f.severity for f in self.findings}
        self.verdict = FAIL if ERROR in sev else NEEDS_WORK if WARNING in sev else PASS
        return self


@lru_cache(maxsize=1)
def _schema() -> tuple[Graph, Graph]:
    return load_ontology(), Graph().parse(SHAPES_PATH, format="turtle")


def _shacl_findings(kb: KB, decision_id: str, report: AuditReport) -> None:
    ont, shapes = _schema()
    _, results, _ = validate(kb.decision_graph(decision_id), shacl_graph=shapes,
                             ont_graph=ont, inference="none")
    sev_map = {SH.Violation: ERROR, SH.Warning: WARNING, SH.Info: INFO}
    seen = set()
    for r in results.subjects(SH.resultSeverity, None):
        sev = sev_map.get(results.value(r, SH.resultSeverity), WARNING)
        msg = str(results.value(r, SH.resultMessage) or "SHACL 제약 위반")
        focus = results.value(r, SH.focusNode)
        label = kb._label(focus) if focus is not None else ""
        if label and label != report.title and not label.isdigit():
            msg = f"[{label}] {msg}"
        if (sev, msg) not in seen:
            seen.add((sev, msg))
            report.add(sev, "STRUCTURE", msg)


def _fmt(v: float) -> str:
    return f"{v:g}"


def compute_scores(rec: DecisionRecord) -> tuple[dict[str, float], dict[str, bool]]:
    """후보별 가중 점수(0~100)와 필수 기준 통과 여부.

    정량 기준은 후보 간 min-max 정규화(방향 반영), 정성 기준은 평점/5.
    평가가 없는 칸은 0점 처리한다.
    """
    names = [c.name for c in rec.candidates]
    total_w = sum(c.weight for c in rec.criteria) or 1.0
    scores = {n: 0.0 for n in names}
    eligible = {n: True for n in names}

    for crit in rec.criteria:
        direction = crit.direction()
        evs = {n: rec.evaluation(n, crit.name) for n in names}
        values = {n: e.value for n, e in evs.items() if e and e.value is not None}
        lo, hi = (min(values.values()), max(values.values())) if values else (0.0, 0.0)

        for n in names:
            e = evs[n]
            part = 0.0
            if e and e.value is not None and direction is not None:
                part = 1.0 if hi == lo else (e.value - lo) / (hi - lo)
                if direction is False:
                    part = 1.0 - part if hi != lo else 1.0
            elif e and e.value is not None and crit.operator == "==":
                part = 1.0 if crit.passes(e.value) else 0.0
            elif e and e.rating is not None:
                part = max(0.0, min(1.0, e.rating / 5.0))
            scores[n] += crit.weight * part

            if crit.mandatory and crit.operator:
                if not (e and e.value is not None and crit.passes(e.value)):
                    eligible[n] = False

    return {n: round(100.0 * s / total_w, 1) for n, s in scores.items()}, eligible


def audit(kb: KB, decision_id: str) -> AuditReport:
    rec = kb.get(decision_id)
    if rec is None:
        raise KeyError(f"결정 '{decision_id}' 을(를) 찾을 수 없습니다.")
    report = AuditReport(decision_id, rec.title, rec.chosen)

    _shacl_findings(kb, decision_id, report)

    chosen_known = any(c.name == rec.chosen for c in rec.candidates)
    for crit in rec.criteria:
        req = "필수 " if crit.mandatory else ""
        spec = f"{crit.operator} {_fmt(crit.threshold)}{crit.unit}" if crit.operator and crit.threshold is not None else ""
        e = rec.evaluation(rec.chosen, crit.name) if chosen_known else None

        if chosen_known and (e is None or (e.value is None and e.rating is None)):
            report.add(ERROR if crit.mandatory else WARNING, "MISSING_EVAL",
                       f"선정 후보 '{rec.chosen}'의 {req}기준 '{crit.name}' 평가값이 없습니다.")
            continue
        if e is None:
            continue
        if spec and e.value is None:
            report.add(ERROR if crit.mandatory else WARNING, "NO_MEASUREMENT",
                       f"{req}기준 '{crit.name}'({spec})은 정량 기준인데 선정 후보의 측정값이 없습니다.")
        elif spec and not crit.passes(e.value):
            report.add(ERROR if crit.mandatory else WARNING, "THRESHOLD_FAIL",
                       f"선정 후보 '{rec.chosen}'가 {req}기준 '{crit.name}'을 만족하지 못합니다 "
                       f"(측정 {_fmt(e.value)}{crit.unit}, 기준 {spec}).")
        if not e.evidence:
            report.add(WARNING, "NO_EVIDENCE",
                       f"선정 후보의 '{crit.name}' 평가에 근거 자료가 없습니다.")

    ungrounded = sum(1 for e in rec.evaluations for ev in e.evidence if ev.grounded is False)
    if ungrounded:
        report.add(WARNING, "UNGROUNDED_QUOTE",
                   f"자동 추출된 인용문 {ungrounded}건이 원문에서 확인되지 않았습니다. 원문 대조가 필요합니다.")

    if rec.candidates and rec.criteria:
        report.scores, report.eligible = compute_scores(rec)
        if chosen_known:
            mine = report.scores[rec.chosen]
            better = [(n, s) for n, s in report.scores.items()
                      if n != rec.chosen and report.eligible[n] and s > mine + SCORE_MARGIN]
            if better:
                n, s = max(better, key=lambda x: x[1])
                report.add(WARNING, "NOT_BEST",
                           f"필수 기준을 모두 통과한 '{n}'의 가중 점수({s})가 선정 후보 '{rec.chosen}'({mine})보다 높습니다. "
                           f"기록되지 않은 선정 사유가 있는지 확인하세요.")

    from .rules import check as check_rules
    for sev, code, msg in check_rules(kb, rec):
        report.add(sev, code, msg)

    return report.finalize()


def audit_all(kb: KB) -> list[AuditReport]:
    return [audit(kb, i) for i in kb.list_ids()]


def render(report: AuditReport) -> str:
    """콘솔 출력용 텍스트."""
    lines = [f"[{report.verdict}] {report.title}  (선정: {report.chosen}, id: {report.decision_id})"]
    if report.scores:
        ranked = sorted(report.scores.items(), key=lambda x: -x[1])
        lines.append("  가중 점수: " + ", ".join(
            f"{n} {s}{'' if report.eligible[n] else '(필수기준 미달)'}" for n, s in ranked))
    for f in report.findings:
        lines.append(f"  - {f.severity} {f.code}: {f.message}")
    if not report.findings:
        lines.append("  - 지적 사항 없음")
    return "\n".join(lines)
