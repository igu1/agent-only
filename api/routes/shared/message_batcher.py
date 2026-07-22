import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable


@dataclass
class _Batch:
    last_received: float
    parts: list[str] = field(default_factory=list)
    task: asyncio.Task | None = None


_batches: dict[str, _Batch] = {}
_lock = asyncio.Lock()


def _join(parts: list[str]) -> str:
    return "\n".join([p for p in parts if p])


async def enqueue(*, key: str, text: str, debounce_seconds: float, handler: Callable[[str], Awaitable[None]]) -> None:
    loop = asyncio.get_running_loop()

    async with _lock:
        batch = _batches.get(key)
        if batch is None:
            batch = _Batch(last_received=loop.time())
            _batches[key] = batch

        batch.parts.append(text)
        batch.last_received = loop.time()

        if batch.task is None or batch.task.done():
            batch.task = asyncio.create_task(_run(key=key, debounce_seconds=debounce_seconds, handler=handler))


async def _run(*, key: str, debounce_seconds: float, handler: Callable[[str], Awaitable[None]]) -> None:
    loop = asyncio.get_running_loop()

    while True:
        await asyncio.sleep(debounce_seconds)

        async with _lock:
            batch = _batches.get(key)
            if batch is None:
                return

            since_last = loop.time() - batch.last_received
            if since_last < debounce_seconds:
                continue

            text = _join(batch.parts)
            del _batches[key]

        if text:
            try:
                await handler(text)
            except Exception:
                import logging

                logging.exception(f"Batch handler error (key={key})")
        return
