"""Google Open Knowledge Format(OKF v0.2) 번들로 내보내기.

OKF는 YAML frontmatter가 달린 마크다운 파일 디렉터리다. 사람은 그대로 읽고,
사내 AI 에이전트는 컨텍스트로 읽을 수 있다. 출처(sources)·신뢰(verified)·수명주기(status)를
frontmatter 표준 필드로 기록한다.
스펙: https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md
"""
from __future__ import annotations

import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import __version__
from .audit import FAIL, PASS, AuditReport, audit
from .models import DecisionRecord
from .store import KB, slug

PRODUCER = f"washer-onto/{__version__}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _doc(front: dict, body: str) -> str:
    fm = yaml.safe_dump(front, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{fm}---\n\n{body.strip()}\n"


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _spec(c) -> str:
    return f"{c.operator} {c.threshold:g}{c.unit}" if c.operator and c.threshold is not None else "정성 평가"


def _eval_text(e, unit: str) -> str:
    if e is None:
        return "–"
    if e.value is not None:
        return f"{e.value:g}{unit}"
    if e.rating is not None:
        return f"평점 {e.rating:g}/5"
    return "–"


def _decision_doc(rec: DecisionRecord, rep: AuditReport, now: str) -> str:
    # 근거 자료 → sources (id는 각주 키로 사용)
    sources, src_id = [], {}
    for e in rec.evaluations:
        for ev in e.evidence:
            key = ev.reference or ev.quote
            if key and key not in src_id:
                src_id[key] = f"src{len(sources) + 1}"
                sources.append({"id": src_id[key], "resource": ev.reference or rec.source or "출처 미상",
                                "title": f"{ev.type}" + (f": {ev.reference}" if ev.reference else "")})

    front: dict = {
        "type": "Selection Decision",
        "title": rec.title,
        "description": f"{rec.category or '모델'} 선정: {rec.chosen} (검증 결과 {rep.verdict})",
        "tags": [t for t in (rec.category, rec.project, rep.verdict) if t],
        "generated": {"by": PRODUCER, "at": now},
    }
    verified = [{"by": f"human:{slug(p)}", "at": f"{rec.decided_on}T00:00:00Z" if rec.decided_on else now}
                for p in rec.reviewed_by]
    if rep.verdict == PASS:
        verified.append({"by": "process:washer-onto-audit", "at": now})
    if verified:
        front["verified"] = verified
    front["status"] = "draft" if rep.verdict == FAIL else "stable"
    if sources:
        front["sources"] = sources
    front.update({"chosen": rec.chosen, "audit_verdict": rep.verdict, "decided_on": rec.decided_on,
                  "decided_by": rec.decided_by})

    b = [f"# 결정 요약\n",
         f"- **선정 모델**: {rec.chosen}",
         f"- **프로젝트**: {rec.project or '–'}",
         f"- **분류**: {rec.category or '–'}",
         f"- **결정일 / 결정자**: {rec.decided_on or '–'} / {', '.join(rec.decided_by) or '–'}",
         f"- **검토자**: {', '.join(rec.reviewed_by) or '–'}",
         f"\n# 선정 사유\n\n{rec.rationale or '기록 없음'}",
         "\n# 선정 기준\n",
         "| 기준 | 조건 | 가중치 | 필수 |", "|---|---|---|---|"]
    for c in rec.criteria:
        b.append(f"| [{_cell(c.name)}](/criteria/{slug(c.name)}.md) | {_cell(_spec(c))} | {c.weight:g} | {'예' if c.mandatory else ''} |")

    b += ["\n# 후보 비교\n",
          "| 기준 | " + " | ".join(_cell(c.name) + (" ✔" if c.name == rec.chosen else "") for c in rec.candidates) + " |",
          "|---|" + "---|" * len(rec.candidates)]
    for c in rec.criteria:
        cells = []
        for cand in rec.candidates:
            e = rec.evaluation(cand.name, c.name)
            txt = _eval_text(e, c.unit)
            if e and e.value is not None and c.passes(e.value) is False:
                txt += " ✗"
            for ev in (e.evidence if e else []):
                key = ev.reference or ev.quote
                if key in src_id:
                    txt += f"[^{src_id[key]}]"
            cells.append(_cell(txt))
        b.append(f"| {_cell(c.name)} | " + " | ".join(cells) + " |")
    if rep.scores:
        b.append("| **가중 점수** | " + " | ".join(
            f"**{rep.scores[c.name]:g}**" + ("" if rep.eligible[c.name] else " (필수 미달)")
            for c in rec.candidates) + " |")

    b.append(f"\n# 검증 결과\n\n**{rep.verdict}**\n")
    b += [f"- {f.severity}: {f.message}" for f in rep.findings] or ["- 지적 사항 없음"]

    if sources:
        b.append("")
        b += [f"[^{s['id']}]: {s['title']}" for s in sources]
    return _doc(front, "\n".join(b))


def export_bundle(kb: KB, out_dir: str | Path) -> Path:
    """지식베이스 전체를 OKF 번들 디렉터리로 내보낸다(기존 디렉터리는 교체)."""
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    (out / "decisions").mkdir(parents=True)
    (out / "criteria").mkdir()
    now = _now()

    records = kb.all()
    reports = {r.id: audit(kb, r.id) for r in records}
    dec_index, crit_use = [], defaultdict(list)

    for rec in records:
        rep = reports[rec.id]
        (out / "decisions" / f"{rec.id}.md").write_text(_decision_doc(rec, rep, now), encoding="utf-8")
        dec_index.append(f"* [{rec.title}]({rec.id}.md) - 선정: {rec.chosen} · 검증 {rep.verdict}")
        for c in rec.criteria:
            crit_use[c.name].append((rec, c))

    crit_index = []
    for name, uses in sorted(crit_use.items()):
        body = ["# 사용된 결정\n", "| 결정 | 조건 | 가중치 | 필수 | 선정 후보 값 |", "|---|---|---|---|---|"]
        for rec, c in uses:
            body.append(f"| [{_cell(rec.title)}](/decisions/{rec.id}.md) | {_cell(_spec(c))} | {c.weight:g} | "
                        f"{'예' if c.mandatory else ''} | {_cell(_eval_text(rec.evaluation(rec.chosen, c.name), c.unit))} |")
        desc = next((c.description for _, c in uses if c.description), "")
        front = {"type": "Selection Criterion", "title": name,
                 "description": desc or f"{len(uses)}건의 선정 결정에 사용된 기준",
                 "generated": {"by": PRODUCER, "at": now}}
        (out / "criteria" / f"{slug(name)}.md").write_text(_doc(front, "\n".join(body)), encoding="utf-8")
        crit_index.append(f"* [{name}]({slug(name)}.md) - {front['description']}")

    rule_index = []
    all_rules = kb.rules()
    if all_rules:
        (out / "rules").mkdir()
    for r in all_rules:
        front = {"type": "Selection Rule", "title": f"{r.category} · {r.criterion}",
                 "description": r.statement, "tags": [r.category, r.status, "필수" if r.mandatory else "권장"],
                 "generated": {"by": PRODUCER, "at": now}}
        if r.status == "확정" and r.confirmed_by:
            front["verified"] = [{"by": f"human:{slug(r.confirmed_by)}",
                                  "at": f"{r.confirmed_on}T00:00:00Z" if r.confirmed_on else now}]
        front["status"] = {"확정": "stable", "제안": "draft", "폐기": "deprecated"}[r.status]
        if r.sources:
            front["sources"] = [{"id": f"src{i + 1}", "title": src,
                                 "resource": f"/decisions/{src}.md" if (out / "decisions" / f"{src}.md").exists() else src}
                                for i, src in enumerate(r.sources)]
        body = ["# 규칙\n", r.statement, "",
                f"- **조건**: {r.spec()}", f"- **구분**: {'필수' if r.mandatory else '권장'}",
                f"- **상태**: {r.status}" + (f" ({r.confirmed_by}, {r.confirmed_on})" if r.status == "확정" else ""),
                f"- **기준 문서**: [{r.criterion}](/criteria/{slug(r.criterion)}.md)" if r.criterion in crit_use else "",
                f"\n# 이유\n\n{r.rationale or '기록 없음'}", "\n# 변경 이력\n"]
        body += [f"- {rev.at} · {rev.by} · {rev.note}" for rev in r.history]
        (out / "rules" / f"{r.id}.md").write_text(_doc(front, "\n".join(b for b in body if b != "")), encoding="utf-8")
        rule_index.append(f"* [{front['title']}]({r.id}.md) - [{r.status}] {r.statement}")
    if all_rules:
        (out / "rules" / "index.md").write_text("# 선정 규칙\n\n" + "\n".join(rule_index) + "\n", encoding="utf-8")

    (out / "decisions" / "index.md").write_text("# 선정 의사결정\n\n" + "\n".join(dec_index) + "\n", encoding="utf-8")
    (out / "criteria" / "index.md").write_text("# 선정 기준\n\n" + "\n".join(crit_index) + "\n", encoding="utf-8")
    (out / "index.md").write_text(
        '---\nokf_version: "0.2"\n---\n\n# 세탁기 개발 모델 선정 지식\n\n'
        f"* [decisions](decisions/index.md) - 모델 선정 의사결정 {len(records)}건과 검증 결과\n"
        f"* [criteria](criteria/index.md) - 결정에 사용된 선정 기준 {len(crit_use)}개\n"
        + (f"* [rules](rules/index.md) - 개발자가 확인한 선정 규칙 {len(all_rules)}개 (제안·폐기 포함)\n" if all_rules else ""),
        encoding="utf-8")
    (out / "log.md").write_text(
        f"# Directory Update Log\n\n## {now[:10]}\n"
        f"* **Update**: {PRODUCER} 가 지식베이스에서 번들을 재생성함 (결정 {len(records)}건).\n", encoding="utf-8")
    return out
