"""Cooperative cancellation for the single-worker local development server.

No model output or company context is stored here. In-flight remote inference
can finish, but cancellation prevents retries, subsequent batches and commit.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from threading import RLock
from time import monotonic
from uuid import uuid4

from fastapi import HTTPException

_lock = RLock()
_jobs = {}
_current = ContextVar('question_generation', default=None)


class QuestionGenerationCancelled(Exception):
    pass


def reserve():
    with _lock:
        now = monotonic()
        for key, job in list(_jobs.items()):
            if job['status'] != 'running' and now - job['updated'] > 3600:
                del _jobs[key]
        if len(_jobs) >= 256:
            raise HTTPException(429, 'Too many question generation jobs; retry later')
        key = str(uuid4())
        _jobs[key] = {'status': 'pending', 'updated': now}
        return {'id': key, 'status': 'pending'}


def cancel(key):
    with _lock:
        job = _jobs.get(str(key))
        if job is None:
            raise HTTPException(404, 'Question generation job not found')
        if job['status'] in {'pending', 'running'}:
            job.update(status='cancelled', updated=monotonic())
        return {'id': str(key), 'status': job['status']}


def check_cancelled():
    key = _current.get()
    if key:
        with _lock:
            if _jobs[key]['status'] == 'cancelled':
                raise QuestionGenerationCancelled('หยุดการสร้างคำถามแล้ว ไม่มี Assessment ถูกบันทึก')


@contextmanager
def generation(key):
    if key is None:
        yield
        return
    key = str(key)
    with _lock:
        job = _jobs.get(key)
        if job is None:
            raise HTTPException(404, 'Question generation job not found; create a new job')
        if job['status'] == 'cancelled':
            raise QuestionGenerationCancelled('หยุดการสร้างคำถามแล้ว ไม่มี Assessment ถูกบันทึก')
        if job['status'] != 'pending':
            raise HTTPException(409, 'Question generation job has already been used')
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
