# Pairper 해설 생성 시스템

현재 원문 뷰에서 개념·전문용어와 단독/문단 내 수식만 해설 대상으로 잡는다. 문장이나 일반 구를
대상으로 등록하지 않는다. 원문·한국어 번역 파일은 수정하지 않고 `explanations/`에 별도로 저장한다.

`python -m pairper.explanation.cli --library <경로> --all --experiment stage3 --publish`는
전체 논문을 원문 블록별로 나누어 독립 용어 선정 → 고정 목록의 생성 → 별도 내용 검토 →
최대 2회 수정·재검토를 실행한다. 기본 CLI와 MCP 모두 이 v7 경로를 사용한다.
같은 실험 이름으로 다시 실행하면 완료한 배치를 재사용하고 중단 지점부터 계속한다.
`--max-steps`는 실행량 제한일 뿐 완료 기준을 낮추지 않는다. `--plan-only`는 모델 호출 없이 계획만 만든다.

규약을 갱신한 경우 `--adopt-from-run <이전 실행 ID>`로 동일 원문·동일 전체 배치 계획의 후보를
가져올 수 있다. 이전 생성 산출물과 해시는 보존한다. 아래의 엄격한 v6 통과 호환 증명을 제외한
검토와 승인 상태는 전부 폐기한다. 새 필드가
없으면 구조 수정 대상으로 돌리고, 현재 규약과 읽기 화면을 사용한 새 독립검토를 통과해야 공개한다.
진행 중인 이전 규약의 작업도 모델 호출을 마친 경계에서 규약 변경을 감지해 공개를 중단한다.

구조 오류 수정과 실제 의미검토의 수정 횟수는 별도로 센다. 기본 상한은 각각 2회이며,
`--max-repairs 4`로 각각 최대 4회까지 명시적으로 선택할 수 있다. 규약 이전 시 실제 요청 기록에서
복원한 기존 수정 횟수와 알려진 미해결 문제를 보존하고, 상한 변경 전후도 기록한다. 이번 실험에서는
검토 때마다 새로운 실제 전문용어 누락이 발견되고 구조 오류가 의미 수정 횟수까지 소모했던 결과를
근거로 상한을 4회로 확장했다. 검토 기준을 완화하거나 과거 거절을 삭제한 것은 아니다.
한 배치가 상한에 도달해도 실패를 그대로 남긴 채 나머지 배치를 계속 처리한다. 최종 공개는
모든 배치의 현재 후보가 새 독립검토를 통과한 경우에만 가능하다.

MCP는 `start_explanation_workflow` → `run_explanation_workflow` →
`get_explanation_workflow_report`로 사용한다. 자동 실행은 전체 통과 후에만 로컬 공개한다.
`needs_review`는 수정 횟수 안에 해소하지 못한 실제 문제가 있다는 뜻이며 공개하지 않는다.

용어는 종류를 먼저 밝히고 무엇인지 직접 설명한다. 수식은 원문 문맥에서 기호가 가리키는 실제
대상을 설명한 뒤, 전체 의미와 계산 과정을 별도로 설명한다. 설명 문장에서 사람말을 먼저 쓰고
바로 뒤에 해당 수학 기호를 붙인다. 숫자 예시를 의무적으로 만들지 않으며 근거 없는 사례도 만들지 않는다.
단독 변수는 기호 의미만 설명하고 가짜 계산 과정을 붙이지 않는다.
숫자 자체가 수식 토큰으로 추출된 경우에는 그 숫자가 논문에서 무엇을 세거나 측정하는지 설명한다.
저자 소속 첨자·목록 기호·단독 연산자처럼 해설 대상이 아닌 표기는 원문 문맥에 근거한 제외 사유를
기록하고 별도 검토를 받는다. 이 결정을 포함해 모든 원문 수학 토큰의 처리를 확인한다.
수학 표현이 많은 블록은 최대 16개 대상씩 나누되, 모든 대상은 정확히 한 번 처리하고 용어 발견은
첫 분할에만 수행한다. 원문 수식이 여러 조각으로 나뉘어 있으면 전체 문맥의 관계를 보존해야 한다.
용어는 논문 안에서 같은 뜻을 공유하는 항목에만 같은 `concept_key`를 부여한다. 현재 한국어 읽기
화면의 원문 단위 연결을 모델에 제공해 실제 표시된 용어를 별칭으로 선택한다. 한국어 화면은 별칭
연결에만 사용하며 과학적 정의의 근거는 원문으로 유지한다. 한국어 번역이 검토 도중이나 이후에
바뀌면 기존 별칭 검토를 무효화하고 최신 화면으로 다시 검토한다.

