# 세탁기 개발 모델 선정 온톨로지

세탁기 개발 과정에서 **어떤 기준과 근거로 모델을 골랐는지** 기록하고, **기준대로 골랐는지** 자동 검증하는 시스템입니다.
여기서 "모델"은 부품 모델(모터·댐퍼·베어링 등), 제품 모델, AI/제어 알고리즘 모델처럼 여러 후보 중 고른 대상을 모두 뜻합니다.

## 무엇을 하나

| 기능 | 설명 | LLM 필요 |
|---|---|---|
| 결정 등록 | 웹 폼 또는 YAML로 후보·기준·평가·근거를 등록 | 아니오 |
| 문서에서 추출 | 회의록·검토서(txt/md/pdf/docx)에서 결정 내용을 추출, 사람이 검토 후 저장 | 예 (Azure OpenAI) |
| 검증 | 기록 누락, 필수 기준 미달, 근거 누락, 더 우수한 대안 여부를 판정 | 아니오 |
| 규칙 에이전트 | 결정·문서에서 선정 규칙 후보를 찾고, 개발자에게 질문해 확정·수정·폐기한 뒤 검증과 등록 폼에 적용 | 일부 (아래 참고) |
| 대화형 에이전트 | 질문에 도구로 답하고, MCP 도구 연결·컨텍스트 압축·세션 이력·모델 자동 전환을 지원 | 예 |
| 질의 | 자연어 질문 → SPARQL → 답변, 또는 SPARQL 직접 실행 | 자연어만 예 |
| 내보내기 | RDF(Turtle), Google Open Knowledge Format 번들 | 아니오 |

검증은 LLM을 쓰지 않는 규칙 기반이라 같은 기록에는 항상 같은 판정이 나옵니다.

## 설치와 실행

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # Azure OpenAI 키·엔드포인트·배포명 입력

