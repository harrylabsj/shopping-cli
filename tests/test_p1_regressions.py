import json
import os
import runpy
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from shopping_cli import cli
from shopping_cli.agents import merchant_daemon
from shopping_cli.api.app import handle_request
from shopping_cli.api.auth import require_admin_token
from shopping_cli.api.handlers import human_review as human_review_handler
from shopping_cli.core.catalog import create_merchant, create_product, merchant_summary, search_merchants, search_products
from shopping_cli.core.conversations import add_flag, append_message, conversation_summary, ensure_conversation, require_conversation
from shopping_cli.core.errors import AuthError, ConflictError
from shopping_cli.core.policies import create_policy, search_policies
from shopping_cli.core.tokens import token_digest
from shopping_cli.db.session import db_session, open_connection
from shopping_cli.services import agents as agent_service
from shopping_cli.services import conversations as conversation_service
from shopping_cli.services import human_review as human_review_service
from shopping_cli.services import tokens as token_service


def positive_seconds(value, _field):
    return int(value) if value not in (None, "") else None


class P1RegressionTest(unittest.TestCase):
    def seed_merchant(self, db_file: Path, merchant_id: str = "seller-a") -> None:
        with db_session(db_file) as conn:
            create_merchant(
                conn,
                merchant_id=merchant_id,
                name="西湖茶庄",
                city="杭州",
                automation_boundaries="不议价；库存变化必须转人工确认。",
            )

    def test_generic_cli_api_serve_cannot_bypass_production_preflight(self):
        args = SimpleNamespace(db="/tmp/unused.sqlite", data=None, host="127.0.0.1", port=8765)
        with patch.dict(
            os.environ,
            {
                "SHOPPING_DEPLOYMENT_PROFILE": "production",
                "SHOPPING_ADMIN_TOKEN": "a",
                "SHOPPING_BUYER_BOOTSTRAP_TOKEN": "b",
            },
            clear=False,
        ):
            with self.assertRaises(SystemExit) as raised:
                cli.cmd_api_serve(args)
        self.assertIn("at least 32", str(raised.exception))

    PRODUCTION_ENTRY_SECRET_CASES = [
        ("missing admin token", "", "b" * 40, False),
        ("missing buyer bootstrap token", "a" * 40, "", False),
        ("placeholder admin token", "change-me-admin-token-0123456789abcdef", "b" * 40, False),
        ("placeholder buyer bootstrap token", "a" * 40, "replace-with-buyer-token-0123456789ab", False),
        ("short admin token", "short-secret", "b" * 40, False),
        ("short buyer bootstrap token", "a" * 40, "tiny", False),
        ("strong secrets without channel tokens", "a" * 40, "b" * 40, True),
    ]

    def production_entry_env(self, admin_token: str, buyer_token: str) -> dict[str, str]:
        # Channel ingress stays optional in production: no channel token is set
        # here, matching the Compose/docs contract that it fails closed instead.
        return {
            "SHOPPING_DEPLOYMENT_PROFILE": "production",
            "SHOPPING_ADMIN_TOKEN": admin_token,
            "SHOPPING_BUYER_BOOTSTRAP_TOKEN": buyer_token,
            "SHOPPING_DATABASE_URL": "",
            "SHOPPING_CHANNEL_TOKEN": "",
            "SHOPPING_CHANNEL_TOKENS": "",
        }

    @staticmethod
    def fake_uvicorn(serve_calls: list) -> SimpleNamespace:
        return SimpleNamespace(run=lambda *args, **kwargs: serve_calls.append({"args": args, "kwargs": kwargs}))

    def test_console_server_main_enforces_production_secret_gate_before_serving(self):
        from shopping_cli.api import server as api_server

        with tempfile.TemporaryDirectory() as tmp:
            for label, admin_token, buyer_token, should_serve in self.PRODUCTION_ENTRY_SECRET_CASES:
                with self.subTest(entry="shopping_cli.api.server.main", case=label):
                    serve_calls: list = []
                    with (
                        patch.dict(
                            os.environ,
                            self.production_entry_env(admin_token, buyer_token),
                            clear=False,
                        ),
                        patch.dict(sys.modules, {"uvicorn": self.fake_uvicorn(serve_calls)}),
                    ):
                        if should_serve:
                            api_server.main(["--db", str(Path(tmp) / "api.sqlite")])
                        else:
                            with self.assertRaises(SystemExit) as raised:
                                api_server.main(["--db", str(Path(tmp) / "api.sqlite")])
                            self.assertIn("at least 32", str(raised.exception))
                    self.assertEqual(len(serve_calls), 1 if should_serve else 0)

    def test_script_shopping_api_enforces_production_secret_gate_before_serving(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "shopping_api.py"
        with tempfile.TemporaryDirectory() as tmp:
            for label, admin_token, buyer_token, should_serve in self.PRODUCTION_ENTRY_SECRET_CASES:
                with self.subTest(entry="scripts/shopping_api.py", case=label):
                    serve_calls: list = []
                    argv = ["shopping_api.py", "--db", str(Path(tmp) / "api.sqlite")]
                    with (
                        patch.dict(
                            os.environ,
                            self.production_entry_env(admin_token, buyer_token),
                            clear=False,
                        ),
                        patch.dict(sys.modules, {"uvicorn": self.fake_uvicorn(serve_calls)}),
                        patch.object(sys, "argv", argv),
                    ):
                        if should_serve:
                            runpy.run_path(str(script), run_name="__main__")
                        else:
                            with self.assertRaises(SystemExit) as raised:
                                runpy.run_path(str(script), run_name="__main__")
                            self.assertIn("at least 32", str(raised.exception))
                    self.assertEqual(len(serve_calls), 1 if should_serve else 0)

    def test_authentication_rejects_placeholder_shared_secret(self):
        with patch.dict(os.environ, {"SHOPPING_ADMIN_TOKEN": "change-me-admin"}, clear=False):
            with self.assertRaises(AuthError):
                require_admin_token({"admin_token": "change-me-admin"})

    def test_human_review_is_preserved_and_blocks_close_until_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_merchant(db_file)
            with db_session(db_file) as conn:
                conversation = ensure_conversation(conn, "alice", "seller-a")
                add_flag(conn, conversation["id"], "manual_confirmation")
                append_message(conn, conversation["id"], "buyer", "ask_product", "还有货吗？")
                current = conversation_summary(conn, conversation["id"])
                self.assertEqual(current["status"], "human_required")
                self.assertEqual(current["next_actor"], "merchant_human")
                with self.assertRaises(ConflictError):
                    conversation_service.close_conversation(
                        conn,
                        current,
                        current["id"],
                        sender="buyer",
                    )

    def test_concurrent_reuse_open_returns_one_conversation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_merchant(db_file)
            worker_count = 8
            barrier = threading.Barrier(worker_count)

            def ensure_once():
                conn = open_connection(db_file)
                try:
                    barrier.wait(timeout=5)
                    conn.execute("begin immediate")
                    result = ensure_conversation(conn, "alice", "seller-a", reuse_open=True)
                    conn.commit()
                    return result["id"]
                finally:
                    conn.close()

            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                ids = list(pool.map(lambda _value: ensure_once(), range(worker_count)))

            self.assertEqual(len(set(ids)), 1)
            with db_session(db_file) as conn:
                rows = conn.execute(
                    """
                    select id, reuse_key from conversations
                    where buyer_id = 'alice' and merchant_id = 'seller-a' and status != 'closed'
                    """
                ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], ids[0])
            self.assertEqual(rows[0]["reuse_key"], token_digest("alice\nseller-a\n"))

    def test_reuse_open_false_keeps_independent_open_conversations(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_merchant(db_file)
            with db_session(db_file) as conn:
                first = ensure_conversation(conn, "alice", "seller-a", reuse_open=False)
                second = ensure_conversation(conn, "alice", "seller-a", reuse_open=False)
                rows = conn.execute(
                    "select id, status, reuse_key from conversations where buyer_id = 'alice' order by id"
                ).fetchall()

            self.assertNotEqual(first["id"], second["id"])
            self.assertEqual([row["id"] for row in rows], [first["id"], second["id"]])
            self.assertEqual([row["status"] for row in rows], ["open", "open"])
            self.assertEqual([row["reuse_key"] for row in rows], ["", ""])

    def test_human_required_blocks_plain_buyer_and_merchant_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_merchant(db_file)
            with db_session(db_file) as conn:
                conversation = ensure_conversation(conn, "alice", "seller-a")
                add_flag(conn, conversation["id"], "manual_confirmation")

                append_message(conn, conversation["id"], "buyer", "ask_product", "还有货吗？")
                current = conversation_summary(conn, conversation["id"])
                self.assertEqual(current["status"], "human_required")
                self.assertEqual(current["next_actor"], "merchant_human")

                append_message(conn, conversation["id"], "merchant_agent", "ask_stock", "自动回复不应绕过人工确认。")
                current = conversation_summary(conn, conversation["id"])
                self.assertEqual(current["status"], "human_required")
                self.assertEqual(current["next_actor"], "merchant_human")

    def test_resolved_review_allows_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_merchant(db_file)
            with db_session(db_file) as conn:
                conversation = ensure_conversation(conn, "alice", "seller-a")
                flag = add_flag(conn, conversation["id"], "manual_confirmation")
                current = conversation_summary(conn, conversation["id"])
                with self.assertRaises(ConflictError):
                    conversation_service.close_conversation(conn, current, current["id"], sender="buyer")

                resolved = human_review_service.resolve_review(
                    conn,
                    flag["id"],
                    action="reply",
                    sender="merchant",
                )
                self.assertEqual(resolved, 1)

                current = conversation_summary(conn, conversation["id"])
                closed = conversation_service.close_conversation(conn, current, current["id"], sender="merchant")
                self.assertEqual(closed["status"], "closed")

    def test_concurrent_append_and_close_never_reopen_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_merchant(db_file)
            with db_session(db_file) as conn:
                conversation = ensure_conversation(conn, "alice", "seller-a")
            conversation_id = conversation["id"]
            barrier = threading.Barrier(2)
            outcomes: dict[str, Exception | None] = {}

            def run(name, operation):
                conn = open_connection(db_file)
                try:
                    barrier.wait(timeout=5)
                    conn.execute("begin immediate")
                    try:
                        summary = conversation_summary(conn, conversation_id)
                        operation(conn, summary)
                        conn.commit()
                        outcomes[name] = None
                    except ConflictError as exc:
                        conn.rollback()
                        outcomes[name] = exc
                finally:
                    conn.close()

            def do_append(conn, summary):
                conversation_service.append_conversation_message(
                    conn,
                    summary,
                    conversation_id,
                    sender="buyer",
                    intent="ask_product",
                    text="并发追加消息",
                    structured_payload={},
                )

            def do_close(conn, summary):
                conversation_service.close_conversation(conn, summary, conversation_id, sender="buyer")

            threads = [
                threading.Thread(target=run, args=("append", do_append)),
                threading.Thread(target=run, args=("close", do_close)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())

            append_error = outcomes["append"]
            close_error = outcomes["close"]
            with db_session(db_file) as conn:
                final = conversation_summary(conn, conversation_id)
            texts = [message["text"] for message in final["messages"]]

            self.assertFalse(append_error is not None and close_error is not None)
            if close_error is None:
                self.assertEqual(final["status"], "closed")
            else:
                self.assertNotEqual(final["status"], "closed")
            if append_error is None:
                self.assertIn("并发追加消息", texts)
            else:
                self.assertNotIn("并发追加消息", texts)

    def test_concurrent_double_resolve_has_single_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_merchant(db_file)
            with db_session(db_file) as conn:
                conversation = ensure_conversation(conn, "alice", "seller-a")
                flag = add_flag(conn, conversation["id"], "manual_confirmation")
                merchant_token = token_service.issue_merchant_token(conn, "seller-a")
            review_id = flag["id"]
            payload = {
                "action": "reply",
                "sender": "merchant",
                "text": "已人工确认处理。",
                "merchant_token": merchant_token,
            }
            barrier = threading.Barrier(2)
            outcomes: list[str] = []
            errors: list[Exception] = []

            def resolve_once():
                barrier.wait(timeout=5)
                try:
                    human_review_handler.resolve_human_review_item(db_file, review_id, dict(payload))
                    outcomes.append("ok")
                except (ConflictError, sqlite3.OperationalError) as exc:
                    outcomes.append("lost")
                    errors.append(exc)

            threads = [threading.Thread(target=resolve_once) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())

            self.assertEqual(sorted(outcomes), ["lost", "ok"])
            with db_session(db_file) as conn:
                final = conversation_summary(conn, conversation["id"])
            resolved_flags = [item for item in final["flags"] if item["resolved_at"]]
            self.assertEqual(len(final["flags"]), 1)
            self.assertEqual(len(resolved_flags), 1)
            self.assertEqual(resolved_flags[0]["resolution"], "reply")
            resolution_messages = [
                message
                for message in final["messages"]
                if message["structured_payload"].get("resolution") == "reply"
            ]
            self.assertEqual(len(resolution_messages), 1)
            resolved_audits = [
                event for event in final["audit_events"] if event["event"] == "human_review_resolved"
            ]
            self.assertEqual(len(resolved_audits), 1)
            self.assertEqual(final["status"], "waiting_buyer")

    # ---- P1-02: atomic conversation state machine ----

    def test_add_flag_transitions_status_atomically_without_separate_update(self):
        """add_flag itself transitions the conversation to human_required;
        no separate update_conversation_status call is needed."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_merchant(db_file)
            with db_session(db_file) as conn:
                conv = ensure_conversation(conn, "alice", "seller-a")
                append_message(conn, conv["id"], "buyer", "ask_product", "hello")
                # add_flag must transition status by itself
                flag = add_flag(conn, conv["id"], "suspicious")
                current = conversation_summary(conn, conv["id"])
                self.assertEqual(current["status"], "human_required")
                self.assertEqual(flag["reason"], "suspicious")

    def test_update_conversation_status_rejects_wrong_expected_status(self):
        """Passing an expected_status that doesn't match the current status
        must raise ConflictError."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_merchant(db_file)
            with db_session(db_file) as conn:
                conv = ensure_conversation(conn, "alice", "seller-a")
                # Current status is 'open', but we claim to expect 'human_required'
                with self.assertRaises(ConflictError):
                    human_review_service.update_conversation_status(
                        conn,
                        conv["id"],
                        status="waiting_buyer",
                        next_actor="buyer",
                        sender="merchant",
                        expected_status="human_required",
                    )

    def test_resolve_from_non_human_required_is_rejected(self):
        """Resolving reviews when the conversation is NOT in human_required
        must fail — either because there are no unresolved reviews or the
        status precondition fails."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_merchant(db_file)
            with db_session(db_file) as conn:
                conv = ensure_conversation(conn, "alice", "seller-a")
                # No flag was added, so conversation is 'open'.
                # resolve_all_conversation_reviews should find 0 unresolved.
                resolved = human_review_service.resolve_all_conversation_reviews(
                    conn, conv["id"], action="reply", sender="merchant"
                )
                self.assertEqual(resolved, 0)

    def test_concurrent_add_flag_and_close(self):
        """When one thread adds a flag and another closes the conversation,
        only one should win."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_merchant(db_file)
            with db_session(db_file) as conn:
                conv = ensure_conversation(conn, "alice", "seller-a")
                append_message(conn, conv["id"], "buyer", "ask_product", "hello")
            cid = conv["id"]
            barrier = threading.Barrier(2)
            errors: list[Exception] = []

            def add_flag_worker():
                conn = open_connection(db_file)
                try:
                    barrier.wait(timeout=5)
                    add_flag(conn, cid, "suspicious_content")
                    conn.commit()
                except (ConflictError, sqlite3.OperationalError) as exc:
                    conn.rollback()
                    errors.append(exc)
                finally:
                    conn.close()

            def close_worker():
                conn = open_connection(db_file)
                try:
                    barrier.wait(timeout=5)
                    conv_row = require_conversation(conn, cid)
                    conversation_service.close_conversation(
                        conn, {"status": conv_row["status"]}, cid,
                        sender="merchant",
                    )
                    conn.commit()
                except (ConflictError, sqlite3.OperationalError) as exc:
                    conn.rollback()
                    errors.append(exc)
                finally:
                    conn.close()

            threads = [threading.Thread(target=add_flag_worker), threading.Thread(target=close_worker)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            # Both should succeed in isolation, but concurrently one may lose.
            # At least one must succeed; the final state must be consistent.
            self.assertLessEqual(len(errors), 1)
            with db_session(db_file) as conn:
                final = conversation_summary(conn, cid)
                self.assertIn(final["status"], {"human_required", "closed"})

    def test_resolve_close_rejects_concurrent_flag_toctou(self):
        """When resolve+close transitions to 'closed', a concurrent add_flag
        that snuck in between the resolve and the status update must cause
        the close to be rejected (rowcount 0)."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_merchant(db_file)
            with db_session(db_file) as conn:
                conv = ensure_conversation(conn, "alice", "seller-a")
                flag = add_flag(conn, conv["id"], "review_me")
            cid = conv["id"]
            fid = flag["id"]
            barrier = threading.Barrier(2)
            errors: list[Exception] = []

            def resolve_and_close():
                conn = open_connection(db_file)
                try:
                    # Resolve the original flag
                    human_review_service.resolve_review(
                        conn, fid, action="close", sender="merchant"
                    )
                    barrier.wait(timeout=5)
                    # Try to close — but add_flag_worker may have already
                    # inserted a new flag
                    human_review_service.update_conversation_status(
                        conn,
                        cid,
                        status="closed",
                        next_actor="",
                        sender="merchant",
                        expected_status="human_required",
                        reject_if_unresolved=True,
                    )
                    conn.commit()
                except (ConflictError, sqlite3.OperationalError) as exc:
                    conn.rollback()
                    errors.append(exc)
                finally:
                    conn.close()

            def add_flag_worker():
                conn = open_connection(db_file)
                try:
                    barrier.wait(timeout=5)
                    add_flag(conn, cid, "sneaky_flag")
                    conn.commit()
                except (ConflictError, sqlite3.OperationalError) as exc:
                    conn.rollback()
                    errors.append(exc)
                finally:
                    conn.close()

            threads = [
                threading.Thread(target=resolve_and_close),
                threading.Thread(target=add_flag_worker),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            with db_session(db_file) as conn:
                final = conversation_summary(conn, cid)
                # If close won → status is closed and ALL flags resolved.
                # If flag won → status stays human_required with unresolved flag.
                if final["status"] == "closed":
                    unresolved = conn.execute(
                        "select 1 from moderation_flags where conversation_id = ? and resolved_at = ''",
                        (cid,),
                    ).fetchone()
                    self.assertIsNone(unresolved,
                        "closed conversation must have zero unresolved flags")
                else:
                    self.assertEqual(final["status"], "human_required")

    def test_concurrent_agent_token_rotate_has_one_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_merchant(db_file)
            with db_session(db_file) as conn:
                old_token, _expires = token_service.issue_agent_token(
                    conn,
                    "seller-a",
                    token_service.default_merchant_agent_id("seller-a"),
                    positive_whole_seconds=positive_seconds,
                )
            barrier = threading.Barrier(2)

            def rotate_once():
                conn = open_connection(db_file)
                try:
                    barrier.wait(timeout=5)
                    result = agent_service.rotate_agent_token(
                        conn,
                        "seller-a",
                        token=old_token,
                        positive_whole_seconds=positive_seconds,
                    )
                    conn.commit()
                    return result["agent_token"]
                except ConflictError:
                    conn.rollback()
                    return None
                finally:
                    conn.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                replacements = list(pool.map(lambda _value: rotate_once(), range(2)))

            self.assertEqual(sum(token is not None for token in replacements), 1)
            with db_session(db_file) as conn:
                active = conn.execute(
                    "select count(*) from api_tokens where role = 'agent' and revoked_at = ''"
                ).fetchone()[0]
            self.assertEqual(active, 1)

    def test_concurrent_agent_token_revoke_records_single_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_merchant(db_file)
            with db_session(db_file) as conn:
                agent_token, _expires = token_service.issue_agent_token(
                    conn,
                    "seller-a",
                    token_service.default_merchant_agent_id("seller-a"),
                    positive_whole_seconds=positive_seconds,
                )
            barrier = threading.Barrier(2)
            revoked_at_values: list[str] = []
            errors: list[Exception] = []

            def revoke_once():
                conn = open_connection(db_file)
                try:
                    barrier.wait(timeout=5)
                    result = agent_service.revoke_agent_token(conn, "seller-a", token=agent_token)
                    conn.commit()
                    revoked_at_values.append(result["revoked_at"])
                except (ConflictError, sqlite3.OperationalError) as exc:
                    conn.rollback()
                    errors.append(exc)
                finally:
                    conn.close()

            threads = [threading.Thread(target=revoke_once) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())

            self.assertGreaterEqual(len(revoked_at_values), 1)
            self.assertEqual(len(set(revoked_at_values)), 1)
            with db_session(db_file) as conn:
                token_rows = conn.execute(
                    "select revoked_at from api_tokens where role = 'agent'"
                ).fetchall()
                revoke_audits = conn.execute(
                    """
                    select details_json from audit_events
                    where conversation_id = '' and event = 'agent_token_revoked'
                    """
                ).fetchall()
            self.assertEqual(len(token_rows), 1)
            self.assertEqual(token_rows[0]["revoked_at"], revoked_at_values[0])
            self.assertEqual(len(revoke_audits), 1)

    def test_sequential_agent_token_revoke_replay_stays_idempotent_without_new_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_merchant(db_file)
            with db_session(db_file) as conn:
                agent_token, _expires = token_service.issue_agent_token(
                    conn,
                    "seller-a",
                    token_service.default_merchant_agent_id("seller-a"),
                    positive_whole_seconds=positive_seconds,
                )
                first = agent_service.revoke_agent_token(conn, "seller-a", token=agent_token)
                replay = agent_service.revoke_agent_token(conn, "seller-a", token=agent_token)

            self.assertTrue(first["ok"])
            self.assertTrue(replay["ok"])
            self.assertTrue(replay["revoked"])
            self.assertEqual(replay["revoked_at"], first["revoked_at"])
            self.assertEqual(replay["agent_id"], first["agent_id"])
            with db_session(db_file) as conn:
                revoke_audits = conn.execute(
                    """
                    select count(*) from audit_events
                    where conversation_id = '' and event = 'agent_token_revoked'
                    """
                ).fetchone()[0]
            self.assertEqual(revoke_audits, 1)

    def test_merchant_bootstrap_replays_token_and_admin_can_recover(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            admin_token = "admin-bootstrap-secret"
            payload = {
                "id": "seller-a",
                "name": "West Lake Tea",
                "admin_token": admin_token,
                "_idempotency_key": "merchant-create-1",
            }
            with patch.dict(os.environ, {"SHOPPING_ADMIN_TOKEN": admin_token}, clear=False):
                first_status, first = handle_request(db_file, "POST", "/merchants", payload)
                replay_status, replay = handle_request(db_file, "POST", "/merchants", payload)
                rotate_status, rotated = handle_request(
                    db_file,
                    "POST",
                    "/merchants/seller-a/token/rotate",
                    {"admin_token": admin_token},
                )
                stale_replay_status, stale_replay = handle_request(
                    db_file,
                    "POST",
                    "/merchants",
                    payload,
                )

            self.assertEqual(
                (first_status, replay_status, rotate_status, stale_replay_status),
                (200, 200, 200, 409),
            )
            self.assertEqual(first["merchant_token"], replay["merchant_token"])
            self.assertTrue(replay["idempotent"])
            self.assertNotEqual(rotated["merchant_token"], first["merchant_token"])
            self.assertIn("token was rotated", stale_replay["error"])
            with db_session(db_file) as conn:
                with self.assertRaises(AuthError):
                    token_service.require_merchant_token(conn, "seller-a", first["merchant_token"])
                token_service.require_merchant_token(conn, "seller-a", rotated["merchant_token"])

    def test_private_merchant_config_exposes_boundaries_only_to_owner_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            admin_token = "admin-bootstrap-secret"
            with patch.dict(os.environ, {"SHOPPING_ADMIN_TOKEN": admin_token}, clear=False):
                _status, created = handle_request(
                    db_file,
                    "POST",
                    "/merchants",
                    {
                        "id": "seller-a",
                        "name": "West Lake Tea",
                        "automation_boundaries": "Never negotiate price.",
                        "admin_token": admin_token,
                    },
                )
            denied_status, _denied = handle_request(
                db_file,
                "GET",
                "/merchants/seller-a/private-config",
                {},
            )
            allowed_status, private = handle_request(
                db_file,
                "GET",
                "/merchants/seller-a/private-config",
                {"merchant_token": created["merchant_token"]},
            )
            public_status, public = handle_request(db_file, "GET", "/merchants/seller-a")

            self.assertEqual((denied_status, allowed_status, public_status), (403, 200, 200))
            self.assertEqual(private["automation_boundaries"], "Never negotiate price.")
            self.assertTrue(private["version"])
            self.assertNotIn("automation_boundaries", json.dumps(public))

    def test_private_merchant_config_rejects_other_merchant_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            admin_token = "admin-bootstrap-secret"
            with patch.dict(os.environ, {"SHOPPING_ADMIN_TOKEN": admin_token}, clear=False):
                _status_a, created_a = handle_request(
                    db_file,
                    "POST",
                    "/merchants",
                    {"id": "seller-a", "name": "West Lake Tea", "admin_token": admin_token},
                )
                _status_b, created_b = handle_request(
                    db_file,
                    "POST",
                    "/merchants",
                    {"id": "seller-b", "name": "Other Tea", "admin_token": admin_token},
                )
            with db_session(db_file) as conn:
                agent_token_b, _expires = token_service.issue_agent_token(
                    conn,
                    "seller-b",
                    token_service.default_merchant_agent_id("seller-b"),
                    positive_whole_seconds=positive_seconds,
                )
            self.assertNotEqual(created_a["merchant_token"], created_b["merchant_token"])

            cross_merchant_status, _body = handle_request(
                db_file,
                "GET",
                "/merchants/seller-a/private-config",
                {"merchant_token": created_b["merchant_token"]},
            )
            cross_agent_status, _body = handle_request(
                db_file,
                "GET",
                "/merchants/seller-a/private-config",
                {"agent_token": agent_token_b},
            )
            bogus_status, _body = handle_request(
                db_file,
                "GET",
                "/merchants/seller-a/private-config",
                {"merchant_token": "not-a-real-token"},
            )

            self.assertEqual((cross_merchant_status, cross_agent_status, bogus_status), (403, 403, 403))

    def test_conversation_summary_strips_private_merchant_fields(self):
        """H2: 买家可见的会话响应不得含 contact / automation_boundaries。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                create_merchant(
                    conn,
                    "seller-a",
                    "西湖龙井茶庄",
                    city="杭州",
                    contact="wechat:westlake",
                    automation_boundaries='{"floor": 9.5}',
                )
                create_product(conn, "seller-a", "tea-a", "西湖龙井礼盒", 88, 5)
                conversation = ensure_conversation(conn, "alice", "seller-a", "tea-a")
                summary = conversation_summary(conn, conversation["id"])
            product_merchant = summary["product"]["merchant"]
            self.assertNotIn("automation_boundaries", product_merchant)
            self.assertNotIn("contact", product_merchant)
            # 商品投影本身仍保留（商家名/配送等公开信息）
            self.assertEqual(product_merchant["name"], "西湖龙井茶庄")
            self.assertEqual(summary["product"]["price"], 88)

    def test_merchant_bootstrap_replays_without_client_idempotency_key(self):
        """Replay works even when the client does not supply an Idempotency-Key."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            admin_token = "admin-auto-replay-key"
            payload = {
                "id": "seller-x",
                "name": "Auto Replay Tea",
                "admin_token": admin_token,
            }
            with patch.dict(os.environ, {"SHOPPING_ADMIN_TOKEN": admin_token}, clear=False):
                first_status, first = handle_request(db_file, "POST", "/merchants", payload)
                replay_status, replay = handle_request(db_file, "POST", "/merchants", payload)

            self.assertEqual(first_status, 200)
            self.assertEqual(replay_status, 200)
            self.assertTrue(first["merchant_token"].startswith("shopping_merchant_"))
            self.assertEqual(first["merchant_token"], replay["merchant_token"])
            self.assertTrue(replay["idempotent"])
            # Token must be valid
            with db_session(db_file) as conn:
                token_service.require_merchant_token(conn, "seller-x", first["merchant_token"])

    def test_merchant_bootstrap_recover_endpoint_returns_fresh_token(self):
        """POST /merchants/{id}/token/recover issues a new valid token via admin auth."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            admin_token = "admin-recover-secret"
            with patch.dict(os.environ, {"SHOPPING_ADMIN_TOKEN": admin_token}, clear=False):
                create_status, created = handle_request(
                    db_file, "POST", "/merchants",
                    {"id": "seller-r", "name": "Recover Tea", "admin_token": admin_token},
                )
                recover_status, recovered = handle_request(
                    db_file, "POST", "/merchants/seller-r/token/recover",
                    {"admin_token": admin_token},
                )

            self.assertEqual(create_status, 200)
            self.assertEqual(recover_status, 200)
            self.assertTrue(recovered["recovered"])
            self.assertTrue(recovered["merchant_token"].startswith("shopping_merchant_"))
            self.assertNotEqual(recovered["merchant_token"], created["merchant_token"])
            with db_session(db_file) as conn:
                # Original token is now revoked
                with self.assertRaises(AuthError):
                    token_service.require_merchant_token(conn, "seller-r", created["merchant_token"])
                # Recovered token is valid
                token_service.require_merchant_token(conn, "seller-r", recovered["merchant_token"])

    def test_merchant_bootstrap_recover_rejects_non_admin(self):
        """Recover endpoint requires admin token; merchant token is not enough."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            admin_token = "admin-recover-auth"
            with patch.dict(os.environ, {"SHOPPING_ADMIN_TOKEN": admin_token}, clear=False):
                _status, created = handle_request(
                    db_file, "POST", "/merchants",
                    {"id": "seller-z", "name": "Auth Test Tea", "admin_token": admin_token},
                )
                denied_status, _denied = handle_request(
                    db_file, "POST", "/merchants/seller-z/token/recover",
                    {"merchant_token": created["merchant_token"]},
                )

            self.assertEqual(denied_status, 403)

    def test_merchant_bootstrap_stale_replay_points_to_recover_endpoint(self):
        """After recovery rotates the token, replaying the bootstrap request raises
        a ConflictError that mentions the recover endpoint."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            admin_token = "admin-stale-replay"
            payload = {
                "id": "seller-s",
                "name": "Stale Replay Tea",
                "admin_token": admin_token,
            }
            with patch.dict(os.environ, {"SHOPPING_ADMIN_TOKEN": admin_token}, clear=False):
                handle_request(db_file, "POST", "/merchants", payload)
                # Recover rotates the token
                handle_request(
                    db_file, "POST", "/merchants/seller-s/token/recover",
                    {"admin_token": admin_token},
                )
                # Replaying the original bootstrap now fails because the deterministic
                # token was revoked by recovery.
                stale_status, stale = handle_request(db_file, "POST", "/merchants", payload)

            self.assertEqual(stale_status, 409)
            self.assertIn("token/recover", stale.get("error", ""))

    def test_cli_merchant_create_returns_token(self):
        """CLI merchant create outputs a merchant_token that can be used for auth."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            buf = StringIO()
            args = SimpleNamespace(
                db=str(db_file),
                id="cli-tea",
                name="CLI Tea Merchant",
                city="杭州",
                service_area="",
                contact="",
                hours="",
                automation_boundaries="",
                tags="",
                delivery_fee=0,
                delivery_eta_minutes=0,
                delivery_radius_km=0,
                format="json",
                data=None,
            )
            with redirect_stdout(buf):
                from shopping_cli import cli_catalog_commands
                cli_catalog_commands.cmd_merchant_create(args)

            output = json.loads(buf.getvalue())
            self.assertTrue(output["ok"])
            self.assertTrue(output["merchant_token"].startswith("shopping_merchant_"))
            with db_session(db_file) as conn:
                token_service.require_merchant_token(conn, "cli-tea", output["merchant_token"])

    def test_daemon_paths_do_not_collide_for_lossy_merchant_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = {
                merchant_daemon.agent_paths(merchant_id, tmp)["pid_file"]
                for merchant_id in ("seller/a", "seller:a", "seller a", "seller_a")
            }
            self.assertEqual(len(paths), 4)


if __name__ == "__main__":
    unittest.main()
