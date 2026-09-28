"""people/person_links as the single canonical contact model: legacy migration, id collisions,
global-vs-campaign DNC parity, and ambiguous-match reviews. See app/cache.py: _migrate_v3."""
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app import demo
from app.api import create_app
from app.cache import MIGRATIONS, SCHEMA_V1, SCHEMA_V2, Cache
from app.campaigns import CampaignService
from app.storage import CampaignStore
from app.workspace import Workspace
from tests.support import idle, make_campaign, wait

TEXT = "Rutgers/Princeton professors working on computational neurodevelopment who may work with undergraduates"


def boot(root):
    store = CampaignStore(root)
    cache = Cache(root / "cache.sqlite3")
    svc = CampaignService(store, cache, demo.DemoModel(cache), demo.demo_fetcher(cache), None, Workspace(cache, root))
    return TestClient(create_app(service=svc, token="t"), headers={"x-app-token": "t"}), svc


def people_of(client, cid):
    return {c["name"]: c for c in client.get(f"/api/campaigns/{cid}").json()["candidates"]}


def _legacy_db(path):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA_V1)
    db.executescript(SCHEMA_V2)
    return db


def _insert_contact(db, **fields):
    now = time.time()
    row = {"name": "", "organization": None, "role": None, "email": None, "profile_url": None, "name_key": "",
          "notes": "", "tags": "[]", "relationship": "new", "do_not_contact": 0, "dnc_reason": None,
          "last_contacted_at": None, "owner": None, "source": "campaign", "created_at": now, "updated_at": now,
          **fields}
    cols = list(row)
    cur = db.execute(f"INSERT INTO contacts ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     [row[c] for c in cols])
    return cur.lastrowid


def _insert_person(db, **fields):
    now = time.time()
    row = {"name": "", "organization": None, "role": None, "email": None, "profile_url": None, "name_key": "",
          "notes": "", "tags": "[]", "relationship": "new", "do_not_contact": 0, "dnc_reason": None,
          "last_contacted_at": None, "owner": None, "source": "manual", "created_at": now, "updated_at": now,
          **fields}
    cols = list(row)
    cur = db.execute(f"INSERT INTO people ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     [row[c] for c in cols])
    return cur.lastrowid


# ------------------------------------------------------------------ legacy migration
def test_legacy_fixture_migrates_without_loss_or_duplication(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    db = _legacy_db(path)

    # A legacy DNC contact never seen by the ledger: notes, tags, DNC, and a campaign link to preserve.
    dnc_id = _insert_contact(db, name="Jane Doe", organization="Rutgers", role="Prof",
                             email="jane@rutgers.example.edu", profile_url="https://rutgers.example.edu/jane",
                             name_key="jane doe|rutgers", notes="Asked to stop emailing.", tags='["alumni"]',
                             do_not_contact=1, dnc_reason="asked to stop")
    db.execute("INSERT INTO contact_links VALUES (?,?,?,?)", ("cmp_20260101_aaaaaaaa", "cand_1", dnc_id, time.time()))
    db.execute("INSERT INTO interactions (contact_id,campaign_id,candidate_id,kind,detail,meta,at) "
              "VALUES (?,?,?,?,?,?,?)", (dnc_id, "cmp_20260101_aaaaaaaa", "cand_1", "note", "legacy note", None,
                                         time.time()))

    # A legacy contact that should merge into an existing ledger person by verified email: extra
    # notes/tags/relationship must survive onto the merged row, never overwrite what's already there.
    person_id = _insert_person(db, name="Ada Park", organization="NYU", email="ada@nyu.example.edu",
                               name_key="ada park|nyu", notes="Met at HackRU.", tags='["ml"]',
                               relationship="contacted")
    dup_id = _insert_contact(db, name="Ada Park", organization="NYU", email="ada@nyu.example.edu",
                             name_key="ada park|nyu", notes="Interested in mentoring.", tags='["mentor"]',
                             relationship="replied")
    db.commit()
    db.close()

    c = Cache(path)
    people = {r["name"]: r for r in c.q("SELECT * FROM people")}
    assert set(people) == {"Jane Doe", "Ada Park"}  # no duplicate row created for the merge

    jane = people["Jane Doe"]
    assert jane["do_not_contact"] and jane["dnc_reason"] == "asked to stop"
    assert jane["tags"] == '["alumni"]' and "stop emailing" in jane["notes"]
    links = c.q("SELECT * FROM person_links WHERE campaign_id=?", ("cmp_20260101_aaaaaaaa",))
    assert links and links[0]["contact_id"] == jane["id"]
    interactions = c.q("SELECT * FROM interactions WHERE campaign_id=?", ("cmp_20260101_aaaaaaaa",))
    assert len(interactions) == 1 and interactions[0]["contact_id"] == jane["id"]  # not lost, not duplicated

    ada = people["Ada Park"]
    assert ada["id"] == person_id  # merged into the existing ledger row, not the legacy row's id
    assert ada["relationship"] == "replied"  # merge keeps the more advanced relationship
    assert set(json.loads(ada["tags"])) == {"ml", "mentor"}  # unioned, nothing dropped
    assert "HackRU" in ada["notes"] and "mentoring" in ada["notes"]  # both notes kept

    # the legacy store is retired (emptied, not dropped: app/ops.py still queries it) so nothing double-counts
    assert c.q("SELECT COUNT(*) n FROM contacts")[0]["n"] == 0
    assert c.q("SELECT COUNT(*) n FROM contact_links")[0]["n"] == 0


def test_reopening_migrated_database_is_idempotent(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    db = _legacy_db(path)
    _insert_contact(db, name="Solo Contact", name_key="solo contact|")
    db.commit()
    db.close()

    first = Cache(path)
    before = first.q("SELECT * FROM people ORDER BY id")
    assert first.q("PRAGMA user_version")[0]["user_version"] == len(MIGRATIONS)

    second = Cache(path)
    after = second.q("SELECT * FROM people ORDER BY id")
    assert before == after  # no re-merge, no new rows, no changed timestamps
    assert second.q("PRAGMA user_version")[0]["user_version"] == len(MIGRATIONS)


# ------------------------------------------------------------------ id collisions
def test_colliding_legacy_and_canonical_ids_are_kept_separate(tmp_path):
    """A legacy contacts.id can coincidentally equal an unrelated people.id (independent
    AUTOINCREMENT sequences). Migrated data must never attribute one person's history to the other."""
    path = tmp_path / "legacy.sqlite3"
    db = _legacy_db(path)

    # Both get id 1 in their own table: a guaranteed collision once merged into one id space.
    person_id = _insert_person(db, name="Ada Park", organization="NYU", email="ada@nyu.example.edu",
                               name_key="ada park|nyu")
    contact_id = _insert_contact(db, name="Jane Doe", organization="Rutgers", email="jane@rutgers.example.edu",
                                 name_key="jane doe|rutgers")
    assert person_id == contact_id == 1

    db.execute("INSERT INTO contact_links VALUES (?,?,?,?)", ("cmp_20260101_bbbbbbbb", "cand_9", contact_id,
                                                               time.time()))
    # A campaign-scoped legacy interaction (contact_id=1 means Jane, via contacts) ...
    db.execute("INSERT INTO interactions (contact_id,campaign_id,candidate_id,kind,detail,meta,at) "
              "VALUES (?,?,?,?,?,?,?)", (contact_id, "cmp_20260101_bbbbbbbb", "cand_9", "note", "for jane", None,
                                         time.time()))
    # ... and a global, contact-API-style interaction (contact_id=1 means Ada, via people).
    db.execute("INSERT INTO interactions (contact_id,campaign_id,candidate_id,kind,detail,meta,at) "
              "VALUES (?,?,?,?,?,?,?)", (person_id, None, None, "note", "for ada", None, time.time()))
    db.commit()
    db.close()

    c = Cache(path)
    ada_id = c.q("SELECT id FROM people WHERE name='Ada Park'")[0]["id"]
    jane_id = c.q("SELECT id FROM people WHERE name='Jane Doe'")[0]["id"]
    assert ada_id != jane_id

    for_jane = c.q("SELECT * FROM interactions WHERE detail='for jane'")[0]
    for_ada = c.q("SELECT * FROM interactions WHERE detail='for ada'")[0]
    assert for_jane["contact_id"] == jane_id
    assert for_ada["contact_id"] == ada_id  # never remapped: it was already correctly Ada's

    link = c.q("SELECT * FROM person_links WHERE campaign_id=?", ("cmp_20260101_bbbbbbbb",))[0]
    assert link["contact_id"] == jane_id


# ------------------------------------------------------------------ ambiguous legacy matches
def test_ambiguous_legacy_contact_opens_a_review_instead_of_guessing(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    db = _legacy_db(path)
    a = _insert_person(db, name="Avery Lin", organization="Rutgers", email="a1@example.com",
                       name_key="avery lin|rutgers")
    b = _insert_person(db, name="Avery Lin", organization="Rutgers", email="a2@example.com",
                       name_key="avery lin|rutgers")
    # same name+org as both existing people, no email/url to disambiguate: must not silently pick one.
    _insert_contact(db, name="Avery Lin", organization="Rutgers", name_key="avery lin|rutgers",
                    do_not_contact=1, dnc_reason="legacy dnc")
    db.commit()
    db.close()

    c = Cache(path)
    names = [r["name"] for r in c.q("SELECT name FROM people")]
    assert names.count("Avery Lin") == 3  # a, b, and a new row for the ambiguous legacy contact
    reviews = c.q("SELECT * FROM person_reviews WHERE status='open'")
    assert len(reviews) == 1
    options = json.loads(reviews[0]["options"])
    assert {a, b} <= set(options) and len(options) == 3  # a, b, plus the new unmerged row
    # the DNC flag on the migrated-but-unresolved contact is preserved, not lost or silently applied to a/b
    new_row_id = (set(options) - {a, b}).pop()
    new_row = c.q("SELECT * FROM people WHERE id=?", (new_row_id,))[0]
    assert new_row["do_not_contact"] and new_row["dnc_reason"] == "legacy dnc"
    assert not c.q("SELECT * FROM people WHERE id=?", (a,))[0]["do_not_contact"]
    assert not c.q("SELECT * FROM people WHERE id=?", (b,))[0]["do_not_contact"]


# ------------------------------------------------------------------ DNC parity with global Contacts
def test_candidate_dnc_immediately_appears_in_global_contacts(tmp_path):
    client, svc = boot(tmp_path / "data")
    with client:
        cid = make_campaign(client, TEXT)
        client.post(f"/api/campaigns/{cid}/discover")
        wait(client, cid, lambda p: p["candidates"].get("discovered") and idle(p))
        cand = next(iter(people_of(client, cid).values()))
        assert not any(c["do_not_contact"] for c in client.get("/api/contacts").json())

        r = client.put(f"/api/campaigns/{cid}/candidates/{cand['candidate_id']}/do-not-contact",
                       json={"do_not_contact": True, "reason": "asked not to be emailed"})
        assert r.status_code == 200

        contact_id = people_of(client, cid)[cand["name"]]["contact_id"]
        contact = next(c for c in client.get("/api/contacts").json() if c["id"] == contact_id)
        assert contact["do_not_contact"] is True and contact["dnc_reason"] == "asked not to be emailed"
