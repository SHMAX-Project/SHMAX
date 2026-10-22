import asyncio
import pytest
from tests.test_runtime_offline import runtime
from shmax.adapters.max.base import MaxEvent
from shmax.adapters.max.errors import MaxAuthError
from shmax.max_recovery import MaxRecovery


async def eventually(fn):
    async with asyncio.timeout(5):
        while not fn():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_reconnect_rebinds_one_consumer_and_delivers(runtime):
    rt = runtime
    a = rt.max_adapter
    a.generation = 0
    failures = [TimeoutError(), ConnectionError(), None]

    async def connect_saved():
        error = failures.pop(0) if failures else None
        if error:
            raise error
        a._events = asyncio.Queue()
        a.generation += 1
        a._started = True
        a._connected = True

    original = a.health

    async def health():
        return {**await original(), "session_available": True}

    a.connect_saved = connect_saved
    a.health = health
    rec = MaxRecovery(rt, initial_delay=0.01, retry_cap=0.02, health_interval=0.02)
    rt.max_recovery = rec
    a.set_connected(False)
    rec.start()
    await eventually(lambda: rec.status["state"] == "HEALTHY")
    assert rec.status["attempts"] == 3
    rec.status["last_success_at"]=12345
    await rec.attempt()
    assert rec.status["last_success_at"]==12345
    for n in range(4):
        old = a._events
        rec.trigger()
        await eventually(
            lambda: a._events is not old and rec.status["state"] == "HEALTHY"
        )
        await old.put(MaxEvent(f"old-{n}", "max-dialog-1", "m1", "obsolete"))
        await a.inject_message(MaxEvent(f"new-{n}", "max-dialog-1", "m1", f"new-{n}"))
        await eventually(
            lambda: any(
                f"new-{n}" in m["text"] for m in rt.telegram_adapter.sent_messages
            )
        )
        assert (
            len(
                [
                    t
                    for t in asyncio.all_tasks()
                    if t.get_name() == "shmax.max_listener" and not t.done()
                ]
            )
            == 1
        )
    assert not any("obsolete" in m["text"] for m in rt.telegram_adapter.sent_messages)
    await eventually(lambda:len(rt.journal.query(search="delivery.success")["events"])>=4)
    await rt.journal.flush()
    assert len(rt.journal.query(search="delivery.success")["events"]) >= 4
    await rec.close()


@pytest.mark.asyncio
async def test_auth_failure_not_retried_rapidly(runtime):
    rt = runtime
    a = rt.max_adapter

    async def health():
        return {"connected": False, "authenticated": False, "session_available": True}

    async def rejected():
        raise MaxAuthError("synthetic revoked token")

    a.health = health
    a.connect_saved = rejected
    a.generation = 0
    rec = MaxRecovery(rt, initial_delay=0.01)
    rt.max_recovery = rec
    rec.start()
    await eventually(lambda: rec.status["state"] == "AUTH_REQUIRED")
    n = rec.status["attempts"]
    await asyncio.sleep(0.05)
    assert rec.status["attempts"] == n and rec.status["retry_in_seconds"] is None
    await rec.close()


@pytest.mark.asyncio
async def test_missing_session_never_connects(runtime):
    rt = runtime
    a = rt.max_adapter

    async def health():
        return {"connected": False, "authenticated": False, "session_available": False}

    async def forbidden():
        pytest.fail("must not authenticate")

    a.health = health
    a.connect_saved = forbidden
    rec = MaxRecovery(rt)
    rt.max_recovery = rec
    assert not await rec.attempt() and rec.status["state"] == "AUTH_REQUIRED"


@pytest.mark.asyncio
async def test_shutdown_cancels_hanging_connect(runtime):
    rt = runtime
    a = rt.max_adapter
    started = asyncio.Event()

    async def health():
        return {"connected": False, "authenticated": False, "session_available": True}

    async def hanging():
        started.set()
        await asyncio.Event().wait()

    a.health = health
    a.connect_saved = hanging
    rec = MaxRecovery(rt)
    rt.max_recovery = rec
    rec.start()
    await started.wait()
    await asyncio.wait_for(rec.close(), 1)
    assert rec.task is None


