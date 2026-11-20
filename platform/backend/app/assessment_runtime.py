"""Per-assessment callbacks; worker threads never write database state."""
from contextlib import contextmanager
from contextvars import ContextVar
from . import assessment_jobs

_checkpoint = ContextVar('assessment_checkpoint', default=None)


@contextmanager
def checkpoint_handler(callback):
    token = _checkpoint.set(callback)
    try:
        yield
    finally:
        _checkpoint.reset(token)


def completed(controls, findings, failed_ids=()):
    assessment_jobs.check_cancelled()
    callback = _checkpoint.get()
    if findings and callback:
        callback(findings)
    assessment_jobs.publish(completed_ids=[c['id'] for c in controls],
                            validated_ids=[f['control_id'] for f in findings], failed_ids=list(failed_ids))
