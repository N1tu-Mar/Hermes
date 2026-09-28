"""Bounded job admission and conservative provider-call budgeting."""

import asyncio
from types import SimpleNamespace

import pytest

from app.jobs import Coordinator, QueueSaturated
from app.openai_client import BudgetExceeded, ModelError, OpenAIModel


class FakeCache:
    def __init__(self, budget=10):
        self.jobs = []
        self.interrupted = False
        self.counters = {
            "api_calls": 0,
            "budget": budget,
            "input_tokens": 0,
            "output_tokens": 0,
        }

    def put_job(self, job_id, campaign_id, kind, candidate_id, status, error=None):
        self.jobs.append((job_id, campaign_id, kind, candidate_id, status, error))

    def mark_interrupted(self):
        self.interrupted = True

    def usage(self, campaign_id):
        return dict(self.counters)

    def bump_usage(self, campaign_id, **increments):
        for key, value in increments.items():
            self.counters[key] += value


class FakeResponses:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = 0
        self.called = asyncio.Event()
        self.release = None

    async def create(self, **kwargs):
        self.calls += 1
        self.called.set()
        if self.release is not None:
            await self.release.wait()
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class ProviderError(Exception):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


def response(text="{}", input_tokens=3, output_tokens=2):
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    return SimpleNamespace(output_text=text, output=[], usage=usage)


def model(cache, responses):
    instance = object.__new__(OpenAIModel)
    instance.client = SimpleNamespace(responses=responses)
    instance.model = "test-model"
    instance.cache = cache
    instance.sem = asyncio.Semaphore(2)
    instance._budget_lock = asyncio.Lock()
    return instance


def test_saturated_queue_rejects_caller_without_orphan_row():
    async def run():
        cache = FakeCache()
        coordinator = Coordinator(cache, {"research": None}, research_workers=0, queue_size=1)
        coordinator.start()
        try:
            accepted = await asyncio.to_thread(coordinator.submit, "campaign", "research", "first")
            with pytest.raises(QueueSaturated, match="research job queue is saturated"):
                await asyncio.to_thread(coordinator.submit, "campaign", "research", "rejected")

            assert coordinator.depths()["research"] == 1
            assert [(row[0], row[3], row[4]) for row in cache.jobs] == [(accepted, "first", "queued")]
        finally:
            await coordinator.close()

    asyncio.run(run())


def test_budget_one_allows_exactly_one_concurrent_provider_request():
    async def run():
        cache = FakeCache(budget=1)
        provider = FakeResponses([response()])
        provider.release = asyncio.Event()
        api = model(cache, provider)
        args = ("campaign", "instructions", "input", "schema", {"type": "object"})

        calls = [asyncio.create_task(api.structured(*args)) for _ in range(2)]
        await provider.called.wait()
        await asyncio.sleep(0)
        provider.release.set()
        results = await asyncio.gather(*calls, return_exceptions=True)

        assert provider.calls == 1
        assert cache.counters["api_calls"] == 1
        assert sum(isinstance(result, BudgetExceeded) for result in results) == 1
        assert sum(isinstance(result, tuple) for result in results) == 1

    asyncio.run(run())


def test_every_failed_provider_attempt_consumes_budget():
    async def run():
        cache = FakeCache(budget=3)
        provider = FakeResponses([ProviderError("temporary", 500) for _ in range(3)])
        api = model(cache, provider)

        with pytest.raises(ModelError, match="ProviderError: temporary"):
            await api.structured("campaign", "instructions", "input", "schema", {"type": "object"})

        assert provider.calls == 3
        assert cache.counters["api_calls"] == 3

    asyncio.run(run())


def test_non_retryable_billable_failure_consumes_budget():
    async def run():
        cache = FakeCache(budget=5)
        provider = FakeResponses([ProviderError("bad request", 400)])
        api = model(cache, provider)

        with pytest.raises(ModelError, match="ProviderError: bad request"):
            await api.structured("campaign", "instructions", "input", "schema", {"type": "object"})

        assert provider.calls == 1
        assert cache.counters["api_calls"] == 1

    asyncio.run(run())


def test_cancellation_during_retry_backoff_stops_attempts():
    async def run():
        cache = FakeCache(budget=5)
        provider = FakeResponses([ProviderError("temporary", 500)])
        api = model(cache, provider)
        task = asyncio.create_task(api.structured("campaign", "instructions", "input", "schema", {"type": "object"}))

        await provider.called.wait()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert provider.calls == 1
        assert cache.counters["api_calls"] == 1

    asyncio.run(run())
