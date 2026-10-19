import asyncio
import pytest
from tests.test_runtime_offline import runtime
from shmax.queue.repository import DeliveryQueueRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [True, False])
async def test_invalid_payload_terminal_without_retry(runtime, empty):
    rt = runtime
    for t in rt._worker_tasks:
        t.cancel()
    await asyncio.gather(*rt._worker_tasks, return_exceptions=True)
    rt._worker_tasks.clear()
    calls = []

    async def rejected(**kwargs):
        calls.append(kwargs)
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import SendMessage

        raise TelegramBadRequest(
            method=SendMessage(chat_id=100, text="synthetic"),
            message="Bad Request: message text is empty",
        )

    rt.telegram_adapter.send_message = rejected
    async with rt.db.session() as s:
        row = await DeliveryQueueRepository(s).enqueue(
            direction="max_to_telegram",
            target_chat_id="100",
            payload={
                "text": "" if empty else "synthetic",
                "direction": "max_to_telegram",
            },
        )
    rec = await rt._claim_one(60)
    await rt._drain_one(rec)
    async with rt.db.session() as s:
        saved = await DeliveryQueueRepository(s).get(queue_id=row.id)
    assert saved.status == "failed" and saved.attempts == 0
    assert len(calls) == (0 if empty else 1)
    assert saved.last_error == (
        "InvalidPayload:empty_outbound_payload"
        if empty
        else "TelegramBadRequest:empty_text"
    )
    await rt.journal.flush()
    events = rt.journal.query(search="delivery.failed")["events"]
    assert events
    assert not rt.journal.query(search="delivery.success")["events"]
