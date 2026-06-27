"""Worker = the thing that actually implements a PROCEED ticket.

Separation of concerns the council pushed for: the DECISION (decide.py, general + real) is
distinct from the IMPLEMENTATION (what bytes to write). In production the worker is the agent
(Claude reads the ticket and edits files). For the hermetic acceptance demo we use a
deterministic DemoWorker so the loop is provable without a live model — but the orchestrator,
state machine, gates, park-semantics and revert it exercises are all the real, general code.

A Worker.apply() makes its edits in `repo_dir` and returns a short description of what it did.
If it cannot implement the ticket it raises WorkerCannotImplement, which the orchestrator maps
to a clean outcome (never a crash, never a stop).
"""
from __future__ import annotations

import os


class WorkerCannotImplement(Exception):
    pass


class Worker:
    def apply(self, ticket, repo_dir: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError


class DemoWorker(Worker):
    """Deterministic stand-in for the agent, used by the acceptance demo only."""

    def apply(self, ticket, repo_dir: str) -> str:
        handler = getattr(self, f"_do_{ticket.id.replace('-', '_')}", None)
        if handler is None:
            raise WorkerCannotImplement(f"DemoWorker has no handler for {ticket.id}")
        return handler(repo_dir)

    # ticket-01-trivial: add a startup log line (low blast-radius, reversible) -> should DONE
    def _do_ticket_01_trivial(self, repo_dir: str) -> str:
        app = os.path.join(repo_dir, "app.py")
        with open(app, "a", encoding="utf-8") as fh:
            fh.write('\nlog("agents-never-sleep demo started")\n')
        return "appended a startup log line to app.py"

    # ticket-03-redgate: break add() -> gate catches FAIL_INTRODUCED_BY_DIFF -> revert + fail
    def _do_ticket_03_redgate(self, repo_dir: str) -> str:
        mu = os.path.join(repo_dir, "mathutil.py")
        with open(mu, "r", encoding="utf-8") as fh:
            src = fh.read()
        src = src.replace("return a + b", "return a - b")
        with open(mu, "w", encoding="utf-8") as fh:
            fh.write(src)
        return "changed add() to subtract in mathutil.py"
