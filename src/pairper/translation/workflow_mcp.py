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

    def current_report(instance, run_id):
        try:
            return instance.report(run_id)
        except ValueError:
            observed = instance.observe_source_change(run_id)
            if observed is None:
                raise
            return observed

    def active_count():
        return len({id(j["thread"]) for j in jobs.values() if j["thread"].is_alive()})

    def source_job_report(run_id):
        with lock:
            record=jobs.get(run_id)
            if not record or not record.get("source_planning"):
                return None
            alive=record["thread"].is_alive()
            result=record.get("result")
            error=record.get("error")
        if alive or result is None:
            event=getattr(record["instance"].runner,"cancel_event",None)
            cancelled=bool(event is not None and event.is_set())
            return {"run_id":run_id,"planning_job_id":run_id,
                "status":"cancelled" if cancelled else "source_planning" if alive else "needs_review",
                "background_running":alive,"execution_error":error,"ready_run_id":None,
                "prior_Korean_inputs":0,"translation_approval_granted":False}
        report=current_report(record["instance"],result["run_id"])
        return {**report,"planning_job_id":run_id,"ready_run_id":result["run_id"],
            "background_running":False,"execution_error":error}

    def actual_run_id(run_id):
        pending=source_job_report(run_id)
        if pending is None:
            return run_id
        if pending.get("ready_run_id") is None:
            raise ValueError("Native source-only partition planning is not ready")
        return pending["ready_run_id"]

    @tool()
    def start_translation_workflow(paper_id: str, experiment_id: str, max_units: int = 32,
                                   math_partition_v2: bool = False, source_planning_retry_reason: str | None = None) -> dict:
        """Create/resume a source-only run. Existing Korean outputs are never imported."""
        if not math_partition_v2:
            return workflow().start(paper_id, experiment_id, max_units=max_units)
        from .contract import sha
        from .math_partition import VERSION
        job_id="partition-"+sha([VERSION,paper_id,experiment_id])
        with lock:
            previous=jobs.get(job_id)
            if previous and previous["thread"].is_alive():
                return {"run_id":job_id,"planning_job_id":job_id,"status":"source_planning",
                    "background_running":True,"ready_run_id":None,"prior_Korean_inputs":0}
            if active_count()>=4:
                raise ValueError("At most4native workflow/planning workers may run")
            instance=workflow(runner=True)
            record={"source_planning":True,"instance":instance,"result":None,"error":None}
            def prepare():
                try:
                    record["result"]=instance.start(paper_id,experiment_id,max_units=max_units,
                        math_partition_v2=True, **({"source_planning_retry_reason": source_planning_retry_reason}
                            if source_planning_retry_reason is not None else {}))
                except Exception as exc:
                    record["error"]=str(exc)
            thread=threading.Thread(target=prepare,daemon=True,name="pairper-partition-source")
            record["thread"]=thread
            jobs[job_id]=record
            thread.start()
        return {"run_id":job_id,"planning_job_id":job_id,"status":"source_planning",
            "background_running":True,"ready_run_id":None,"prior_Korean_inputs":0,
            "translation_approval_granted":False}

    @tool()
    def get_translation_workflow_report(run_id: str) -> dict:
        """Read generation/review/repair progress and concrete blocking errors."""
        planning=source_job_report(run_id)
        if planning is not None:
            return planning
        result = current_report(workflow(), run_id)
        with lock:
            job = jobs.get(run_id)
            running = bool(job and job["thread"].is_alive())
            if job and not running:
                # The worker may have committed after the first DB snapshot.
                # Once it has stopped, read its final durable state again.
                result = current_report(workflow(), run_id)
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
        run_id=actual_run_id(run_id)
        instance = workflow(runner=True)
        instance.report(run_id)  # source/identity check before background dispatch
        with lock:
            previous = jobs.get(run_id)
            if previous and previous["thread"].is_alive():
                return {"run_id": run_id, "background_running": True, "already_running": True}
            if active_count()>=4:
                raise ValueError("At most4native workflow/planning workers may run")
            record = {"error": None, "instance": instance}

            def execute():
                try:
                    while True:
                        try:
                            report = instance.step(run_id)
                        except ValueError:
                            observed = instance.observe_source_change(run_id)
                            if observed is None:
                                raise
                            report = observed
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
        run_id=actual_run_id(run_id)
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
            if record and record.get("result"):
                record=jobs.get(record["result"]["run_id"],record)
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
