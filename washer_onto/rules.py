"""규칙 에이전트: 선정 규칙을 찾아 제안하고, 개발자에게 물어 확정하고, 확정된 규칙을 적용한다.

  1) 제안   mine_rules()     기존 결정들에서 반복되는 기준을 규칙 후보로 만든다 (LLM 불필요)
            extract_rules()  설계 기준서·회의록에서 규칙 후보를 뽑는다 (Azure OpenAI)
  2) 질문   규칙 후보마다 애매한 점을 질문으로 만들어 대기열에 넣는다
  3) 갱신   answer()         개발자 답변으로 규칙을 확정/수정/폐기한다
                             (선택지 답변은 LLM 불필요, 자유 서술 답변은 Azure OpenAI가 해석)
  4) 사용   check()          확정된 규칙으로 결정을 검증한다 (audit 에서 호출)
            criteria_for()   새 결정 등록 시 해당 분류의 규칙을 기준으로 채워준다

규칙의 최종 권한은 사람에게 있다: 확정·폐기된 규칙은 에이전트가 다시 덮어쓰지 않는다.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from . import llm
from .models import (CONFIRMED, PROPOSED, REJECTED, Criterion, DecisionRecord, Operator, Question,
                     Revision, Rule, RuleStatus, norm_name)
from .store import KB, slug

AGENT = "agent"
OPT_MANDATORY = "예, 반드시 지켜야 하는 필수 규칙입니다"
OPT_RECOMMENDED = "예, 권장 규칙입니다"
OPT_NOT_RULE = "아니오, 일반 규칙이 아닙니다"
_OP_WORD = {">=": "이상", "<=": "이하", ">": "초과", "<": "미만", "==": ""}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _statement(r: Rule) -> str:
    if r.operator and r.threshold is not None:
        return (f"{r.category} 선정 시 '{r.criterion}'은(는) {r.threshold:g}{r.unit} "
                f"{_OP_WORD[r.operator]}".rstrip() + "이어야 한다.")
    return f"{r.category} 선정 시 '{r.criterion}'을(를) 평가해야 한다."


def _spec(c) -> str:
    return f"{c.operator} {c.threshold:g}{c.unit}" if c.operator and c.threshold is not None else "정성 평가"


def _ask(kb: KB, rule_id: str, kind: str, text: str, options: list[str]) -> None:
    """규칙에 열린 질문이 하나만 있도록 교체한다."""
    for q in kb.questions(status="open", rule_id=rule_id):
        kb.delete_question(q.id)
    kb.upsert_question(Question(rule_id=rule_id, kind=kind, text=text, options=options))


# ---------------------------------------------------------------- 1) 제안

def mine_rules(kb: KB) -> dict:
    """기존 결정에서 (분류, 기준)별로 규칙 후보를 만들고 확인 질문을 생성한다."""
    groups: dict[tuple[str, str], list[tuple[DecisionRecord, Criterion]]] = defaultdict(list)
    for rec in kb.all():
        if rec.category:
            for c in rec.criteria:
                groups[(rec.category, norm_name(c.name))].append((rec, c))

    existing = {(r.category, n): r for r in kb.rules() for n in r.names()}
    proposed = skipped = 0
    for (category, key), uses in sorted(groups.items()):
        old = existing.get((category, key))
        if old and old.status != PROPOSED:      # 사람이 확정/폐기한 규칙은 건드리지 않는다
            skipped += 1
            continue
        specs = Counter((c.operator, c.threshold, c.unit) for _, c in uses)
        (op, th, unit), _ = specs.most_common(1)[0]
        rule = Rule(id=old.id if old else "", category=category, criterion=uses[0][1].name,
                    operator=op, threshold=th, unit=unit,
                    mandatory=sum(c.mandatory for _, c in uses) * 2 >= len(uses),
                    weight=sorted(c.weight for _, c in uses)[len(uses) // 2],
                    sources=sorted({rec.id for rec, _ in uses}),
                    history=old.history if old else [])
        rule.statement = _statement(rule)
        changed = old is None or (old.spec(), old.sources) != (rule.spec(), rule.sources)
        if changed:
            rule.history.append(Revision(at=_now(), by=AGENT,
                                         note=f"결정 {len(uses)}건에서 규칙 후보 도출: {rule.spec()}"))
        rid = kb.upsert_rule(rule)
        proposed += changed

        if len(specs) > 1:
            lines = "; ".join(f"{_spec(c)} ← {rec.title}" for rec, c in uses)
            options = sorted({_spec(c) for _, c in uses}) + [OPT_NOT_RULE]
            if changed or not kb.questions(status="open", rule_id=rid):
                _ask(kb, rid, "conflict",
                     f"{category}의 '{rule.criterion}' 기준이 결정마다 다릅니다 ({lines}). 표준으로 삼을 기준은 무엇입니까?",
                     options)
        elif changed or not kb.questions(status="open", rule_id=rid):
            req = "필수" if rule.mandatory else "선택"
            _ask(kb, rid, "scope",
                 f"'{uses[0][0].title}' 등 {len(uses)}건에서 {category}의 '{rule.criterion}' 기준({rule.spec()}, {req})을 "
                 f"사용했습니다. 앞으로 모든 {category} 선정에 적용하는 규칙입니까?",
                 [OPT_MANDATORY, OPT_RECOMMENDED, OPT_NOT_RULE])
    return {"proposed": proposed, "kept": skipped, "open_questions": len(kb.questions(status="open"))}


class RuleDraft(BaseModel):
    category: str = Field(description="적용 대상 분류 (예: 모터, 댐퍼, AI 모델)")
    criterion: str = Field(description="기준 이름")
    operator: Optional[Operator] = None
    threshold: Optional[float] = None
    unit: str = ""
    mandatory: bool = False
    statement: str = Field(description="규칙을 한 문장으로")
    rationale: str = Field("", description="문서에 적힌 규칙의 이유. 없으면 빈 문자열")
    quote: str = Field("", description="규칙의 근거가 되는 원문 문장을 그대로 복사")
    questions: list[str] = Field(default_factory=list, description="규칙을 확정하려면 개발자에게 물어야 할 질문")


class RuleDrafts(BaseModel):
    rules: list[RuleDraft] = Field(default_factory=list)


_EXTRACT_SYSTEM = """너는 세탁기 개발 조직의 문서에서 '모델 선정 규칙'을 찾아내는 에이전트다.
규칙이란 특정 분류(모터, 댐퍼, 베어링, AI 모델 등)의 후보를 고를 때 반복해서 적용해야 하는 기준이다.