python -m washer_onto add examples/decisions.yaml   # 예시 2건 등록 (가상 데이터)
python -m washer_onto audit                         # 검증
streamlit run app.py                                # 웹 화면
```

`.env` 변수명은 `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`(배포 이름),
`AZURE_OPENAI_API_VERSION`(선택)입니다. 이미 다른 이름을 쓰고 있다면 `washer_onto/llm.py` 상단의 별칭 목록을 확인하세요.

## 명령행

```bash
python -m washer_onto extract examples/meeting_notes.txt   # 문서 → 검토용 YAML
python -m washer_onto add meeting_notes.decisions.yaml     # 검토 후 등록
python -m washer_onto audit [결정ID]                        # 부적합이 있으면 종료코드 2
python -m washer_onto ask "소음 기준이 사용된 결정은?"
python -m washer_onto sparql 'SELECT ?t WHERE { ?d a wm:SelectionDecision ; rdfs:label ?t }'
python -m washer_onto export-okf -o okf_bundle
python -m washer_onto list | delete <결정ID>
```

지식베이스는 기본적으로 `data/kb.ttl` 한 파일입니다(`--kb` 또는 웹은 `WASHER_KB` 환경변수로 변경).

## 대화형 에이전트

웹 화면의 "대화" 탭이나 `python -m washer_onto chat --user <이름>`으로 씁니다.
구성원이 질문하면 에이전트가 지식베이스 도구를 호출해 확인한 뒤 답합니다.

### 설정

`.env`만으로 동작합니다. 모델을 여러 개 쓰려면 배포 이름을 **성능 오름차순**으로 적습니다.

```
AZURE_OPENAI_MODELS=작은배포,중간배포,큰배포
```

가격·MCP 서버·압축 기준까지 정하려면 `agent_config.example.json`을 `agent_config.json`으로 복사해 채웁니다
(토큰이 들어갈 수 있어 git에는 올라가지 않습니다). 가격을 넣으면 대화별 비용이 표시되고, 비우면 토큰 수만 표시됩니다.

### 모델 선택과 전환

| 상황 | 동작 |
|---|---|
| 평소 | 질문 난이도를 추정해 시작 모델을 고름 (단순 조회 → 작은 모델, 분석·비교 → 큰 모델). 추가 LLM 호출 없음 |
| 모델이 어렵다고 판단 | `escalate` 도구로 스스로 상위 모델에 넘김 |
| 도구 호출 오류가 2번 연속 | 상위 모델로 자동 전환 |
| 배포 호출 실패 | 등급이 가까운 다른 배포로 대체 |
| 요약·압축 | 항상 가장 작은 모델 |
| 사용자가 지정 | 화면의 모델 선택 또는 `/model <배포이름>`으로 고정, `/model auto`로 해제 |

어떤 답변을 어떤 모델이 몇 토큰으로 만들었는지, 언제 왜 전환했는지 모두 이력에 남습니다.

### 비용 절감 (컨텍스트 압축)

- **대화 요약 압축**: 프롬프트가 `compact_at_tokens`를 넘으면 최근 `keep_recent_turns`턴을 뺀 앞부분을 요약으로 바꿔 보냅니다. 요약은 누적 갱신되며 사용자 의도, 확인된 수치와 ID, 미해결 사항을 남기도록 지시합니다. `/compact`로 직접 실행할 수도 있습니다.
- **도구 결과 자르기**: 긴 도구 결과는 `tool_output_chars`까지만 모델에 보여줍니다.
- **캐시 친화적 순서**: 고정 시스템 프롬프트와 도구 정의를 맨 앞에 두어 Azure OpenAI의 자동 프롬프트 캐시가 적용되기 쉽게 했습니다.
- 압축해도 **원문 이력은 DB에 그대로** 남아 화면에서 볼 수 있습니다.

### 세션·이력·사용자 의도

`data/sessions.db`(SQLite)에 저장합니다.

- 사용자별 대화 세션: 목록에서 골라 이어가기, 삭제
- 메시지 원문, 도구 호출과 결과, 메시지별 모델·토큰
- 동작 이력: 모델 전환, 압축, 도구 승인/거절, 명령어
- **기억**: "앞으로 항상 표로 답해줘"처럼 계속 지켜 달라는 지시는 사용자별로 저장되어 모든 대화에 적용됩니다. `/memory`로 보고 `/forget <번호>`로 지웁니다.

### 승인 (사람 확인)

지식베이스를 바꾸는 도구(`save_decision`, `answer_rule_question`, `mine_rules`)와 허용 목록에 없는 MCP 도구는
사용자가 승인해야 실행됩니다. 거절하면 실행하지 않고 모델에 거절 사실을 알립니다.
상태가 모두 DB에 있어, 승인 대기 중에 프로세스가 재시작돼도 이어서 진행됩니다.

### MCP 연결

**외부 MCP 서버의 도구를 에이전트에 붙이기** – `agent_config.json`의 `mcp_servers`에 적습니다.

```json
"mcp_servers": {
  "사내위키": {"command": "npx", "args": ["-y", "<패키지>"], "auto_approve": ["search"]},
  "PLM":     {"url": "https://<호스트>/mcp", "headers": {"Authorization": "Bearer <토큰>"}}
}
```

stdio와 streamable HTTP를 지원합니다. `auto_approve`에 적은 도구만 승인 없이 실행됩니다(`true`면 전부).
연결에 실패한 서버는 건너뛰고 화면에 사유를 표시합니다.

**이 지식베이스를 MCP 서버로 제공하기** – 다른 에이전트(사내 Copilot 등)가 온톨로지를 도구로 쓸 수 있습니다.

```bash
python -m washer_onto.mcp_server          # stdio
python -m washer_onto.mcp_server --http   # streamable HTTP
```

### 대화 명령어

`/model`, `/model <배포이름>`, `/model auto`, `/compact`, `/memory`, `/forget <번호>`, `/usage`, `/help`

## 규칙 에이전트

개별 결정에 흩어진 기준을 조직의 **선정 규칙**으로 끌어올리는 흐름입니다. 규칙의 최종 권한은 사람에게 있습니다.

1. **제안** – 에이전트가 규칙 후보를 만듭니다.
   - `rules mine`: 등록된 결정에서 분류별로 반복되는 기준을 찾습니다 (LLM 불필요).
   - `rules extract <문서>`: 설계 기준서·회의록에서 규칙을 뽑습니다 (Azure OpenAI). 근거 문장이 원문에 없으면 그 사실을 질문에 밝힙니다.
2. **질문** – 후보마다 확인할 점을 질문으로 만듭니다. 예: "모든 모터 선정에 적용하는 규칙입니까?", "결정마다 기준이 다른데(48dB / 50dB) 표준은 무엇입니까?"
3. **답변 → 갱신** – 개발자가 웹 화면의 "규칙·질문" 탭이나 `rules answer`로 답합니다.
   - 선택지 답변은 그대로 반영됩니다 (LLM 불필요).
   - 자유 서술 답변("대용량은 46dB로 강화됐어요")은 Azure OpenAI가 해석해 수치·필수 여부·이유를 고치고, 정보가 부족하면 추가 질문을 만듭니다.
   - 누가 언제 무엇을 바꿨는지 변경 이력에 남습니다.
4. **사용** – 확정된 규칙은
   - 같은 분류의 모든 결정 검증에 적용되고 (`RULE_MISSING`, `RULE_DEVIATION`, `RULE_VIOLATION`),
   - 결정 등록 폼에서 분류를 입력하면 기준 표에 미리 채워지고,
   - OKF 번들의 `rules/`에 확정자와 함께 실립니다.

확정·폐기된 규칙은 에이전트가 다시 실행돼도 덮어쓰지 않습니다. 다시 검토하려면 화면에서 "제안 상태로 되돌리기"를 누릅니다.

```bash
python -m washer_onto rules mine
python -m washer_onto rules questions
python -m washer_onto rules answer 모터-탈수-소음-q1 1 --by 박검증          # 선택지 번호
python -m washer_onto rules answer 모터-효율-q1 "IE4 등급부터는 90% 이상" --by 김모터   # 자유 서술
python -m washer_onto rules list --status 확정
python -m washer_onto rules history 모터-탈수-소음
```

질문은 웹 화면의 대기열에 쌓이는 방식입니다. 메신저로 담당자에게 직접 보내는 기능은 없습니다.

## 검증 기준

| 코드 | 심각도 | 내용 |
|---|---|---|
| STRUCTURE | 오류/경고/참고 | SHACL 구조 검증: 선정 후보가 후보 목록에 없음, 기준 없음, 대안 후보 없음, 결정자·사유·검토자 누락 등 |
| THRESHOLD_FAIL | 필수면 오류 | 선정 후보의 측정값이 기준 임계값을 만족하지 못함 |
| MISSING_EVAL / NO_MEASUREMENT | 필수면 오류 | 선정 후보의 평가값이 없음 |
| NO_EVIDENCE | 경고 | 선정 후보 평가에 근거 자료가 없음 |
| UNGROUNDED_QUOTE | 경고 | LLM이 추출한 인용문이 원문에 없음 |
| NOT_BEST | 경고 | 필수 기준을 통과한 다른 후보의 가중 점수가 3점 넘게 높음 |
| RULE_MISSING | 필수 규칙이면 오류 | 확정 규칙의 기준이 결정에 없음 |
| RULE_DEVIATION | 경고 | 결정의 기준이 확정 규칙보다 느슨함 |
| RULE_VIOLATION | 필수 규칙이면 오류 | 선정 후보의 측정값이 확정 규칙을 만족하지 못함 |

판정: 오류가 있으면 **부적합**, 경고만 있으면 **보완필요**, 없으면 **적합**.
가중 점수는 정량 기준을 후보 간 min-max 정규화(방향 반영)하고 정성 기준은 평점/5로 환산해 가중 평균한 100점 만점 값입니다.
규칙은 `washer_onto/schema/shapes.ttl`(구조)과 `washer_onto/audit.py`(정량)에서 조정합니다.

## 구조

```
washer_onto/
  schema/washer.ttl   OWL 온톨로지 (W3C PROV-O 정렬)
  schema/shapes.ttl   SHACL 검증 규칙
  models.py           결정 기록 모델 (YAML·폼·LLM 출력 공통)
  store.py            RDF 저장/조회 (rdflib)
  audit.py            검증
  rules.py            규칙 에이전트 (제안 → 질문 → 답변 반영 → 적용)
  mcp_server.py       이 지식베이스를 MCP 서버로 제공
  agent/core.py       대화형 에이전트의 도구 호출 루프, 승인 처리
  agent/router.py     모델 선택·승급·장애 대체
  agent/compaction.py 컨텍스트 구성과 요약 압축
  agent/sessions.py   세션·메시지·이력·기억 저장 (SQLite)
  agent/mcp_client.py 외부 MCP 서버 연결
  agent/tools.py      에이전트 도구 등록부 (kbtools.py 가 실제 기능)
  extract.py          문서 추출 + 인용문 원문 대조
  qa.py               자연어 질의
  okf.py              Open Knowledge Format 내보내기
  llm.py              Azure OpenAI 연결
