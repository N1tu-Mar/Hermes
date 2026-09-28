"""OpenAI Responses API adapter with per-campaign budget and usage accounting.

One client per process. Stable instructions go first so prompt caching can
reuse the prefix; per-person content goes last.
"""

import asyncio
import json
import os


class BudgetExceeded(Exception):
    pass


class ModelError(Exception):
    pass


class OpenAIModel:
    demo = False

    def __init__(self, cache, api_key=None, model=None):
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key or os.environ["OPENAI_API_KEY"], timeout=90, max_retries=0)
        self.model = model or os.environ.get("OPENAI_MODEL") or "gpt-5.6-terra"
        self.cache = cache
        self.sem = asyncio.Semaphore(2)  # shared by both workers: one account, one budget
        self._budget_lock = asyncio.Lock()

    async def _reserve_attempt(self, campaign_id):
        """Atomically charge one unit before an outbound provider attempt."""
        async with self._budget_lock:
            u = self.cache.usage(campaign_id)
            if u["api_calls"] >= u["budget"]:
                raise BudgetExceeded(f"campaign API budget of {u['budget']} calls used")
            self.cache.bump_usage(campaign_id, api_calls=1)

    async def structured(self, campaign_id, instructions, user_input, schema_name, schema, web_search=False):
        """Returns (parsed_json, cited_urls). Retries transient errors with backoff."""
        kwargs = dict(
            model=self.model,
            instructions=instructions,
            input=user_input,
            text={"format": {"type": "json_schema", "name": schema_name, "schema": schema, "strict": True}},
        )
        if web_search:
            kwargs["tools"] = [{"type": "web_search"}]
            kwargs["include"] = ["web_search_call.action.sources"]
        delay = 2
        for attempt in range(3):
            try:
                async with self.sem:
                    await self._reserve_attempt(campaign_id)
                    resp = await self.client.responses.create(**kwargs)
                break
            except asyncio.CancelledError:
                raise
            except BudgetExceeded:
                raise
            except Exception as e:  # openai.APIError subclasses; keep message short
                status = getattr(e, "status_code", None)
                if attempt == 2 or (status and status < 500 and status != 429):
                    raise ModelError(f"{type(e).__name__}: {str(e)[:200]}") from e
                await asyncio.sleep(delay)
                delay *= 3
        usage = getattr(resp, "usage", None)
        self.cache.bump_usage(
            campaign_id,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
        )
        try:
            data = json.loads(resp.output_text)
        except (json.JSONDecodeError, TypeError) as e:
            raise ModelError(f"unparseable model output: {e}") from e
        return data, _cited_urls(resp)


def _cited_urls(resp):
    urls = set()
    for item in getattr(resp, "output", []) or []:
        for part in getattr(item, "content", None) or []:
            for ann in getattr(part, "annotations", None) or []:
                if getattr(ann, "url", None):
                    urls.add(ann.url)
        action = getattr(item, "action", None)
        for src in getattr(action, "sources", None) or []:
            if getattr(src, "url", None):
                urls.add(src.url)
    return urls
