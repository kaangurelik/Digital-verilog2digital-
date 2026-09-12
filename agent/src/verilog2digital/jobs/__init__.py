"""Durable jobs with isolated native workers and immutable published revisions."""

__all__ = ["JobError", "JobLimits", "cancel_job", "job_status", "recover_jobs", "run_inspection", "run_job", "run_validation"]


def __getattr__(name):
    if name in __all__:
        from . import manager
        return getattr(manager, name)
    raise AttributeError(name)