각 모델 호출은 기존 번역 실행기의 임시 작업폴더·도구/네트워크 비활성화·구조화 출력·취소/시간 제한
절차를 재사용한다. 해설 생성과 의미검토는 다른 초기화된 호출이며 같은 모델의 별도 검토임을 숨기지 않는다.
호출별 원문 요청, 프롬프트, 스키마, 응답, 이벤트, 사용량, 해시를 남긴다. 검토는 후보 전체와 SHA256으로
결부되며 생성 후 원문이 달라지거나 검토 이후 후보가 달라지면 공개를 막는다.
잘못된 원문 인용 범위나 기호 바인딩 같은 구조 오류도 정확한 원인을 다음 수정 호출에 전달한다.
같은 잘못된 후보를 승인하거나 버리고 완료 처리하지 않으며, 수정 한도를 넘기면 검토 필요 상태로 남긴다.
용어 인용문이 해당 원문 단위에서 정확히 한 번만 나오는 경우에는 프로그램이 유니코드 좌표를 직접
계산한다. 여러 번 나오거나 인용문이 일치하지 않으면 추정하지 않는다. 모델의 원본 응답은 그대로
보존하고 좌표를 정정한 응답·이전과 이후 좌표·각각의 해시를 별도 기록한다.

전체 블록 방문과 정확한 기호/범위 검증은 구조적 안전장치다. 이것만으로 모든 독자의 이해도나 모든
전문용어 발견을 증명하지 않는다. 별도 의미검토와 실제 읽기 화면 검증을 통해 품질을 평가한다.

v7은 용어 선정과 해설 내용 검토를 분리한다. 원문 텍스트 묶음마다 별도 선정 검토가 기존 후보,
알려진 누락, 모든 텍스트 단위를 한 번에 확인하고 용어 목록·선정 이유·원문 좌표·해시를 고정한다.
이후 생성은 목록에서 빠진 해설만 작성하고, 수정은 내용에 문제가 있는 항목만 다시 작성한다.
독립 내용 검토는 고정된 목록과 모든 수식을 빠짐없이 검토한다. 나중에 발견한 선택 대상 밖의
전문용어는 정확한 원문 좌표와 이유를 가진 보완 기록으로 남긴다. 이를 근거로 사전처럼 모든
단어를 해설했다고 주장하지 않는다. 이미 작성한 설명의 이해를 막는 전문용어, 틀린 기호 의미,
빠진 계산 과정은 여전히 내용 오류이며 게시를 차단한다. 다음 업데이트에서 새 선정 버전을
만들어 보완 대상을 검토할 수 있지만 이번 구현에는 자동 사전 확장 엔진을 넣지 않는다.

v6의 실패·미검토 후보는 이력과 수정 횟수를 보존하고 새 선정 및 내용 검토를 받는다. v6의 실제
전체 범위 검토를 통과한 항목만 제한적으로 승계할 수 있다. 원본 요청·응답·프롬프트·스키마·원본
응답 해시, 현재 원문·한국어 화면·후보·수학 대상 집합이 모두 일치하고 이후 거절이 없어야 한다.
이 경우도 새 v7 검토라고 기록하지 않고 `legacy_v6_fullscope_pass` 근거를 명시한다.
일반 텍스트 안에서 뒤늦게 발견한 관계식은 기존 수식 배치를 바꾸지 않고 별도 필수 배치로 추가한다.
추가 수식 목록의 해시가 바뀌면 이전 실행은 그대로 게시할 수 없다.

