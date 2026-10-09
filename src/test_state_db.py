"""Tests and diagnostics for the durable SQLite state layer."""
from __future__ import annotations

import tempfile
from pathlib import Path

from state_db import StateDB


def test_proxy_assignment_and_replacement() -> None:
    with tempfile.TemporaryDirectory() as d:
        db = StateDB(Path(d) / "state.sqlite3")
        db.sync_proxies(["one:1:u:p", "two:2:u:p"])
        assert db.assign_proxy("alice") == "one:1:u:p"
        assert db.assign_proxy("alice") == "one:1:u:p"
        assert db.assign_proxy("bob") == "two:2:u:p"
        assert db.replace_proxy("alice", "one:1:u:p", "connect timeout") is None
        db.close()


def test_page_is_durable_and_idempotent() -> None:
    with tempfile.TemporaryDirectory() as d:
        db = StateDB(Path(d) / "state.sqlite3")
        page = {"items": [{"code": "abc", "user": {"username": "alice"}, "video_versions": []}]}
        state = {
            "cursor_key": "after",
            "end_cursor": "next",
            "page_num": 1,
            "total_items": 1,
            "total_videos": 0,
            "total_accounts": 1,
            "status": "running",
        }
        db.save_page("#tag", page, 1, state)
        db.save_page("#tag", page, 1, state)
        assert db.load_tag("#tag")["page_num"] == 1
        con = db.connection()
        n_profiles = con.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
        n_links = con.execute("SELECT COUNT(*) FROM tag_profiles").fetchone()[0]
        assert n_profiles == 1 and n_links == 1
        db.close()


if __name__ == "__main__":
    test_proxy_assignment_and_replacement()
    test_page_is_durable_and_idempotent()
    print("state tests: OK")
