"""Exercise cancellation with real async HTTP lifecycle and mocked sockets, no GPU."""
import asyncio
from threading import Event, Thread
from uuid import uuid4
import httpx
import pytest
from fastapi import HTTPException
from app import assessment_jobs as jobs, cancellable_http
from .test_gateway import gateway
from .test_inference_diagnostics import fake_gateway


@pytest.fixture(autouse=True)
def socket_free_async_loop(monkeypatch):
    # This Windows sandbox blocks socketpair(), even for a mocked HTTP client.
    # No real I/O is used here; keep the actual asyncio task/timer/cancel logic.
    import selectors
    import time
    class TimerSelector(selectors.SelectSelector):
        def select(self, timeout=None):
            if not self.get_map():
                if timeout:time.sleep(min(timeout,0.05))
                return []
            return super().select(timeout)
    class SocketFreeLoop(asyncio.SelectorEventLoop):
        def __init__(self):super().__init__(selector=TimerSelector())
        def _make_self_pipe(self):pass
        def _close_self_pipe(self):pass
        def _write_to_self(self):pass
    monkeypatch.setattr(asyncio.events,'new_event_loop',SocketFreeLoop)


def test_active_http_is_interrupted_without_waiting_for_timeout(monkeypatch):
    entered,closed,remote_stop=Event(),Event(),Event()
    original_async=httpx.AsyncClient
    original_sync=httpx.Client
    infer_handles=[]
    async def waiting(request):
        import json
        infer_handles.append(json.loads(request.content)['cancellation_id'])
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
    def stopping(request):
        assert request.headers['Authorization']=='Bearer synthetic-key'
        assert request.url.path==f'/infer/{infer_handles[0]}/cancel'
        remote_stop.set()
        return httpx.Response(200,json={'status':'stop_requested'})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original_async(transport=httpx.MockTransport(waiting),**kwargs))
    monkeypatch.setattr(httpx,'Client',lambda **kwargs:original_sync(transport=httpx.MockTransport(stopping),**kwargs))
    job=jobs.reserve('http-interrupt')
    outcomes=[]
    def worker():
        try:
            with jobs.generation(job['id'],'http-interrupt'):
                cancellable_http.post('https://synthetic.example',{'Authorization':'Bearer synthetic-key'},{'operation':'assess'},180)
        except jobs.AssessmentCancelled:
            outcomes.append('cancelled')
    thread=Thread(target=worker,daemon=True)
    thread.start()
    try:
        assert entered.wait(5)
        assert jobs.cancel(job['id'])['status']=='cancelled'
        assert remote_stop.wait(2)
        thread.join(2)
        assert not thread.is_alive() and closed.is_set() and outcomes==['cancelled']
    finally:
        jobs.cancel(job['id'])
        thread.join(2)


def test_http_success_unregisters_remote_stop(monkeypatch):
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'output':[]})),**kwargs))
    job=jobs.reserve('http-success')
    with jobs.generation(job['id'],'http-success'):
        response=cancellable_http.post('https://synthetic.example',{}, {},180)
        assert response.json()=={'output':[]}
        with jobs._lock:assert jobs._jobs[job['id']]['stops']=={}
        with jobs.commit_guard():pass
    assert jobs.cancel(job['id'])['status']=='completed'


def test_gateway_stop_before_infer_prevents_loading(fake_gateway):
    key=uuid4().hex
    assert gateway.cancel_inference(key,'Bearer test-key')['status']=='stop_requested'
    with pytest.raises(HTTPException) as error:
        gateway.infer(gateway.Request(operation='assess',model='test-model',system='test',payload={},cancellation_id=key),'Bearer test-key')
    assert error.value.status_code==409 and 'messages' not in fake_gateway


def test_gateway_cancel_is_authenticated(fake_gateway):
    with pytest.raises(HTTPException) as error:
        gateway.cancel_inference(uuid4().hex,'Bearer wrong-key')
    assert error.value.status_code==401


def test_gpu_generation_exits_at_next_stop_check(fake_gateway,monkeypatch):
    key=uuid4().hex
    generator=gateway.model_pipeline()
    calls=[]
    def generate(messages,**options):
        calls.append('started')
        criterion=options['stopping_criteria'][0]
        assert criterion(None,None) is False
        gateway.cancel_inference(key,'Bearer test-key')
        criterion(None,None)
        pytest.fail('Cancelled generation must not complete or return partial JSON')
    monkeypatch.setattr(type(generator),'__call__',lambda self,*args,**kwargs:generate(*args,**kwargs))
    with pytest.raises(HTTPException) as error:
        gateway.infer(gateway.Request(operation='assess',model='test-model',system='test',payload={},cancellation_id=key),'Bearer test-key')
    assert error.value.status_code==409 and calls==['started']
    assert gateway.last_inference_stats['status']=='cancelled'
    assert gateway.last_inference_stats['valid_output'] is False
    assert gateway.lock.acquire(blocking=False)
    gateway.lock.release()


def test_cancel_queued_inference_without_waiting_for_gpu(fake_gateway):
    key=uuid4().hex
    outcomes=[]
    def worker():
        try:
            gateway.infer(gateway.Request(operation='assess',model='test-model',system='test',payload={},cancellation_id=key),'Bearer test-key')
        except HTTPException as error:outcomes.append(error.status_code)
    gateway.lock.acquire()
    thread=Thread(target=worker,daemon=True)
    thread.start()
    try:
        gateway.cancel_inference(key,'Bearer test-key')
        thread.join(2)
        assert not thread.is_alive() and outcomes==[409]
        assert 'messages' not in fake_gateway
    finally:
        gateway.lock.release()
        thread.join(2)


def test_normal_completion_with_cancel_handle_remains_success(fake_gateway):
    key=uuid4().hex
    result=gateway.infer(gateway.Request(operation='assess',model='test-model',system='test',payload={},cancellation_id=key),'Bearer test-key')
    assert result['output']==[]
    assert gateway.cancel_inference(key,'Bearer test-key')['status']=='finished'


def test_inference_timeout_also_signals_remote_stop(monkeypatch):
    original_async=httpx.AsyncClient
    remote_stop=Event()
    async def timeout(request):
        raise httpx.ReadTimeout('synthetic timeout',request=request)
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original_async(transport=httpx.MockTransport(timeout),**kwargs))
    monkeypatch.setattr(jobs,'_dispatch',lambda callback:remote_stop.set())
    job=jobs.reserve('timeout-stop')
    with jobs.generation(job['id'],'timeout-stop'):
        with pytest.raises(httpx.ReadTimeout):
            cancellable_http.post('https://synthetic.example',{}, {},180)
    assert remote_stop.is_set()
