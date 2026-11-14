"""Typed retry classification preserves upstream deadlines and finite budgets."""

import importlib.util
from email.utils import formatdate

from tests.support import OfflineTestCase


class RetryPolicyTests(OfflineTestCase):
    def policy(self):
        self.assertIsNotNone(importlib.util.find_spec("backend.services.retry_policy"), "A shared finite retry policy is required")
        from backend.services import retry_policy
        return retry_policy

    def test_retry_after_preserves_seconds_and_http_dates(self):
        policy = self.policy()
        self.assertEqual(policy.retry_after("120", 1000), 1120)
        self.assertEqual(policy.retry_after("0", 1000), 1000)
        self.assertEqual(policy.retry_after(formatdate(2000, usegmt=True), 1000), 2000)
        for value in ("-1", "1.5", "invalid", "9" * 1000):
            self.assertIsNone(policy.retry_after(value, 1000))

    def test_retry_after_is_a_floor_and_backoff_is_jittered_and_bounded(self):
        policy = self.policy()
        error = policy.RequestFailure("UPSTREAM_RATE_LIMITED", retry_at=1200)
        decision = policy.decide(error, 0, 3, 1000, jitter=lambda: 0.5)
        self.assertEqual(decision.status, "retry_wait")
        self.assertEqual(decision.retry_at, 1200)
        self.assertNotEqual(policy.backoff(1, lambda: 0), policy.backoff(1, lambda: 1))
        self.assertTrue(0 < policy.backoff(100, lambda: 1) <= 300)

    def test_unknown_and_authentication_failures_do_not_retry(self):
        policy = self.policy()
        from backend.services.resume import FreshAuthRequired
        self.assertEqual(policy.decide(RuntimeError("secret"), 0, 3, 1000).status, "failed")
        self.assertEqual(policy.decide(FreshAuthRequired(), 0, 3, 1000).status, "waiting_for_auth")
        self.assertEqual(policy.decide(TimeoutError(), 3, 3, 1000).status, "failed")

    def test_local_admission_wait_does_not_consume_the_network_retry_budget(self):
        policy = self.policy()
        error = policy.RequestFailure("ADMISSION_LIMITED", retry_at=1100, consumes_retry=False)
        decision = policy.decide(error, 3, 3, 1000)
        self.assertEqual(decision.status, "retry_wait")
        self.assertEqual(decision.retry_count, 3)