규칙:
- 문서에 적힌 내용만 추출한다. 수치를 지어내지 않는다.
- 정량 규칙은 operator(>=, <=, >, <, ==), threshold, unit을 채운다.
- "필수", "반드시", "규격", "법규" 표현이 있으면 mandatory=true.
- quote 에는 근거 원문을 한 글자도 바꾸지 말고 복사한다.
- questions 에는 이 규칙을 확정하기 전에 담당 개발자에게 확인해야 할 점을 0~2개의 구체적인 질문으로 쓴다.
  예: 적용 범위가 불분명함, 수치의 측정 조건이 없음, 필수 여부가 불명확함, 이유가 적혀 있지 않음.
  문서만으로 명확하면 빈 배열로 둔다.
- 일회성 결정 사항(특정 후보를 골랐다는 사실)은 규칙이 아니다."""


def extract_rules(kb: KB, text: str, source: str = "") -> dict:
    """문서에서 규칙 후보를 추출해 '제안' 상태로 넣고 질문을 만든다 (Azure OpenAI)."""
    from .extract import _chunks, _norm
    haystack = _norm(text)
    existing = {(r.category, n): r for r in kb.rules() for n in r.names()}
    proposed = skipped = 0
    for chunk in _chunks(text):
        drafts = llm.chat_structured(
            _EXTRACT_SYSTEM, f"문서명: {source or '(미상)'}\n\n--- 문서 시작 ---\n{chunk}\n--- 문서 끝 ---", RuleDrafts)
        for d in drafts.rules:
            if not d.category.strip() or not d.criterion.strip():
                continue
            old = existing.get((d.category, norm_name(d.criterion)))
            if old and old.status != PROPOSED:
                skipped += 1
                continue
            rule = Rule(id=old.id if old else "", category=d.category, criterion=d.criterion,
                        operator=d.operator if d.threshold is not None else None,
                        threshold=d.threshold if d.operator else None, unit=d.unit, mandatory=d.mandatory,
                        statement=d.statement, rationale=d.rationale,
                        sources=sorted({*(old.sources if old else []), source or "문서"}),
                        history=old.history if old else [])
            rule.statement = rule.statement or _statement(rule)
            rule.history.append(Revision(at=_now(), by=AGENT, note=f"문서 '{source or '문서'}'에서 규칙 후보 추출: {rule.spec()}"))
            rid = kb.upsert_rule(rule)
            existing[(rule.category, norm_name(rule.criterion))] = kb.get_rule(rid)
            proposed += 1

            for q in kb.questions(status="open", rule_id=rid):
                kb.delete_question(q.id)
            grounded = bool(d.quote.strip()) and _norm(d.quote) in haystack
            if not grounded:
                kb.upsert_question(Question(
                    rule_id=rid, kind="scope",
                    text=f"문서 '{source}'에서 이 규칙의 근거 문장을 확인하지 못했습니다. \"{rule.statement}\" — 실제로 적용하는 규칙입니까?",
                    options=[OPT_MANDATORY, OPT_RECOMMENDED, OPT_NOT_RULE]))
            for text_q in d.questions[:2]:
                kb.upsert_question(Question(rule_id=rid, kind="open", text=text_q))
            if grounded and not d.questions:
                kb.upsert_question(Question(
                    rule_id=rid, kind="scope",
                    text=f"문서 '{source}'에 다음 규칙이 있습니다. \"{rule.statement}\" — 현재도 유효한 규칙입니까?",
                    options=[OPT_MANDATORY, OPT_RECOMMENDED, OPT_NOT_RULE]))
    return {"proposed": proposed, "kept": skipped, "open_questions": len(kb.questions(status="open"))}


# ---------------------------------------------------------------- 3) 갱신

class RuleUpdate(BaseModel):
    operator: Optional[Operator] = None
    threshold: Optional[float] = None
    unit: str = ""
    mandatory: bool = False
    statement: str = ""
    rationale: str = ""
    aliases: list[str] = Field(default_factory=list)
    status: RuleStatus = "제안"
    followup_question: Optional[str] = Field(None, description="아직 확정하기에 정보가 부족할 때 추가로 물을 질문")


_INTERPRET_SYSTEM = """너는 세탁기 개발 조직의 선정 규칙을 관리하는 에이전트다.
현재 규칙, 네가 개발자에게 한 질문, 개발자의 답변이 주어진다. 답변을 반영해 갱신된 규칙 전체를 출력하라.

