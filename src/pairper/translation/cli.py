"""Run the same source-bound workflow outside an MCP conversation."""

import argparse
import io
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pairper.storage.library import Library

from .agent_runner import CodexRunner
from .service import TranslationService
from .source_repair import SourceRepairer
from .workflow import Workflow


def main(argv=None):
    # Windows console/pipe defaults can be CP949, which cannot encode source
    # tokens such as ⟦math_id⟧. Keep JSON and diagnostics UTF-8; StringIO capture
    # streams have no byte encoding and must not be reconfigured.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
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
    parser.add_argument("--math-partition-v2", action="store_true", help="새 빈 실험에서 원문 수식 경계를 native 제안·독립 PDF 검토한 후 번역")
    parser.add_argument("--recheck-source", metavar="REASON", help="원문 문제로 차단된 작업을 새 PDF 검사로 재확인; 번역 승인 아님")
    parser.add_argument(
        "--retry-failed",
        metavar="REASON",
        help="실행 오류로 막힌 작업만 명시적으로 재시도; 품질 검토와 수정 횟수는 유지",
    )
    args = parser.parse_args(argv)
    if args.plan_only and args.math_partition_v2:
        parser.error("V2 source partition planning calls native AI and cannot use plan-only")
    if args.plan_only and args.recheck_source is not None:
        parser.error("Source reinspection calls AI and cannot be combined with plan-only")
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
        repairer = (
            SourceRepairer(library, runner, service.directory / "source-repair") if runner else None
        )
        instance = Workflow(service, runner, source_repairer=repairer)
        start_options = {"max_units":args.max_units,"max_source_chars":args.max_source_chars}
        if args.math_partition_v2:
            start_options["math_partition_v2"] = True
            if args.retry_failed is not None:
                start_options["source_planning_retry_reason"] = args.retry_failed
        report = instance.start(pid, args.experiment, **start_options)
        print(json.dumps({"event": "started", **report}, ensure_ascii=False), flush=True)
        if args.recheck_source is not None and report['status'] not in ('published', 'ready'):
            report = instance.recheck_source_blocked(report['run_id'], args.recheck_source)
            print(json.dumps({'event': 'source_rechecked', **report}, ensure_ascii=False), flush=True)
            if report['status'] == 'source_changed':
                report = instance.start(pid, args.experiment, **start_options)
                print(json.dumps({'event': 'fresh_restart_after_source_repair', **report}, ensure_ascii=False), flush=True)
        if args.retry_failed is not None and report["status"] not in ("published", "ready"):
            report = instance.retry_failed(report["run_id"], args.retry_failed)
            print(
                json.dumps({"event": "execution_retry_requested", **report}, ensure_ascii=False),
                flush=True,
            )
        if args.plan_only:
            return report
        source_restarts = 0
        for _ in range(args.max_steps):
            if report["status"] in ("published", "needs_review"):
                break
            try:
                report = instance.step(report["run_id"])
            except ValueError:
                # A parallel source worker can publish a new view while this
                # worker is awaiting its old lease's response. Only a validated
                # identity change enters the existing bounded empty-restart path.
                observed = instance.observe_source_change(report['run_id'])
                if observed is None:
                    raise
                report = observed
            print(json.dumps({"event": "progress", **report}, ensure_ascii=False), flush=True)
            if report["status"] == "source_changed":
                source_restarts += 1
                if source_restarts > 3:
                    report = {
                        **report,
                        "status": "needs_review",
                        "error": "Source repair restart limit exceeded",
                    }
                    break
                report = instance.start(pid, args.experiment, **start_options)
                print(
                    json.dumps(
                        {"event": "fresh_restart_after_source_repair", **report}, ensure_ascii=False
                    ),
                    flush=True,
                )
            # A new explicit CLI invocation resumes a previously cancelled run;
            # cancellation during this invocation still stops it immediately.
            if report["status"] == "cancelled":
                break
            if report["status"] == "ready":
                report = instance.commit(report["run_id"], publish=args.publish)
                break
        return report

    def execute_isolated(pid):
        try:
            return execute(pid)
        except Exception as exc:
            report = {"paper_id": pid, "status": "failed", "error": str(exc)}
            print(json.dumps({"event": "paper_failed", **report}, ensure_ascii=False), flush=True)
            return report

    pool = ThreadPoolExecutor(max_workers=args.workers)
    try:
        reports = list(pool.map(execute_isolated, ids))
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
    return 0 if all(r["status"] != "failed" and (
        r["status"] in ("ready", "published") or args.plan_only) for r in reports) else 2


if __name__ == "__main__":
    raise SystemExit(main())