@pytest.mark.asyncio
async def test_real_adapter_callbacks_and_no_sms(tmp_path, monkeypatch):
    from shmax.adapters.max import pymax_adapter as module
    from shmax.adapters.max.config import PyMaxAdapterConfig
    from shmax.adapters.max.errors import MaxSessionError

    callbacks = []

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.is_connected = False

        def on_message(self):
            def register(fn):
                callbacks.append(fn)
                return fn

            return register

        def on_message_edit(self):
            return lambda fn: fn

        def on_disconnect(self):
            return lambda fn: fn

        async def close(self):
            pass

    adapter = module.PyMaxMaxAdapter(
        PyMaxAdapterConfig(phone=None, session_dir=tmp_path)
    )
    monkeypatch.setattr(module, "_PyMaxClient", Client)
    monkeypatch.setattr(adapter, "_from_pymax_message", lambda x, *args, **kwargs: x)
    first = adapter._build_client(saved_only=True)
    old = adapter._handle
    adapter._build_client(saved_only=True)
    new = adapter._handle
    assert adapter.generation == 2
    await callbacks[0]("obsolete", None)
    await callbacks[1]("current", None)
    assert old.events.empty() and await new.events.get() == "current"
    with pytest.raises(MaxSessionError):
        await first.kwargs["auth_flow"].authenticate(None)
    assert (
        first.kwargs["extra_config"].reconnect is False
        and first.kwargs["extra_config"].relogin is False
    )
    await adapter.disconnect()
    assert new.disconnect_event.is_set()


def test_sdk_revoked_login_and_temporary_api_are_distinct():
    from pymax import ApiError
    from pymax.protocol.enums import Opcode
    from shmax.adapters.max.pymax_adapter import _classify_pymax_error
    from shmax.adapters.max.errors import MaxAuthError, MaxTransportError

    revoked = ApiError(opcode=Opcode.LOGIN, error="FAIL_LOGIN_TOKEN")
    temporary = ApiError(opcode=Opcode.LOGIN, error="TEMPORARY_UNAVAILABLE")
    assert isinstance(_classify_pymax_error(revoked), MaxAuthError)
    assert isinstance(_classify_pymax_error(temporary), MaxTransportError)


def test_temporary_probe_is_not_auth_required():
    import time
    from shmax.integration_status import evidence_state
    from shmax.adapters.max.fake import FakeMaxAdapter
    from shmax.config import RuntimeMode

    adapter = FakeMaxAdapter()
    probe = {
        "adapter": adapter,
        "at": time.monotonic(),
        "error": True,
        "error_class": "verification_unavailable",
    }
    assert (
        evidence_state(True, adapter, probe, None, time.monotonic(), RuntimeMode.TEST)
        == "VERIFICATION_UNAVAILABLE"
    )
    probe["error_class"] = "auth_rejected"
    assert (
        evidence_state(True, adapter, probe, None, time.monotonic(), RuntimeMode.TEST)
        == "AUTH_REQUIRED"
    )

@pytest.mark.asyncio
async def test_verified_recovery_does_not_wait_for_stale_watchdog(runtime):
    import dataclasses,time
    from sqlalchemy import select
    from shmax.database.models import MaxAccount
    from shmax.watchdog.base import AdapterHealthState
    rt=runtime
    async with rt.db.session() as session:
        row=(await session.execute(select(MaxAccount))).scalars().first()
        row.phone='synthetic-fixture-only';await session.commit()
    adapter=rt.max_adapter;adapter.generation=1;rt._max_listener_generation=1
    rec=MaxRecovery(rt);rt.max_recovery=rec;rec.status['state']='HEALTHY'
    rt.max_watchdog._snapshot=dataclasses.replace(rt.max_watchdog.snapshot,state=AdapterHealthState.DOWN)
    rt._integration_probes['max']={'adapter':adapter,'at':time.monotonic(),'wall_at':int(time.time()),'verified':True,'health':{'connected':True,'authenticated':True,'account_id':'synthetic'}}
    assert (await rt.integration_snapshot())['max']['state']=='HEALTHY'
    rt._integration_probes['max'].update(error=True,error_class='verification_unavailable')
    assert (await rt.integration_snapshot())['max']['state']=='VERIFICATION_UNAVAILABLE'

@pytest.mark.asyncio
async def test_real_adapter_cancels_both_waiters_without_leaking_old_queue(tmp_path):
    from types import SimpleNamespace
    from shmax.adapters.max.pymax_adapter import PyMaxMaxAdapter,_PyMaxHandle
    from shmax.adapters.max.config import PyMaxAdapterConfig
    adapter=PyMaxMaxAdapter(PyMaxAdapterConfig(phone=None,session_dir=tmp_path))
    async def close():pass
    for generation in range(1,4):
        adapter._generation=generation
        adapter._handle=_PyMaxHandle(client=SimpleNamespace(close=close),events=asyncio.Queue(),started=True)
        iterator=adapter.listen_events();consumer=asyncio.create_task(anext(iterator))
        await asyncio.sleep(0);await asyncio.sleep(0)
        consumer.cancel();await asyncio.gather(consumer,return_exceptions=True);await iterator.aclose()
        await adapter.disconnect()
        assert not [task for task in asyncio.all_tasks() if task.get_name().startswith('shmax.max.event.') and not task.done()]
