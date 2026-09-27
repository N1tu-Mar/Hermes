import pytest
from fastapi.testclient import TestClient

from app import demo
from app.api import create_app
from app.cache import Cache
from app.campaigns import CampaignService
from app.gmail import GmailDrafts
from app.storage import CampaignStore
from tests.support import FakeGmailService


def make_service(root, gmail=None):
    store = CampaignStore(root)
    cache = Cache(root / "cache.sqlite3")
    return CampaignService(store, cache, demo.DemoModel(cache), demo.demo_fetcher(cache), gmail)


@pytest.fixture
def env(tmp_path):
    fake = FakeGmailService()
    svc = make_service(tmp_path / "data", GmailDrafts(fake))
    with TestClient(create_app(service=svc, token="t"), headers={"x-app-token": "t"}) as client:
        yield client, svc, fake
