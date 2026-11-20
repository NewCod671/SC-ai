"""Interrupt active inference I/O without leaving a model-request worker behind."""
import asyncio
import logging
from uuid import uuid4
import httpx
from . import assessment_jobs as jobs

logger = logging.getLogger('uvicorn.error')


def post(url, headers, data, timeout):
    event = jobs.cancel_event()
    if event is None:
        raise RuntimeError('Cancellable HTTP requires an assessment job')
    cancellation_id = uuid4().hex
    body = {**data, 'cancellation_id': cancellation_id}

    def stop_remote():
        try:
            with httpx.Client(timeout=2, follow_redirects=False) as client:
                response = client.post(url + '/infer/' + cancellation_id + '/cancel', headers=headers)
                response.raise_for_status()
            logger.info('inference.cancel.sent')
        except Exception as exc:
            # URLs, response bodies, credentials and exception messages must
            # not leak in logs. The class/status is enough to distinguish a
            # disconnected tunnel from a gateway rejecting the cancel call.
            if isinstance(exc, httpx.HTTPStatusError):
                cause = f'http_{exc.response.status_code}'
            else:
                cause = type(exc).__name__
            logger.warning('inference.cancel.remote_unconfirmed cause=%s', cause)

    async def send():
        jobs.check_cancelled()
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            task = asyncio.create_task(client.post(url + '/infer', headers=headers, json=body))
            try:
                while not task.done():
                    if event.is_set():
                        raise jobs.AssessmentCancelled('หยุดประเมินแล้ว เก็บข้อที่ตรวจสำเร็จไว้สำหรับรันต่อ คำตอบและผลเดิมยังอยู่')
                    await asyncio.wait({task}, timeout=0.05)
                jobs.check_cancelled()
                return await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    with jobs.remote_stop(stop_remote):
        try:
            return asyncio.run(send())
        except httpx.RequestError:
            jobs._dispatch(stop_remote)
            raise
