"""명령행 인터페이스:  python -m washer_onto <명령>"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from .models import DecisionRecord
from .store import KB


def _load_records(path: Path) -> list[DecisionRecord]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "decisions" in data:
        data = data["decisions"]
    if isinstance(data, dict):
        data = [data]
    return [DecisionRecord.model_validate(d) for d in data]


def _dump_records(records: list[DecisionRecord]) -> str:
    return yaml.safe_dump({"decisions": [r.model_dump(exclude_none=True) for r in records]},
                          allow_unicode=True, sort_keys=False)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="washer_onto", description="세탁기 개발 모델 선정 온톨로지")
    ap.add_argument("--kb", default="data/kb.ttl", help="지식베이스 파일 (기본: data/kb.ttl)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="YAML 결정 기록을 지식베이스에 등록")
    p.add_argument("files", nargs="+", type=Path)

    p = sub.add_parser("extract", help="문서에서 결정을 추출해 검토용 YAML로 저장 (Azure OpenAI)")
    p.add_argument("document", type=Path, help="txt/md/pdf/docx")
    p.add_argument("-o", "--out", type=Path, help="출력 YAML (기본: <문서명>.decisions.yaml)")
    p.add_argument("--add", action="store_true", help="검토 없이 바로 지식베이스에 등록")

    p = sub.add_parser("list", help="등록된 결정 목록")

    p = sub.add_parser("audit", help="결정이 기준대로 내려졌는지 검증")
    p.add_argument("id", nargs="?", help="결정 ID (생략 시 전체)")

    p = sub.add_parser("ask", help="자연어 질문 (Azure OpenAI)")
    p.add_argument("question")

    p = sub.add_parser("sparql", help="SPARQL 직접 실행")
    p.add_argument("query")

    p = sub.add_parser("export-okf", help="Google Open Knowledge Format 번들로 내보내기")
    p.add_argument("-o", "--out", default="okf_bundle")

    p = sub.add_parser("rules", help="선정 규칙: 에이전트가 제안하고 개발자가 확정")
    rs = p.add_subparsers(dest="rcmd", required=True)
    rs.add_parser("mine", help="기존 결정에서 규칙 후보와 질문 만들기")
    x = rs.add_parser("extract", help="문서에서 규칙 후보와 질문 만들기 (Azure OpenAI)")
    x.add_argument("document", type=Path)
    x = rs.add_parser("list", help="규칙 목록")
    x.add_argument("--status", choices=["제안", "확정", "폐기"])
    rs.add_parser("questions", help="답변을 기다리는 질문")
    x = rs.add_parser("answer", help="질문에 답변 (선택지 번호 또는 자유 서술)")
    x.add_argument("question_id")
    x.add_argument("answer")
    x.add_argument("--by", required=True, help="답변자 이름")
    x = rs.add_parser("history", help="규칙 변경 이력")
    x.add_argument("rule_id")

    p = sub.add_parser("chat", help="에이전트와 대화 (도구·MCP·압축·모델 자동 선택)")
    p.add_argument("--user", required=True, help="사용자 이름 (세션·기억의 소유자)")
    p.add_argument("--session", help="이어갈 세션 ID")
    p.add_argument("--config", help="agent_config.json 경로")

    p = sub.add_parser("sessions", help="내 대화 세션 목록")
    p.add_argument("--user", required=True)

    p = sub.add_parser("delete", help="결정 삭제")
    p.add_argument("id")

    args = ap.parse_args(argv)
    if args.cmd in ("chat", "sessions"):
        from .agent import repl
        if args.cmd == "chat":
            repl.run(args.user, args.session, args.config)
        else:
            repl.list_sessions(args.user)
        return 0
    kb = KB(args.kb)

    if args.cmd == "add":
        for f in args.files:
            for rec in _load_records(f):
                print("등록:", kb.upsert(rec))
        kb.save()

    elif args.cmd == "extract":
        from .extract import extract, read_document
        records = extract(read_document(args.document), source=args.document.name)
        if not records:
            print("문서에서 선정 의사결정을 찾지 못했습니다.")
            return 1
        total = sum(len(e.evidence) for r in records for e in r.evaluations)
        bad = sum(1 for r in records for e in r.evaluations for ev in e.evidence if ev.grounded is False)
        print(f"결정 {len(records)}건 추출. 인용 {total}건 중 원문 미확인 {bad}건.")
        if args.add:
            for rec in records:
                print("등록:", kb.upsert(rec))
            kb.save()
        else:
            out = args.out or args.document.with_suffix(".decisions.yaml")
            out.write_text(_dump_records(records), encoding="utf-8")
            print(f"검토용 파일: {out}\n내용 확인·수정 후 등록:  python -m washer_onto add {out}")

    elif args.cmd == "list":
        for rec in kb.all():
            print(f"{rec.id}\t{rec.title}\t선정: {rec.chosen}\t{rec.decided_on or ''}")

    elif args.cmd == "audit":
        from .audit import FAIL, audit, audit_all, render
        reports = [audit(kb, args.id)] if args.id else audit_all(kb)
        print("\n\n".join(render(r) for r in reports))
        return 2 if any(r.verdict == FAIL for r in reports) else 0

    elif args.cmd == "ask":
        from .qa import ask
        res = ask(kb, args.question)
        print(res["answer"], "\n\n--- 사용된 SPARQL ---\n" + res["sparql"], sep="")

    elif args.cmd == "sparql":
        result = kb.query(args.query)
        print("\t".join(str(v) for v in result.vars or []))
        for row in result:
            print("\t".join("" if v is None else str(v) for v in row))

    elif args.cmd == "export-okf":
        from .okf import export_bundle
        print("OKF 번들:", export_bundle(kb, args.out))

    elif args.cmd == "rules":
        from . import rules as R
        if args.rcmd == "mine":
            res = R.mine_rules(kb)
            kb.save()
            print(f"규칙 후보 {res['proposed']}건 제안/갱신, 사람이 정한 규칙 {res['kept']}건 유지, 대기 질문 {res['open_questions']}건")
        elif args.rcmd == "extract":
            from .extract import read_document
            res = R.extract_rules(kb, read_document(args.document), source=args.document.name)
            kb.save()
            print(f"규칙 후보 {res['proposed']}건 제안, 대기 질문 {res['open_questions']}건")
        elif args.rcmd == "list":
            for r in kb.rules(status=args.status):
                req = "필수" if r.mandatory else "권장"
                print(f"[{r.status}] {r.id}\t{r.category} · {r.criterion}\t{r.spec()}\t{req}\t{r.confirmed_by}")
        elif args.rcmd == "questions":
            for q in kb.questions(status="open"):
                print(f"{q.id}\n  {q.text}")
                for i, o in enumerate(q.options, 1):
                    print(f"    {i}) {o}")
        elif args.rcmd == "answer":
            q = kb.get_question(args.question_id)
            text = args.answer
            if q and text.isdigit() and 1 <= int(text) <= len(q.options):
                text = q.options[int(text) - 1]
            rule, summary = R.answer(kb, args.question_id, text, args.by)
            kb.save()
            print(f"[{rule.status}] {rule.statement}\n  변경: {summary}")
        elif args.rcmd == "history":
            for rev in kb.get_rule(args.rule_id).history:
                print(f"{rev.at}  {rev.by}\n  {rev.note}")

    elif args.cmd == "delete":
        kb.delete(args.id)
        kb.save()
        print("삭제:", args.id)

    return 0


if __name__ == "__main__":
    sys.exit(main())
