import json
import tempfile
from pathlib import Path
from unittest import TestCase, mock
from shopping_cli.api.handlers.human_review import resolve_human_review, resolve_human_review_item
from shopping_cli.db.session import db_session
from shopping_cli.core.catalog import create_merchant
from shopping_cli.core.conversations import ensure_conversation, add_flag
from shopping_cli.services.tokens import issue_merchant_token
from shopping_cli.core.errors import AuthError, ValidationError


class ActorLabels(TestCase):
    def seed(self, path, reason):
        with db_session(path) as conn:
            create_merchant(conn, "admin", "Synthetic")
            token = issue_merchant_token(conn, "admin")
            cid = ensure_conversation(conn, "buyer", "admin")["id"]
            rid = add_flag(conn, cid, reason)["id"]
        return token, cid, rid

    def test_two_entries_merchant_admin_named_identity_not_operator(self):
        for single in [True, False]:
            with self.subTest(single=single), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "db"
                token, cid, rid = self.seed(path, "human_required")
                payload = {"merchant_token": token, "sender": "operator", "action": "reply", "text": "safe reply"}
                (resolve_human_review_item(path, rid, payload) if single else resolve_human_review(path, cid, payload))
                with db_session(path) as conn:
                    row = conn.execute(
                        "select sender,structured_payload_json from messages where conversation_id=? order by id desc",
                        (cid,),
                    ).fetchone()
                    self.assertEqual(row["sender"], "merchant")
                    self.assertEqual(json.loads(row["structured_payload_json"])["source_id"], "admin")
                    audit = conn.execute(
                        "select actor from audit_events where conversation_id=? and event='message_appended' order by id desc",
                        (cid,),
                    ).fetchone()
                    self.assertEqual(audit["actor"], "merchant")

    def test_admin_operator_positive_and_merchant_suspicious_guard_invalid_sender_preserved(self):
        for single in [True, False]:
            with (
                self.subTest(single=single),
                tempfile.TemporaryDirectory() as tmp,
                mock.patch.dict(
                    "os.environ",
                    {"SHOPPING_ADMIN_TOKEN": "synthetic-admin-credential", "SHOPPING_DEPLOYMENT_PROFILE": "local"},
                ),
            ):
                path = Path(tmp) / "db"
                token, cid, rid = self.seed(path, "suspicious_content")
                def call(payload):
                    return (resolve_human_review_item(path, rid, payload)
                            if single else resolve_human_review(path, cid, payload))
                with self.assertRaises(AuthError):
                    call({"merchant_token": token, "sender": "operator", "text": "denied"})
                with self.assertRaises(ValidationError):
                    call({"admin_token": "synthetic-admin-credential", "sender": "illegal", "text": "denied"})
                call({"admin_token": "synthetic-admin-credential", "sender": "merchant", "text": "approved"})
                with db_session(path) as conn:
                    row = conn.execute(
                        "select sender,structured_payload_json from messages where conversation_id=? order by id desc",
                        (cid,),
                    ).fetchone()
                    self.assertEqual(row["sender"], "operator")
                    self.assertEqual(json.loads(row["structured_payload_json"])["source_id"], "admin")
