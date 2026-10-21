"""Automatic explanation generation tools; manual demo overlays are not a workflow."""
import threading


def register_explanation_tools(server, library, tool, directory=None):
    from .paper_context import PaperContextRunner as ExplanationRunner
    from .paper_context import PaperContextWorkflow as ExplanationWorkflow
    from .service import ExplanationService

    jobs, lock = {}, threading.Lock()

    def workflow(runner=False, max_repairs=2):
        return ExplanationWorkflow(ExplanationService(library, directory),
                                   ExplanationRunner() if runner else None, max_repairs=max_repairs)

    @tool()
    def get_explanation_guide() -> str:
        """Read the source-grounded automatic explanation generation and review contract."""
        return ('start_explanation_workflow → run_explanation_workflow → '
                'get_explanation_workflow_report. 개념·전문용어와 단독/문단 내 수식만 해설한다. '
                '용어는 종류를 먼저 밝히고 직접 설명한다. 수식은 문맥별 기호 의미, 전체 의미, '
                '계산 과정을 설명한다. 사람말 뒤에 기호를 쓰고 임의 숫자 예시는 강제하지 않는다. '
                '용어 범주는 별도 작은 KIND 검토로 실제 종류를 밝히는지 확인한다. 기존 과학 검토를 '
                '통과한 후보도 같은 실행을 재개하면 과학 검토를 반복하지 않고 이 인증만 보충할 수 있다. '
                '생성과 독립검토 모두 논문 전체 원문에서 필요한 정의·기호 역할·고유 계산 방식을 찾아 '
                '각 항목의 정확한 원문 인용과 함께 확인한다. 기존 실행을 재검증하려면 '
                'start_explanation_workflow(adopt_from_run_id=기존run_id)로 새 문맥 실행을 만든다. '
                '모든 원문 텍스트에서 해설할 용어를 별도 선정 검토로 고정한 뒤 그 목록과 모든 '
                '수식의 내용을 독립검토한다. 뒤늦게 발견한 추가 용어는 좌표와 이유를 보완 기록에 '
                '남기되 잘못된 기호 의미·내용·계산 과정은 반드시 수정한다. '
                '별도 초기화된 모델 검토를 모두 통과한 현재 원문 전체만 공개한다. '
                '한국어 표시 누락은 get_explanation_anchor_report로 정확한 대상과 현재 필드를 확인하고 '
                'record_explanation_alias_finding으로 동일 개념의 고유 literal을 등록한 뒤 run으로 재검토한다. '
                'needs_review는 완료가 아니며 원문 변경 시 새 실행을 시작한다.')

    @tool()
    def start_explanation_workflow(paper_id: str, experiment_id: str = 'default',
                                   max_characters: int = 6500, adopt_from_run_id: str | None = None,
                                   max_repairs: int = 2) -> dict:
        """Plan/resume all text units and display/inline formulas from the current source."""
        return workflow(max_repairs=max_repairs).start(paper_id, experiment_id=experiment_id, max_characters=max_characters,
                                adopt_from_run_id=adopt_from_run_id)

    @tool()
    def get_explanation_workflow_report(run_id: str) -> dict:
        """Read persistent progress and exact failures; reviewed is not the same as published."""
        result = workflow().report(run_id)
        with lock:
            job = jobs.get(run_id)
            result['background_running'] = bool(job and job['thread'].is_alive())
            result['execution_error'] = job.get('error') if job else None
        return result

    @tool()
    def run_explanation_workflow(run_id: str, publish: bool = True) -> dict:
        """Generate→fresh semantic review→repair for the whole paper, then publish if all pass.

        Uses the local Codex login and an isolated no-tools subprocess per model call.
        Returns immediately; poll report. Source changes and unresolved issues block publication.
        """
        instance = workflow(runner=True)
        instance.report(run_id)
        with lock:
            previous = jobs.get(run_id)
            if previous and previous['thread'].is_alive():
                return {'run_id': run_id, 'background_running': True, 'already_running': True}
            record = {'error': None, 'instance': instance}

            def execute():
                try:
                    report = instance.run(run_id)
                    if report['status'] == 'ready' and publish:
                        instance.commit(run_id)
                except Exception as exc:
                    record['error'] = str(exc)

            thread = threading.Thread(target=execute, daemon=True, name='pairper-explanations')
            record['thread'] = thread
            jobs[run_id] = record
            thread.start()
        return {'run_id': run_id, 'background_running': True}

    @tool()
    def cancel_explanation_workflow(run_id: str) -> dict:
        """Terminate the active model process; retain completed evidence for resumption."""
        with lock:
            record = jobs.get(run_id)
            if record:
                record['instance'].runner.cancel()
        return {'run_id': run_id, 'cancellation_requested': bool(record)}

    @tool()
    def get_paper_explanations(paper_id: str, published_only: bool = True) -> dict:
        """Read source-bound term/formula catalog and explanations; suppress stale publications."""
        return workflow().service.get(paper_id, published_only)

    @tool()
    def get_explanation_anchor_report(paper_id: str, run_id: str | None = None) -> dict:
        """Inspect unresolved exact Korean term IDs and their current provenance fields."""
        from .alias_review import diagnostics
        instance = workflow()
        result = diagnostics(instance.service, paper_id)
        if run_id:
            from .catalog import digest
            state = instance._read(run_id)
            if state['paper_id'] != paper_id or state['source_fingerprint'] != result['source_fingerprint']:
                raise ValueError('Alias diagnostics run belongs to another paper/source')
            bindings = {i['target']['id']: digest(b['candidate']) for b in state['batches']
                        for i in (b.get('candidate') or {}).get('items', [])}
            for term in result['unresolved_terms']:
                term['candidate_sha256'] = bindings.get(term['target_id'])
            result['run_id'] = run_id
        return result

    @tool()
    def record_explanation_alias_finding(run_id: str, target_id: str, literal: str,
                                        expected_translation_identity: dict, reason: str,
                                        expected_candidate_sha256: str, complete_occurrences: dict | None = None) -> dict:
        """Record one exact source/pass-bound alias proposal; resume the run for fresh reviews."""
        return workflow().record_alias_finding(run_id, target_id, literal=literal,
            expected_translation_identity=expected_translation_identity, reason=reason,
            expected_candidate_sha256=expected_candidate_sha256, complete_occurrences=complete_occurrences)

    @tool()
    def publish_explanation_workflow(run_id: str) -> dict:
        """Publish only a complete current-source run with candidate-bound semantic reviews."""
        return workflow().commit(run_id)

    return {'jobs': jobs, 'workflow': workflow}