모델 검토가 통과했어도 외부 감사에서 기존 기준에 맞지 않는 구체적 내용 오류가 확인되면
`record_content_finding`으로 원문 대상·관찰한 필드 값·원래 기준·지적 이유·기존 통과 후보 해시를
결부해 다시 연다. category 동의어 반복 및 exact alias 정정의 기존 one-shot은 해당 finding에만
추가 수정 1회를 허용한다. '구조', '연산', 약어 해독처럼 유용한 범주를 획일화하지 않는다.
같은 finding은 다른 실행에서도 예산을 다시 받을 수 없고 기존 통과·거절·수정 횟수는 보존한다.
process 정정과 term_referent 정정은 이 추가 예산을 사용하지 않는다. 등록·실행 모두 남은 일반
semantic 예산이 있어야 한다. process는 최초 통과 과정 전체와 순서를 유지하며 한 연속 설명
그룹만 보완한다. term_referent는 선택한 대상 자체를 다른 종류로 잘못 정의했을 때만 category,
description, concept_key를 함께 고친다. 원문 인용·좌표·ID·aliases 및 다른 모든 항목은 불변이며,
새 key는 모델이 원문에 맞게 선택한다. 기존 manifest와 통과 증거를 보존한 semantic-key amendment를
기록하고 새 manifest·후보·현재 원문/한국어 문맥에 묶어 전체 science와 KIND를 다시 검토한다.
이 일반예산 경로에서 거절 후 재수정도 동일 finding/used1과 이전 거절을 유지하며 남은 일반
예산만 사용한다. 기존 one-shot의 실패를 이 경로로 우회할 수 없다. 미통과 상태는 게시하지 않는다.
이는 기존 미해결 거절의 상한을 우회하거나 검토 기준을 새로 만드는 경로가 아니다.

예산 감사는 중첩된 이전 실행의 실제 수정 요청도 재귀적으로 확인한다. 실제 call 경로가 같은
중첩 복사만 중복 제거하고, 내용 해시가 같아도 다른 call은 각각 센다. 구조·의미 혼합 요청은
양쪽에 분류할 수 있으며 기존 값과 실제 요청 수 중 큰 값만 사용한다. 당시의 total repairs와
모든 거절 이력은 그대로 남긴다. 뒤늦은 계수 교정만으로 실제 통과한 해설을 무효화하지 않는다.

이미 거절된 process에 새로 승인한 추가 실험은 pass-only finding 경로와 분리한다.
`record_rejected_content_experiment`는 현재 거절 후보·검토·원문·고정 목록의 해시, 해당 대상,
구체적 승인 이유를 기록하고 그 process만 추가 1회 수정하도록 제한한다. 일반 상한은 유지하고
다른 필드·대상·기존 오류에 이 예산을 쓸 수 없다. 같은 실험의 반복 예산은 막으며, 전체 고정
목록의 새 독립검토까지 통과하지 못하면 `needs_review`로 남긴다.


### Focused category-kind certificate

The v7 default workflow separately checks whether each selected term category identifies its kind. The prompt and decision schema are the same as the two native prototype experiments; no vocabulary blacklist, term discovery, or scientific rewriting is added. Only each term's exact source quote/unit, concept key, category and description are sent. The scientific pass remains independent.

A certificate binds the source fingerprint, frozen manifest fingerprint, exact term content, source-context digest, rule hash and all checked IDs. Identical scopes can reuse it; any changed binding requires a new check. Math-only batches save an explicit empty-term-scope certificate without a model call. Publication requires a current certificate in addition to every existing scientific/source/Korean gate. Previous publication history remains intact.

`SelectionWorkflow.certify_kinds(run_id)` checks one already scientifically reviewed batch without regenerating it. Normal `step`/`run`, MCP execution and the default CLI automatically supplement missing certificates, including an older ready/published run. Recommit through the normal API publishes the updated evidence. A focused rejection records the source-specific reason and blocks publication; category-only repair spends the existing semantic budget, cannot rewrite description or any other field, and must pass both a fresh full scientific review and a focused recheck. The existing source/pass-bound one-shot finding route remains the only explicit additional allowance; no counters are reset and no rejection is silently ignored.

The prototype evidence found the same five known sparsity-family defects in both calls, while all 23 distinct useful-kind controls passed. This supports the small-review hypothesis for that defect family, not a general claim that all category errors are detected.


### Exact Korean alias diagnostics and corrections

`get_explanation_anchor_report(paper_id, run_id)` returns unresolved published term IDs, their source bindings, current translated provenance fields (deduplicated), translation identity and the requested run's current candidate hashes. This is diagnostic evidence, not automatic equivalence: there is no casefold matching and no claim that every translated paraphrase is discoverable.

Register a same-concept literal through `record_explanation_alias_finding`, then use the existing `run_explanation_workflow`. The backend requires the exact current translation identity and exactly one literal occurrence inside that original source unit's translated provenance. It records the translated field hash/coordinates and original source unit hash. A changed revision requires a new bound proposal; repeating the same revision/target/field/code cannot grant another allowance. Python exposes the same operation as `SelectionWorkflow.record_alias_finding`.

