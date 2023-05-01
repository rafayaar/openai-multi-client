import logging
import asyncio
from dataclasses import dataclass
from threading import Thread
from typing import Any

from aioprocessing import AioJoinableQueue, AioQueue
from tenacity import wait_random_exponential, stop_after_attempt, AsyncRetrying, RetryError
import openai

logger = logging.getLogger(__name__)


@dataclass
class Payload:
    endpoint: str
    data: dict
    metadata: dict | None
    max_retries: int
    retry_multiplier: float
    retry_max: float
    attempt: int = 0
    failed: bool = False
    response: Any = None


class OpenAIMultiClient:
    def __init__(self,
                 concurrency: int = 10,
                 max_retries: int = 10,
                 retry_multiplier: float = 1,
                 retry_max: float = 60,
                 endpoint: str | None = None,
                 data_template: dict | None = None,
                 metadata_template: dict | None = None,
                 custom_api=None):
        self._endpoint = endpoint
        self._data_template = data_template or {}
        self._metadata_template = metadata_template or {}
        self._max_retries = max_retries
        self._retry_multiplier = retry_multiplier
        self._retry_max = retry_max
        self._concurrency = concurrency
        self._loop = asyncio.new_event_loop()
        self._in_queue = AioJoinableQueue(maxsize=concurrency)
        self.out_queue = AioQueue(maxsize=concurrency)
        self._event_loop_thread = Thread(target=self._run_event_loop)
        self._event_loop_thread.start()
        self._mock_api = custom_api
        for i in range(concurrency):
            asyncio.run_coroutine_threadsafe(self._worker(i), self._loop)

    def _run_event_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _process_payload(self, payload: Payload) -> Payload:
        logger.debug(f"Processing {payload}")
        if self._mock_api:
            payload.response = await self._mock_api(payload)
        elif payload.endpoint == "completions":
            payload.response = await openai.Completion.create(**payload.data)
        elif payload.endpoint == "chat.completions" or payload.endpoint == "chat":
            payload.response = await openai.ChatCompletion.create(**payload.data)
        elif payload.endpoint == "embeddings":
            payload.response = await openai.Embedding.create(**payload.data)
        elif payload.endpoint == "edits":
            payload.response = await openai.Edit.create(**payload.data)
        elif payload.endpoint == "audio":
            payload.response = await openai.Audio.create(**payload.data)
        elif payload.endpoint == "images":
            payload.response = await openai.Image.create(**payload.data)
        elif payload.endpoint == "fine-tunes":
            payload.response = await openai.FineTune.create(**payload.data)
        else:
            raise ValueError(f"Unknown endpoint {payload.endpoint}")
        logger.debug(f"Processed {payload}")
        return payload

    async def _worker(self, i):
        while True:
            payload = await self._in_queue.coro_get()

            if payload is None:
                logger.debug(f"Exiting worker {i}")
                self._in_queue.task_done()
                break

            try:
                async for attempt in AsyncRetrying(
                        wait=wait_random_exponential(multiplier=payload.retry_multiplier, max=payload.retry_max),
                        stop=stop_after_attempt(payload.max_retries)):
                    with attempt:
                        try:
                            payload.attempt = attempt.retry_state.attempt_number
                            payload = await self._process_payload(payload)
                            await self.out_queue.coro_put(payload)
                            self._in_queue.task_done()
                        except Exception:
                            logger.exception(f"Error processing {payload}")
                            raise
            except RetryError:
                payload.failed = True
                logger.error(f"Failed to process {payload}")
                await self.out_queue.coro_put(payload)
                self._in_queue.task_done()

    def close(self):
        try:
            for i in range(self._concurrency):
                self._in_queue.put(None)
            self._in_queue.join()
            self.out_queue.put(None)
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._event_loop_thread.join()
        except Exception as e:
            logger.error(f"Error closing: {e}")

    def __iter__(self):
        return self

    def __next__(self):
        out = self.out_queue.get()
        if out is None:
            raise StopIteration
        return out

    def put(self,
            data: dict,
            endpoint: str | None = None,
            *,
            metadata: dict | None = None,
            max_retries: int | None = None,
            retry_multiplier: float | None = None,
            retry_max: float | None = None):
        payload = Payload(
            endpoint=endpoint or self._endpoint,
            data={**self._data_template, **data},
            metadata={**self._metadata_template, **(metadata or {})},
            max_retries=max_retries or self._max_retries,
            retry_multiplier=retry_multiplier or self._retry_multiplier,
            retry_max=retry_max or self._retry_max
        )
        self._in_queue.put(payload)


class OpenAIMultiOrderedClient(OpenAIMultiClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._put_counter = 0
        self._get_counter = 0
        self._get_cache = {}
        self._stopped = False

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            if self._stopped:
                out = None
            else:
                out = self.out_queue.get()
            if out is None:
                self._stopped = True
                if self._get_counter == self._put_counter:
                    raise StopIteration
                else:
                    out = self._get_cache[self._get_counter]
                    del self._get_cache[self._get_counter]
                    self._get_counter += 1
                    return out

            data_counter = out.metadata.get("_put_counter")
            if data_counter == self._get_counter:
                self._get_counter += 1
                return out
            self._get_cache[data_counter] = out
            if self._get_counter in self._get_cache:
                out = self._get_cache[self._get_counter]
                del self._get_cache[self._get_counter]
                self._get_counter += 1
                return out

    def put(self, *args, metadata=None, **kwargs):
        metadata = metadata or {}
        metadata["_put_counter"] = self._put_counter
        self._put_counter += 1
        super().put(*args, metadata=metadata, **kwargs)