app.py                Streamlit 웹 화면
examples/             예시 결정 기록과 회의록 (가상 데이터)
```

온톨로지 핵심 관계:

```
선정 의사결정 ─ hasCandidate ─▶ 후보 모델      (chosenCandidate = 선정된 후보)
             ─ hasCriterion ─▶ 선정 기준      (연산자·임계값·단위·가중치·필수)
             ─ hasEvaluation ▶ 평가 ─ ofCandidate / onCriterion / hasEvidence ─▶ 근거 자료
             ─ decidedBy / reviewedBy ─▶ 담당자
             ─ forProject / category
```

## 사용한 오픈소스

- [rdflib](https://github.com/RDFLib/rdflib) – RDF 그래프·SPARQL
- [pySHACL](https://github.com/RDFLib/pySHACL) – SHACL 검증
- [W3C PROV-O](https://www.w3.org/TR/prov-o/) – 의사결정 출처 표현의 상위 온톨로지
- [Google Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf) – 내보내기 형식
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) – MCP 클라이언트·서버
- [openai](https://github.com/openai/openai-python) (Azure OpenAI), [Streamlit](https://github.com/streamlit/streamlit), pydantic, pypdf, python-docx

### Google Open Knowledge Format(OKF)을 쓰는 방식

OKF는 YAML frontmatter가 달린 마크다운 디렉터리로 지식을 표현하는 Google의 공개 스펙(v0.2)입니다.
온톨로지 언어가 아니라서 추론·제약 검증은 못 하므로, **정본은 RDF에 두고 OKF는 배포 형식**으로 씁니다.

- 결정 1건 = 개념 문서 1개(`decisions/`), 기준별 문서(`criteria/`), 규칙별 문서(`rules/`)가 서로 링크됩니다.
- `sources` = 근거 문서, `verified` = 검토자(`human:`)와 자동 검증 통과(`process:`), `status: draft` = 부적합 결정.
- 번들을 git 저장소나 사내 AI 에이전트의 지식 폴더에 그대로 넣을 수 있습니다.

## 운영 시 참고

- `.env`와 `data/`는 `.gitignore`에 있어 저장소에 올라가지 않습니다.
- 저장소는 단일 Turtle 파일입니다. 동시에 여러 명이 저장하면 마지막 저장이 이깁니다.
  사용자가 늘면 `store.py`의 `KB`를 Apache Jena Fuseki나 Oxigraph 같은 트리플 스토어로 바꾸세요.
- 대화의 사용자 구분은 입력한 이름 기준입니다. 로그인·권한 확인은 없으므로 사내망 안에서만 쓰거나 앞단에 인증을 두세요.
- MCP 서버가 돌려준 내용은 외부 자료입니다. 신뢰할 수 있는 서버만 연결하고, 쓰기 기능이 있는 도구는 `auto_approve`에 넣지 마세요.
- 문서 추출 결과는 초안입니다. 수치가 원문과 맞는지 사람이 확인한 뒤 저장하세요.
- 테스트: `pytest`
