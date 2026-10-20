"""Run the same source-bound workflow outside an MCP conversation."""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pairper.storage.library import Library

from .agent_runner import CodexRunner
from .service import TranslationService
from .source_repair import SourceRepairer
from .workflow import Workflow


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pairper 원문 기반 한국어 번역 실행·독립검토·수정")
    parser.add_argument("paper_ids", nargs="*")
    parser.add_argument("--all", action="store_true", help="현재 원문 뷰가 있는 논문 전체")
    parser.add_argument("--library", type=Path)
    parser.add_argument("--directory", type=Path, help="번역/실행 이력 저장소; 원본과 분리")
    parser.add_argument("--experiment", required=True, help="새 실행은 새 이름, 재개는 같은 이름")
    parser.add_argument("--max-units", type=int, default=32)
    parser.add_argument("--max-source-chars", type=int, default=18000)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--model", help="현재 계정과 CLI가 실제 지원하는 모델(미지정 시 CLI 기본)")
    parser.add_argument("--codex", type=Path, help="사용할 Codex native 실행 파일")
    parser.add_argument("--model-catalog", type=Path, help="선택 근거인 실제 계정 model/list 응답")
    parser.add_argument("--max-steps", type=int, default=10000)
    parser.add_argument("--publish", action="store_true", help="모든 검토 통과 후 로컬 확정")
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 4 or not 1 <= args.max_steps <= 10000:
        parser.error("workers must be1..4 and max-steps1..10000")
    library = Library(args.library)
    ids = args.paper_ids
    if args.all:
        ids = [p["id"] for p in library.list_papers() if p.get("has_view")]
    if not ids:
        parser.error("Select paper IDs or --all")
    if len(set(ids)) != len(ids):
        parser.error("Duplicate paper IDs")
    runners = []

    def execute(pid):
        runner = (
            None
            if args.plan_only
            else CodexRunner(
                cli_path=args.codex, model=args.model, model_catalog_path=args.model_catalog
            )
        )
        if runner:
            runners.append(runner)
        service = TranslationService(library, args.directory)
        repairer = SourceRepairer(library, runner, service.directory / 'source-repair') if runner else None
        instance = Workflow(service, runner, source_repairer=repairer)
        report = instance.start(
            pid, args.experiment, max_units=args.max_units, max_source_chars=args.max_source_chars
        )
        print(json.dumps({"event": "started", **report}, ensure_ascii=False), flush=True)
        if args.plan_only:
            return report
        source_restarts = 0
        for _ in range(args.max_steps):
            if report["status"] in ("published", "needs_review"):
                break
            report = instance.step(report["run_id"])
            print(json.dumps({"event": "progress", **report}, ensure_ascii=False), flush=True)
            if report['status'] == 'source_changed':
                source_restarts += 1
                if source_restarts > 3:
                    report = {**report, 'status': 'needs_review', 'error': 'Source repair restart limit exceeded'}
                    break
                report = instance.start(pid, args.experiment, max_units=args.max_units,
                                        max_source_chars=args.max_source_chars)
                print(json.dumps({'event': 'fresh_restart_after_source_repair', **report}, ensure_ascii=False), flush=True)
            # A new explicit CLI invocation resumes a previously cancelled run;
            # cancellation during this invocation still stops it immediately.
            if report["status"] == "cancelled":
                break
            if report["status"] == "ready":
                report = instance.commit(report["run_id"], publish=args.publish)
                break
        return report

    pool = ThreadPoolExecutor(max_workers=args.workers)
    try:
        reports = list(pool.map(execute, ids))
    except KeyboardInterrupt:
        for runner in runners:
            runner.cancel()
        print(json.dumps({"status": "interrupted", "completed_tasks_retained": True}))
        return 130
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    print(json.dumps({"event": "finished", "papers": reports}, ensure_ascii=False))
    return 0 if all(r["status"] in ("ready", "published") or args.plan_only for r in reports) else 2


if __name__ == "__main__":
    raise SystemExit(main())
