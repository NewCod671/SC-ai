"""Cooperative cancellation for the single-worker local development server.

No model output or company context is stored here. Active HTTP inference is interrupted and receives a remote stop signal;
cancellation also prevents retries, subsequent batches and commit.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from threading import RLock, Event, Thread
from time import monotonic
from uuid import uuid4

from fastapi import HTTPException

_lock = RLock()
_jobs = {}
_current = ContextVar('assessment_run', default=None)


class AssessmentCancelled(Exception):
    pass


def reserve(assessment_id):
    with _lock:
        now = monotonic()
        for key, job in list(_jobs.items()):
            if job['status'] != 'running' and now - job['updated'] > 3600:
                del _jobs[key]
        if len(_jobs) >= 256:
            raise HTTPException(429, 'Too many assessment run jobs; retry later')
        key = str(uuid4())
        _jobs[key] = {'status': 'pending', 'updated': now, 'assessment_id': str(assessment_id), 'event': Event(), 'stops': {}, 'progress': {'sequence': 0, 'phase': 'pending', 'total': 0, 'completed': 0, 'validated': 0, 'failed': 0}, 'done_ids': set(), 'valid_ids': set(), 'failed_ids': set()}
        return {'id': key, 'status': 'pending'}


def _dispatch(callback):
    Thread(target=callback, daemon=True, name='inference-stop').start()


def cancel(key):
    callbacks = []
    with _lock:
        job = _jobs.get(str(key))
        if job is None:
            raise HTTPException(404, 'Assessment run job not found')
        if job['status'] in {'pending', 'running'}:
            job.update(status='cancelled', updated=monotonic())
            job['event'].set()
            callbacks = list(job['stops'].values())
            job['stops'].clear()
        result = {'id': str(key), 'status': job['status']}
    # The cancel endpoint never waits on Colab or a stuck model request.
    for callback in callbacks:
        _dispatch(callback)
    return result


def cancel_event():
    key = _current.get()
    with _lock:
        return _jobs[key]['event'] if key else None


@contextmanager
def remote_stop(callback):
    key = _current.get()
    if key is None:
        yield
        return
    token = uuid4().hex
    with _lock:
        job = _jobs[key]
        cancelled = job['event'].is_set()
        if not cancelled:
            job['stops'][token] = callback
    if cancelled:
        _dispatch(callback)
    try:
        check_cancelled()
        yield
    finally:
        with _lock:
            job['stops'].pop(token, None)


def snapshot(key):
    with _lock:
        job = _jobs.get(str(key))
        if job is None:
            raise HTTPException(404, 'Assessment run job not found')
        return {'id': str(key), 'status': job['status'], **job['progress']}


def publish(*, phase=None, total=None, completed_ids=(), validated_ids=(), failed_ids=()):
    key = _current.get()
    if key is None:
        return
    with _lock:
        job = _jobs[key]
        if job['status'] not in {'pending', 'running'}:
            return
        job['done_ids'].update(completed_ids)
        job['valid_ids'].update(validated_ids)
        job['failed_ids'].update(failed_ids)
        progress = job['progress']
        before={k:v for k,v in progress.items() if k!='sequence'}
        if phase is not None:progress['phase'] = phase
        if total is not None:progress['total'] = total
        progress.update(completed=len(job['done_ids']), validated=len(job['valid_ids']), failed=len(job['failed_ids']))
        if before!={k:v for k,v in progress.items() if k!='sequence'}:progress['sequence'] += 1


def check_cancelled():
    key = _current.get()
    if key:
        with _lock:
            if _jobs[key]['status'] == 'cancelled':
                raise AssessmentCancelled('หยุดประเมินแล้ว เก็บข้อที่ตรวจสำเร็จไว้สำหรับรันต่อ คำตอบและผลเดิมยังอยู่')


@contextmanager
def generation(key, assessment_id):
    if key is None:
        yield
        return
    key = str(key)
    with _lock:
        job = _jobs.get(key)
        if job is None:
            raise HTTPException(404, 'Assessment run job not found; create a new job')
        if job['assessment_id'] != str(assessment_id):
            raise HTTPException(409, 'Assessment job belongs to another assessment')
        if any(other is not job and other['assessment_id'] == str(assessment_id) and other['status'] == 'running' for other in _jobs.values()):
            raise HTTPException(409, 'An assessment is already running')
        if job['status'] == 'cancelled':
            raise AssessmentCancelled('หยุดประเมินแล้ว เก็บข้อที่ตรวจสำเร็จไว้สำหรับรันต่อ คำตอบและผลเดิมยังอยู่')
        if job['status'] != 'pending':
            raise HTTPException(409, 'Assessment run job has already been used')
        job.update(status='running', updated=monotonic())
    token = _current.set(key)
    try:
        yield
    finally:
        _current.reset(token)
        with _lock:
            if job['status'] == 'running':
                job.update(status='failed', updated=monotonic())


@contextmanager
def commit_guard():
    # Cancellation and saving have one ordering: if cancel wins, no commit;
    # if commit wins, the cancel endpoint honestly reports completed.
    with _lock:
        check_cancelled()
        yield
        key = _current.get()
        if key:
            _jobs[key].update(status='completed', updated=monotonic())
