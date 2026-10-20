"""Program-driven translation runs for connected AI clients; no manual approvals."""

import threading


def register_workflow_tools(server, library, tool, directory=None):
    from .agent_runner import CodexRunner
    from .service import TranslationService
    from .source_repair import SourceRepairer
    from .workflow import Workflow

    jobs, lock = {}, threading.Lock()

    def workflow(runner=False):
        service = TranslationService(library, directory)
        agent = CodexRunner() if runner else None
        repairer = SourceRepairer(library, agent, service.directory / 'source-repair') if agent else None
        return Workflow(service, agent, source_repairer=repairer)

    @tool()
    def start_translation_workflow(paper_id: str, experiment_id: str, max_units: int = 32) -> dict:
        """Create/resume a source-only run. Existing Korean outputs are never imported."""
        return workflow().start(paper_id, experiment_id, max_units=max_units)

    @tool()
    def get_translation_workflow_report(run_id: str) -> dict:
        """Read generation/review/repair progress and concrete blocking errors."""
        result = workflow().report(run_id)
        with lock:
            job = jobs.get(run_id)
            running = bool(job and job["thread"].is_alive())
            if job and not running:
                # The worker may have committed after the first DB snapshot.
                # Once it has stopped, read its final durable state again.
                result = workflow().report(run_id)
            result["background_running"] = running
            result["execution_error"] = job.get("error") if job else None
        return result

    @tool()
    def run_translation_workflow(run_id: str) -> dict:
        """Run actual configured AI generation→independent review→repair in the background.

        Returns immediately; poll get_translation_workflow_report. This runs the
        local account's Codex CLI, not a model hidden inside the PDF reader.
        It never publishes an unfinished run or uses previously translated papers.
        """
        instance = workflow(runner=True)
        instance.report(run_id)  # source/identity check before background dispatch
        with lock:
            previous = jobs.get(run_id)
            if previous and previous["thread"].is_alive():
                return {"run_id": run_id, "background_running": True, "already_running": True}
            record = {"error": None, "instance": instance}

            def execute():
                try:
                    while True:
                        report = instance.step(run_id)
                        if report["status"] in ("ready", "published", "needs_review", "cancelled", "source_changed"):
                            if report["status"] == "ready":
                                instance.commit(run_id, publish=False)
                            break
                except Exception as exc:
                    record["error"] = str(exc)

            thread = threading.Thread(target=execute, daemon=True, name="pairper-translation")
            record["thread"] = thread
            jobs[run_id] = record
            thread.start()
        return {"run_id": run_id, "background_running": True, "published": False}

    @tool()
    def finalize_translation_workflow(run_id: str) -> dict:
        """Locally publish only when every task has fresh grounded review and source gates pass."""
        with lock:
            previous = jobs.get(run_id)
            if previous and previous["thread"].is_alive():
                raise ValueError("The AI execution is still running")
        return workflow().commit(run_id, publish=True)

    @tool()
    def cancel_translation_workflow(run_id: str) -> dict:
        """Stop the actual AI subprocess; completed tasks and failure evidence remain resumable."""
        with lock:
            record = jobs.get(run_id)
            if record:
                record["instance"].runner.cancel()
        return {"run_id": run_id, "cancellation_requested": bool(record), "history_retained": True}

    @server.prompt()
    def run_korean_translation_system() -> str:
        return (
            "Use start_translation_workflow with an explicit experiment identifier, then "
            "run_translation_workflow and poll get_translation_workflow_report. The program "
            "selects source-only tasks, attaches original images, calls AI and performs independent "
            "review/correction. Do not manually replace candidate text or approve an unresolved "
            "run. needs_review means actual remaining problems. Finalize only a ready run, "
            "source_changed means a PDF-grounded source correction was applied; start the same "
            "experiment again to create an empty translation run for the new source. "
            "then verify actual reader display. Previous Korean data is not a fresh-run input."
        )
