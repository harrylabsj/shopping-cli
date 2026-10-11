from __future__ import annotations
import unittest
from shopping_cli.db.session import db_session, now_iso
from shopping_cli.services.negotiation import require_negotiation_actor, _find_decision_replay
import test_negotiation_api as setup_fixture


class LongReplayTest(unittest.TestCase):
    def setUp(self):
        self.f = setup_fixture.NegotiationApiTest()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def test_actual_accepted_decision_replays_after_101_later_messages_without_new_write(self):
        status, _ = self.f.claim(self.f.merchant_token)
        self.assertEqual(status, 200)
        decision = setup_fixture.make_decision(self.f.conversation_id, self.f.buyer_message_id)
        status, first = self.f.submit(self.f.merchant_token, decision)
        self.assertEqual(status, 200)
        self.assertEqual(first["policy_result"]["result"], "accepted")
        with db_session(self.f.db_file) as c:
            for n in range(101):
                c.execute(
                    "INSERT INTO messages(conversation_id,sender,intent,text,structured_payload_json,created_at) VALUES (?,?,?,?,?,?)",
                    (self.f.conversation_id, "buyer", "support", f"later synthetic {n}", "{}", now_iso()),
                )
            before = c.execute("SELECT count(*) FROM messages").fetchone()[0]
            audits = c.execute("SELECT count(*) FROM audit_events").fetchone()[0]
        status, replay = self.f.submit(self.f.merchant_token, decision)
        self.assertEqual(status, 200, replay)
        self.assertEqual(replay["policy_result"]["message_id"], first["policy_result"]["message_id"])
        self.assertFalse(decision["proposal"]["stock"]["reserved"])
        with db_session(self.f.db_file) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM messages").fetchone()[0], before)
            self.assertEqual(c.execute("SELECT count(*) FROM audit_events").fetchone()[0], audits)
            self.assertEqual(c.execute("SELECT stock FROM products WHERE sku='cup-1'").fetchone()[0], 12)
            self.assertFalse(
                {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                & {"orders", "payments", "reservations"}
            )
        changed = {**decision, "public_message": "Different synthetic public text"}
        status, _ = self.f.submit(self.f.merchant_token, changed)
        self.assertEqual(status, 409)
        status, _ = self.f.submit(self.f.other_merchant_token, decision)
        self.assertEqual(status, 403)

    def test_reserved_incomplete_ledger_cannot_forge_completed_replay(self):
        with db_session(self.f.db_file) as c:
            actor = require_negotiation_actor(c, self.f.merchant_token)
            c.execute(
                "INSERT INTO negotiation_decision_idempotency(conversation_id,agent_id,idempotency_key,message_id,decision_json,created_at) VALUES (?,?,?,0,'{}',?)",
                (self.f.conversation_id, actor.agent_id, "unfinished", now_iso()),
            )
            self.assertIsNone(_find_decision_replay(c, actor, self.f.conversation_id, "unfinished"))
