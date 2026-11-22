from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

from app import chat_store, plans, userstore


def _local_only(monkeypatch):
    disk = {}
    monkeypatch.setattr(userstore, "_primary", lambda: "firestore")
    monkeypatch.setattr(userstore.fstore, "enabled", lambda: False)
    monkeypatch.setattr(userstore.cfstore, "enabled", lambda: False)
    monkeypatch.setattr(
        userstore,
        "_read_local_doc",
        lambda uid, collection, doc_id: disk.get((uid, collection, doc_id)),
    )
    monkeypatch.setattr(
        userstore,
        "_write_local",
        lambda uid, collection, doc_id, data: disk.__setitem__(
            (uid, collection, doc_id), dict(data)
        ),
    )
    monkeypatch.setattr(userstore, "_cleanup_spill_blob", lambda *_args: None)
    return disk


def test_set_doc_persists_when_cloud_backends_are_disabled(monkeypatch):
    _local_only(monkeypatch)
    userstore._cache.clear()
    userstore._neg_cache.clear()

    userstore.set_doc("user", "chats", "new-thread", {"id": "new-thread"})

    userstore._cache.clear()
    assert userstore.get_doc("user", "chats", "new-thread") == {
        "id": "new-thread"
    }
    assert userstore.storage_is_healthy() is True


def test_new_chat_and_named_render_plan_survive_local_only_mode(monkeypatch):
    _local_only(monkeypatch)
    userstore._cache.clear()
    userstore._neg_cache.clear()
    chat_store._index_cache.clear()
    chat_store._index_dirty.clear()

    thread = chat_store.create_thread("user", title="New chat")
    created = plans.create_plan(
        "user",
        name="Swagbucks",
        render_pairs=[{"sourceId": "video-1", "caption": "New hook"}],
    )

    userstore._cache.clear()
    chat_store._index_cache.clear()
    assert chat_store.get_thread("user", thread["id"])["id"] == thread["id"]
    assert plans.get_plan("user", created["id"])["name"] == "Swagbucks"


def test_provision_profile_commit_keeps_newer_deactivation(monkeypatch):
    disk = _local_only(monkeypatch)
    key = ("user", "kv", "provision_profiles")
    disk[key] = {"byId": {
        "249": {"updatedAt": "2026-09-29T19:03:42+00:00", "bcDeactivateStatus": "done"},
    }}
    stale_write = {"byId": {
        "249": {"updatedAt": "2026-09-29T18:53:49+00:00", "bcDeactivateStatus": None},
        "251": {"updatedAt": "2026-09-29T19:48:00+00:00", "status": "logging_in"},
    }}

    monkeypatch.setattr(userstore, "local_path", lambda *_: Path("profiles.json"))
    monkeypatch.setattr(userstore, "FileLock", lambda *_args, **_kwargs: nullcontext())
    result = userstore._commit_local_mirror("user", "kv", "provision_profiles", stale_write)

    assert result["byId"]["249"]["bcDeactivateStatus"] == "done"
    assert result["byId"]["251"]["status"] == "logging_in"


def test_provision_step_commit_keeps_completed_worker_result(monkeypatch):
    disk = _local_only(monkeypatch)
    disk[("user", "kv", "provision_steps")] = {"byId": {
        "a63": {"updatedAt": "2026-09-29T19:03:42+00:00", "status": "done", "result": {"ok": True}},
    }}
    stale_write = {"byId": {
        "a63": {"updatedAt": "2026-09-29T18:53:49+00:00", "status": "pending"},
        "warmup": {"updatedAt": "2026-09-29T20:00:00+00:00", "status": "pending"},
    }}
    monkeypatch.setattr(userstore, "local_path", lambda *_: Path("steps.json"))
    monkeypatch.setattr(userstore, "FileLock", lambda *_args, **_kwargs: nullcontext())
    result = userstore._commit_local_mirror("user", "kv", "provision_steps", stale_write)
    assert result["byId"]["a63"]["status"] == "done"
    assert result["byId"]["warmup"]["status"] == "pending"