This uses the existing source/pass-bound one-shot finding ledger without resetting normal semantic counters. A published paper's previous store/history is preserved while its workflow resumes as a draft. The model must independently verify that the proposed literal names the same original concept. Repair may append only the exact proposed alias; every existing alias, other target/source/concept field and complete explanation must remain unchanged. Fresh full scientific review and a new applicable KIND certificate precede normal API republication. Ambiguous/nonliteral/wrong-revision proposals fail closed with a diagnostic reason.

### Retained selection response after host syntax corrections

The only source-acknowledgement normalization removes duplicate `checked_unit_keys` when their exact unique set already equals every required source unit. It never fills omissions/unknown units or deduplicates selected targets. Raw provider response and old rejection/counter history remain immutable; removed acknowledgements and before/after hashes/counts are persisted. `revalidate_selection` can recheck only the latest retained selection call against the original request/schema, current source packet/Korean identity/proposed terms/known issues, then run current host target validation. This is explicit host revalidation, never a new model call or scientific pass. Previously consumed structural counters remain; any subsequent structural failure is bounded normally.


The later v7 alias wording clarifies that “Korean-reader” names refer to the document surface, not the character script. A faithful English proper name actually retained in that document, including its displayed case/ligature spelling, is a valid exact literal. This does not add casefold/fuzzy matching or a new review criterion. Legacy v6 RULES and prior actual prompt artifacts remain unchanged; earlier passes are not relabeled as having read the later clarification.


Invalid selected term offsets now return structured diagnostic feedback: target key/ID, requested source unit, actual matching-unit count, quote, proposed range and every exact occurrence range in that unit. Multiple occurrences remain ambiguous and are never automatically chosen. Unknown/missing units retain count zero with no invented occurrence. The existing JSON feedback path carries the diagnosis to the next bounded selection attempt; protocol versions, source facts and consumed counters are unchanged.

### Bringing an already published workflow into the default library

Generation in an isolated library can publish correct explanations while leaving the app's default workflow directory without the run. Publication alone therefore does not prove that the app can inspect or resume that run. `tools/import_explanation_workflow.py --library <actual-library> --source-state <reviewed-state.json>` makes a read-only plan; `--apply` installs only an absent default workflow after verifying the actual current publication, source, Korean identity, every candidate/review/KIND binding, and repair history. It does not publish or copy database contents.

The importer preserves the state and referenced model-call artifacts byte for byte, including referenced ancestor runs. An external archive index maps each original logical artifact path to a verified local archive; no paths or hashes inside scientific evidence are rewritten. The receipt binds the original state SHA and actual publication revision. Registered archives always take precedence and missing or altered evidence fails closed. A final publication check and atomic no-overwrite installation protect against concurrent changes. Existing conflicting runs are never replaced.

The immutable original-state archive is separate from the normal mutable current state, so later alias findings and ordinary bounded repair can continue. Default report/run and retained-evidence lookups remain usable after the temporary origin disappears. Regression checks cover this continuation, exact ancestry/raw-byte preservation, idempotence, conflicting state, stale publication/translation, archive tampering, logical-path relocation and publication changes during copy.

### Paper-wide definitions in the default generation and review system

The follow-up content audit found definitions elsewhere in the paper that were absent from both the local generation packet and its independent review packet. Those calls could agree while missing the same source information. The default MCP and `pairper-explain` paths now use `PaperContextWorkflow` and `PaperContextRunner`. The fixed-selection contract remains; the complete structured original-paper context is additionally supplied to both content generation/repair and independent scientific review, including appendices and footnotes. No keyword shortlist, previously generated definition, translated prose or hand-written answer replaces the original source. The complete source is emitted once in a stable prompt prefix and remains present in full in the recorded request.

Every generated item returns exact original block/quote references and the role that those references establish. Every reviewed item independently returns definition checks with exact references and a supported/insufficient/contradicted decision. The host checks exact source substrings and complete nonduplicated item coverage; a failed definition check must be an actual content issue and cannot pass. The semantic reviewer must distinguish identifying a kind from actually defining the concept, explain concrete symbol referents, and apply this paper's operation definitions instead of remembered conventions. A citation by itself is not semantic proof.

