"""Explicit fixed-selection entry point; does not alter already running v6 workers."""
from .cli import main as run_cli
from .selection_runner import SelectionRunner
from .selection_workflow import SelectionWorkflow


def main(argv=None):
    return run_cli(argv, workflow_class=SelectionWorkflow, runner_class=SelectionRunner)


if __name__ == '__main__':
    raise SystemExit(main())
