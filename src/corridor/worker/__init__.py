"""The worker process: the outbox loop and the scheduled jobs.

A composition root, like the API: it wires handlers and jobs from the modules below it and
nothing imports it.
"""

from corridor.worker.jobs import build_jobs
from corridor.worker.main import Worker, build_registry, run
from corridor.worker.scheduler import Job, Scheduler

__all__ = [
    "Job",
    "Scheduler",
    "Worker",
    "build_jobs",
    "build_registry",
    "run",
]
