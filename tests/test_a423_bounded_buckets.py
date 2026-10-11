from unittest import TestCase, mock
from shopping_cli.api.handlers import erp, negotiation
from shopping_cli.core.errors import RateLimitError


class BoundedBuckets(TestCase):
    def variants(self):
        return [
            (erp, "_erp_sync_buckets", "_ERP_SYNC_BUCKET_CAP", "_enforce_erp_sync_rate_limit"),
            (negotiation, "_decision_buckets", "_DECISION_BUCKET_CAP", "_enforce_decision_rate_limit"),
        ]

    def test_expired_keys_pruned(self):
        for mod, name, cap, fn in self.variants():
            with (
                self.subTest(module=mod.__name__),
                mock.patch.object(mod, name, {"old": (1.0, 1)}),
                mock.patch.object(mod.time, "monotonic", return_value=100.0),
            ):
                getattr(mod, fn)("new")
                self.assertNotIn("old", getattr(mod, name))
                self.assertEqual(len(getattr(mod, name)), 1)

    def test_capacity_rejects_new_without_evicting_active_or_resetting_counts(self):
        for mod, name, cap, fn in self.variants():
            with (
                self.subTest(module=mod.__name__),
                mock.patch.object(mod, name, {"active": (90.0, 2)}),
                mock.patch.object(mod, cap, 1, create=True),
                mock.patch.object(mod.time, "monotonic", return_value=100.0),
            ):
                with self.assertRaises(RateLimitError):
                    getattr(mod, fn)("new")
                self.assertEqual(getattr(mod, name), {"active": (90.0, 2)})
                getattr(mod, fn)("active")
                self.assertEqual(getattr(mod, name)["active"], (90.0, 3))

    def test_window_resets_expired_same_key_and_existing_limit_still_blocks(self):
        for mod, name, cap, fn in self.variants():
            with (
                self.subTest(module=mod.__name__),
                mock.patch.object(mod, name, {"key": (1.0, 9999)}),
                mock.patch.object(mod.time, "monotonic", return_value=100.0),
            ):
                getattr(mod, fn)("key")
                self.assertEqual(getattr(mod, name)["key"], (100.0, 1))
                getattr(mod, name)["key"] = (100.0, 9999)
                with self.assertRaises(RateLimitError):
                    getattr(mod, fn)("key")
