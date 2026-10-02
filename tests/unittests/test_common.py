"""Shared validation helpers."""

import unittest

from logsync.common import safe_user


class CommonTests(unittest.TestCase):
    def test_user_validation(self) -> None:
        """Accept a username component and reject empty, dot, and slash-containing values."""
        self.assertEqual(safe_user("alice"), "alice")
        for invalid in ("", ".", "..", "../alice", "alice/bob"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                safe_user(invalid)
