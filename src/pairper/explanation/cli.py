"""Whole-paper or whole-library explanation workflow, resumable between invocations."""
import argparse
import json
from pathlib import Path

from pairper.storage.library import Library

from .paper_context import PaperContextRunner as ExplanationRunner
from .paper_context import PaperContextWorkflow as ExplanationWorkflow
from .service import ExplanationService


def main(argv=None, *, workflow_class=None, runner_class=None):
    workflow_class = workflow_class or ExplanationWorkflow
    runner_class = runner_class or ExplanationRunner
    parser = argparse.ArgumentParser(description='Pairper 개념·전문용어·수식 해설 생성 및 독립검토')
    parser.add_argument('paper_ids', nargs='*')
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--library', type=Path)
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--experiment', default='default')
    parser.add_argument('--adopt-from-run', help='동일 원문의 이전 후보만 재사용; 검토는 전부 새로 수행')
    parser.add_argument('--max-characters', type=int, default=6500)
    parser.add_argument('--max-steps', type=int, default=10000)
    parser.add_argument('--max-repairs', type=int, default=2,
                        help='구조/의미 각각의 수정 상한 0..4; 이전 수정 이력은 보존')
    parser.add_argument('--codex', type=Path)
    parser.add_argument('--model')
    parser.add_argument('--plan-only', action='store_true')
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args(argv)
    library = Library(args.library)
    papers = ([p['id'] for p in library.list_papers() if p.get('has_view')]
              if args.all else args.paper_ids)
    if not papers or len(set(papers)) != len(papers) or not 1 <= args.max_steps <= 10000:
        parser.error('Select unique paper IDs or --all; max-steps must be 1..10000')
    if args.adopt_from_run and len(papers) != 1:
        parser.error('--adopt-from-run requires exactly one paper')
    if not 0 <= args.max_repairs <= 4:
        parser.error('--max-repairs must be 0..4')
    runner = None if args.plan_only else runner_class(cli_path=args.codex, model=args.model)
    workflow = workflow_class(ExplanationService(library, args.directory), runner,
                                   max_repairs=args.max_repairs)
    results = []
    try:
        for paper_id in papers:
            report = workflow.start(paper_id, experiment_id=args.experiment,
                                    max_characters=args.max_characters,
                                    adopt_from_run_id=args.adopt_from_run)
            print(json.dumps({'event': 'started', **report}, ensure_ascii=False), flush=True)
            if not args.plan_only:
                for _ in range(args.max_steps):
                    if report['status'] in ('ready', 'published', 'needs_review', 'source_changed') and not report.get('kind_pending_batches'):
                        break
                    report = workflow.step(report['run_id'])
                    print(json.dumps({'event': 'progress', **report}, ensure_ascii=False), flush=True)
                if report['status'] == 'ready' and args.publish:
                    workflow.commit(report['run_id'])
                    report = workflow.report(report['run_id'])
            results.append(report)
    except KeyboardInterrupt:
        if runner:
            runner.cancel()
        return 130
    return 0 if args.plan_only or all(r['status'] in ('ready', 'published') for r in results) else 2


if __name__ == '__main__':
    raise SystemExit(main())
