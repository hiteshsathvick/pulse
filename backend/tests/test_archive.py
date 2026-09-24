"""Phase 24: the raw event archive's tenant-partitioned layout and GDPR subject
erasure (pulse/archive.py). Real MinIO, no other store -- every test uses its own
random org/project UUIDs so it only ever looks at its own prefix."""

import json
import uuid
from datetime import UTC, datetime

from pulse import archive
from pulse.repositories import object_storage


def _entry(org: uuid.UUID, project: uuid.UUID, *, user: str = "", anon: str = "") -> dict[str, str]:
    now = datetime.now(UTC).isoformat()
    return {
        "org_id": str(org),
        "project_id": str(project),
        "event_id": str(uuid.uuid4()),
        "event_name": "button clicked",
        "user_id": user,
        "anonymous_id": anon,
        "timestamp": now,
        "received_at": now,
        "properties": "{}",
    }


async def _objects_for(org: uuid.UUID, project: uuid.UUID) -> dict[str, list[dict[str, str]]]:
    keys = await object_storage.list_keys(archive.tenant_prefix(org, project))
    return {key: json.loads(await object_storage.get_bytes(key)) for key in keys}


async def test_a_batch_is_split_into_one_object_per_tenant() -> None:
    org_a, project_a = uuid.uuid4(), uuid.uuid4()
    org_b, project_b = uuid.uuid4(), uuid.uuid4()
    batch = uuid.uuid4()

    await archive.write_batch(
        batch,
        [
            _entry(org_a, project_a, user="alice"),
            _entry(org_b, project_b, user="alice"),
            _entry(org_a, project_a, user="bob"),
        ],
    )

    a_objects = await _objects_for(org_a, project_a)
    b_objects = await _objects_for(org_b, project_b)
    assert len(a_objects) == 1 and len(b_objects) == 1
    (a_key,) = a_objects
    assert a_key.endswith(f"{batch}.json")
    assert [e["user_id"] for e in a_objects[a_key]] == ["alice", "bob"]
    assert [e["org_id"] for e in next(iter(b_objects.values()))] == [str(org_b)]


async def test_entries_with_unparseable_tenant_fields_cannot_choose_their_path() -> None:
    batch = uuid.uuid4()
    good_org, good_project = uuid.uuid4(), uuid.uuid4()
    hostile = _entry(good_org, good_project)
    hostile["org_id"] = "../../../etc"
    missing = _entry(good_org, good_project)
    del missing["project_id"]

    await archive.write_batch(batch, [hostile, missing, _entry(good_org, good_project)])

    unattributed = [
        key
        for key in await object_storage.list_keys("raw/_unattributed/")
        if key.endswith(f"{batch}.json")
    ]
    assert len(unattributed) == 1
    assert len(json.loads(await object_storage.get_bytes(unattributed[0]))) == 2
    assert all(".." not in key for key in unattributed)
    # The well-formed entry still went to its own tenant.
    assert len(await _objects_for(good_org, good_project)) == 1


async def test_erasing_a_subject_keeps_everyone_elses_entries() -> None:
    org, project = uuid.uuid4(), uuid.uuid4()
    await archive.write_batch(
        uuid.uuid4(),
        [_entry(org, project, user="alice"), _entry(org, project, user="bob")],
    )

    report = await archive.erase_subject(org, project, user_id="alice")

    assert (report.entries_removed, report.objects_rewritten, report.objects_deleted) == (1, 1, 0)
    remaining = [e for entries in (await _objects_for(org, project)).values() for e in entries]
    assert [e["user_id"] for e in remaining] == ["bob"]


async def test_an_object_holding_only_the_subject_is_deleted_outright() -> None:
    org, project = uuid.uuid4(), uuid.uuid4()
    await archive.write_batch(uuid.uuid4(), [_entry(org, project, user="alice")])

    report = await archive.erase_subject(org, project, user_id="alice")

    assert (report.entries_removed, report.objects_deleted) == (1, 1)
    assert await _objects_for(org, project) == {}


async def test_erasure_matches_the_anonymous_id_too() -> None:
    org, project = uuid.uuid4(), uuid.uuid4()
    anon = str(uuid.uuid4())
    await archive.write_batch(
        uuid.uuid4(), [_entry(org, project, anon=anon), _entry(org, project, user="carol")]
    )

    report = await archive.erase_subject(org, project, anonymous_id=anon)

    assert report.entries_removed == 1
    remaining = [e for entries in (await _objects_for(org, project)).values() for e in entries]
    assert [e["user_id"] for e in remaining] == ["carol"]


async def test_erasure_never_touches_another_project_with_the_same_subject_id() -> None:
    org, project_a, project_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await archive.write_batch(
        uuid.uuid4(),
        [_entry(org, project_a, user="shared"), _entry(org, project_b, user="shared")],
    )

    await archive.erase_subject(org, project_a, user_id="shared")

    assert await _objects_for(org, project_a) == {}
    assert len(await _objects_for(org, project_b)) == 1


async def test_a_legacy_tenant_mixed_object_is_cleaned_without_losing_other_tenants() -> None:
    """Objects written before Phase 24 (`raw/YYYY/MM/DD/<batch>.json`) mix every
    tenant's events. Erasure must clean the subject from them and leave the other
    tenants' entries -- and the same subject id in another tenant -- intact."""
    org_a, project_a = uuid.uuid4(), uuid.uuid4()
    org_b, project_b = uuid.uuid4(), uuid.uuid4()
    key = f"raw/{datetime.now(UTC):%Y/%m/%d}/{uuid.uuid4()}.json"
    entries = [
        _entry(org_a, project_a, user="alice"),
        _entry(org_b, project_b, user="alice"),
        _entry(org_a, project_a, user="bob"),
    ]
    await object_storage.put_object(key, json.dumps(entries).encode())
    try:
        report = await archive.erase_subject(org_a, project_a, user_id="alice")

        assert report.entries_removed == 1
        left = json.loads(await object_storage.get_bytes(key))
        assert sorted((e["org_id"], e["user_id"]) for e in left) == sorted(
            [(str(org_b), "alice"), (str(org_a), "bob")]
        )
    finally:
        await object_storage.remove_object(key)


async def test_an_unreadable_object_is_reported_not_silently_skipped() -> None:
    org, project = uuid.uuid4(), uuid.uuid4()
    await archive.write_batch(uuid.uuid4(), [_entry(org, project, user="alice")])
    corrupt = f"{archive.tenant_prefix(org, project)}2026/01/01/{uuid.uuid4()}.json"
    await object_storage.put_object(corrupt, b"this is not json")

    report = await archive.erase_subject(org, project, user_id="alice")

    # The readable object was still cleaned; the corrupt one is surfaced.
    assert report.unreadable == 1
    assert report.entries_removed == 1