- 답변에서 언급하지 않은 필드는 현재 값을 그대로 유지한다.
- 개발자가 수치·필수 여부·적용 조건을 바꾸면 그대로 반영하고 statement 도 그에 맞게 고쳐 쓴다.
- 개발자가 이유를 말하면 rationale 에 정리한다. 기준의 다른 이름을 알려주면 aliases 에 추가한다.
- 답변이 규칙을 인정하는 내용이고 더 물을 것이 없으면 status="확정".
- 규칙이 아니다/더 이상 쓰지 않는다는 답변이면 status="폐기".
- 답변이 모호하거나 핵심 정보가 빠졌으면 status="제안"으로 두고 followup_question 에 구체적인 추가 질문 하나를 쓴다.
- 개발자가 말하지 않은 수치를 지어내지 않는다."""


def _diff(before: Rule, after: Rule) -> str:
    labels = {"operator": "연산자", "threshold": "임계값", "unit": "단위", "mandatory": "필수",
              "statement": "서술", "rationale": "이유", "aliases": "다른 이름", "status": "상태"}
    parts = [f"{labels[f]}: {getattr(before, f) or '–'} → {getattr(after, f) or '–'}"
             for f in labels if getattr(before, f) != getattr(after, f)]
    return "; ".join(parts) or "변경 없음"


def answer(kb: KB, question_id: str, text: str, by: str) -> tuple[Rule, str]:
    """개발자 답변을 규칙에 반영한다. (갱신된 규칙, 변경 요약)을 반환.

    선택지 중 하나면 정해진 대로 반영하고, 자유 서술이면 Azure OpenAI가 해석한다.
    """
    q = kb.get_question(question_id)
    if q is None or q.status != "open":
        raise KeyError(f"열린 질문 '{question_id}' 이(가) 없습니다.")
    text, by = text.strip(), by.strip() or "미상"
    if not text:
        raise ValueError("답변이 비어 있습니다.")
    rule = kb.get_rule(q.rule_id)
    before = rule.model_copy(deep=True)
    followup = None

    if text == OPT_NOT_RULE:
        rule.status = REJECTED
    elif text == OPT_MANDATORY:
        rule.status, rule.mandatory = CONFIRMED, True
    elif text == OPT_RECOMMENDED:
        rule.status, rule.mandatory = CONFIRMED, False
    elif q.kind == "conflict" and text in q.options:
        chosen = next((c for rec in kb.all() if rec.category == rule.category for c in rec.criteria
                       if norm_name(c.name) in rule.names() and _spec(c) == text), None)
        if chosen is None:
            raise ValueError(f"선택지 '{text}' 에 해당하는 기준을 찾지 못했습니다.")
        rule.operator, rule.threshold, rule.unit = chosen.operator, chosen.threshold, chosen.unit
        rule.status = CONFIRMED
        rule.statement = _statement(rule)
    else:
        upd = llm.chat_structured(
            _INTERPRET_SYSTEM,
            f"현재 규칙:\n{rule.model_dump_json(exclude={'history', 'id'}, indent=1)}\n\n질문: {q.text}\n\n"
            f"개발자({by})의 답변: {text}", RuleUpdate)
        if upd.operator and upd.threshold is not None:
            rule.operator, rule.threshold, rule.unit = upd.operator, upd.threshold, upd.unit
        elif not upd.operator and upd.threshold is None:
            rule.operator, rule.threshold = None, None
        rule.mandatory, rule.status = upd.mandatory, upd.status
        rule.statement = upd.statement or _statement(rule)
        rule.rationale = upd.rationale or rule.rationale
        rule.aliases = sorted({*rule.aliases, *upd.aliases} - {rule.criterion})
        followup = upd.followup_question if upd.status == PROPOSED else None

    if rule.status == CONFIRMED:
        rule.confirmed_by, rule.confirmed_on = by, date.today().isoformat()
    summary = _diff(before, rule)
    rule.history.append(Revision(at=_now(), by=f"human:{by}", note=f"질문 「{q.text}」 답변 「{text}」 → {summary}"))
    kb.upsert_rule(rule)

    q.status, q.answer, q.answered_by, q.answered_at = "answered", text, by, _now()
    kb.upsert_question(q)
    if rule.status != PROPOSED:      # 결론이 났으면 같은 규칙의 남은 질문은 닫는다
        for other in kb.questions(status="open", rule_id=rule.id):
            kb.delete_question(other.id)
    elif followup:
        kb.upsert_question(Question(rule_id=rule.id, kind="open", text=followup))
    return rule, summary


# ---------------------------------------------------------------- 4) 사용

def criteria_for(kb: KB, category: str) -> list[Criterion]:
    """해당 분류의 확정 규칙을 새 결정의 기준 목록으로 변환."""
    return [Criterion(name=r.criterion, description=r.statement, operator=r.operator, threshold=r.threshold,
                      unit=r.unit, weight=r.weight, mandatory=r.mandatory)
            for r in kb.rules(status=CONFIRMED, category=category)]


def _looser(rule: Rule, crit: Criterion) -> bool:
    """결정의 기준이 규칙보다 느슨한가."""
    if crit.operator is None or crit.threshold is None:
        return True
    up, down = (">=", ">"), ("<=", "<")
    if rule.operator in up:
        return crit.operator not in up and crit.operator != "==" or crit.threshold < rule.threshold
    if rule.operator in down:
        return crit.operator not in down and crit.operator != "==" or crit.threshold > rule.threshold
    return crit.operator != "==" or abs(crit.threshold - rule.threshold) > 1e-9


def check(kb: KB, rec: DecisionRecord) -> list[tuple[str, str, str]]:
    """확정 규칙으로 결정을 점검한다. (심각도, 코드, 메시지) 목록."""
    out = []
    if not rec.category:
        return out
    for rule in kb.rules(status=CONFIRMED, category=rec.category):
        sev = "오류" if rule.mandatory else "경고"
        kind = "필수 규칙" if rule.mandatory else "권장 규칙"
        who = f"(확정: {rule.confirmed_by}, {rule.confirmed_on})"
        crit = next((c for c in rec.criteria if norm_name(c.name) in rule.names()), None)
        if crit is None:
            out.append((sev, "RULE_MISSING",
                        f"{kind} '{rule.criterion}'({rule.spec()})이 이 결정의 기준에 없습니다 {who}."))
            continue
        if rule.operator is None or rule.threshold is None:
            continue
        if _looser(rule, crit):
            out.append(("경고", "RULE_DEVIATION",
                        f"'{crit.name}' 기준({_spec(crit)})이 {kind}({rule.spec()})보다 느슨합니다 {who}."))
        e = rec.evaluation(rec.chosen, crit.name)
        if e and e.value is not None:
            as_rule = Criterion(name=rule.criterion, operator=rule.operator, threshold=rule.threshold)
            if not as_rule.passes(e.value) and crit.passes(e.value) is not False:
                out.append((sev, "RULE_VIOLATION",
                            f"선정 후보 '{rec.chosen}'의 '{crit.name}' 측정값 {e.value:g}{rule.unit}이 "
                            f"{kind}({rule.spec()})을 만족하지 못합니다 {who}."))
    return out