The complete source corpus SHA, original identity, source fingerprint and definition-rule SHA are included in the scientific review's context binding. Publication rechecks all item references and this full context. Old local-only passes cannot be published as paper-wide passes. `start_explanation_workflow(..., adopt_from_run_id=old_run)` creates a separate source-identical revalidation run, retaining the exact fixed targets and candidate, all prior calls, original scientific reviews, KIND history and repair counters. Every batch receives new scientific and KIND checks. The original run and current publication stay intact until the new run passes and is normally published.

Only a fully reviewed ready/published prior run can enter this new context phase; an unresolved rejection cannot use it to restart a budget. Historical counters remain unchanged. The separately recorded new-context allowance is at most the requested 0..4 additional repairs per kind per batch, measured from the retained historical baseline. This is an explicit new complete-paper evaluation scope, not a reset of earlier consumed attempts. Default fresh-paper runs keep their normal 0..4 limits. Real model calls and exact citations, not test counts or filled fields, are required for the six-paper validation.

The panel now resets its own scroll position when selecting a different target while preserving the paper's position. The regression specifically covers switching without closing the panel; earlier close/open checks did not cover that flow.

Incorrect definition citations remain rejected. Feedback includes the cited block's actual text (up to 800 characters) and exact quote locations across the complete paper (up to 12); an empty quote matches no locations. Later real runs showed exact source quotes paired with the wrong block ID. The host may now correct ONLY that locator when the unchanged verbatim quote occurs in exactly one block of the complete bound paper. Empty, invented, altered, fuzzy or ambiguous quotes are never resolved. Quotes, roles, scientific verdicts and issues remain unchanged. The original provider reply stays immutable, with a separate normalized artifact, source hash and exact before/after correction evidence. This supersedes the earlier no-locator-normalization choice; it does not add scientific source material or excuse a content defect.

`revalidate_retained_generation_locations(run_id, batch_index)` can recheck a completed retained author reply rejected solely for source locations. Original request/response hashes and the full current paper/candidate must match. It resolves only unique verbatim locators, validates the full schema/protocol/grounding, and passes the corrected reply through the existing exact normal generation/repair merge and every narrow-field guard. The reconstructed request must equal the actual original request. It makes zero model calls, retains semantic/structural counters, and records a current source/candidate-bound proof permitting the still-required full independent science review. It grants no science or KIND approval; any later structural failure invalidates the proof. This handles an existing usable author response without inventing a new repair allowance or relabeling its content as reviewed.

Content review must check every inherited issue exactly once. It may additionally check explicitly supplied `complete_alias_findings`; unknown or duplicate checks, missing inherited checks, and unresolved findings in a pass remain rejected. Earlier validation incorrectly rejected these optional known alias checks. `revalidate_retained_alias_review(run_id, batch_index)` rechecks only a completed retained native response affected by that specific host error, with unchanged raw request/response hashes, full paper, candidate, manifest, inherited issues and current alias findings. It validates the complete current schema, content protocol, every literal citation and actual alias anchors again. It preserves both pass and repair verdicts, all scientific issues and consumed counters, makes no model call and grants no retry. A source/candidate-bound host proof allows a retained scientific rejection to use only its remaining ordinary semantic allowance; a subsequent structural failure cannot reuse the proof. Fresh KIND and all normal publication gates still apply. No partial-paper publication follows from this recovery.

### Reader background and preservation of reviewed aliases

Full-paper definitions take priority for notation, tasks, conventions, scales and paper-specific claims. A concise accurate standard definition of an identified term or cited method is valid reader background even when this paper does not teach its mechanism. Such background must not be attributed falsely to this paper or become an extended tutorial. `reader_context` supplies this clarification and every exact current occurrence of prior reviewed aliases, bound to the unchanged prior run, source concept and current translation. All occurrences require independent semantic agreement; no fuzzy matching or automatic equivalence is introduced.

`reassess_reader_context` permits one fresh independent review of an actual bound rejection limited to omitted alias scope or unsupported background detail. Only existing prior-reviewed literal alias metadata can be restored from current complete occurrence proofs. Explanation text, source targets, frozen manifests, consumed counters and previous rejection evidence remain unchanged; no author retry allowance is granted. A stale binding, factual/formula rejection or missing exact alias cannot enter this path. Fresh full science and KIND remain required; another rejection is retained and the same clarification cannot be rerolled.
