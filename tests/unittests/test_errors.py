"""Tests for exception context and rendering helpers."""

import unittest

from logsync.errors import error_context, format_error


class ErrorTests(unittest.TestCase):
    def test_preserves_and_annotates_arbitrary_exceptions(self) -> None:
        """Preserve the original exception and attach context without constructor assumptions."""
        injected = UnicodeDecodeError("ascii", b"\xff", 0, 1, "injected")

        with self.assertRaises(UnicodeDecodeError) as raised:
            with error_context("decoding failed"):
                raise injected

        self.assertIs(raised.exception, injected)
        self.assertEqual(raised.exception.__notes__, ["decoding failed"])

    def test_formats_exception_messages_and_notes_on_one_line(self) -> None:
        """Render an exception type, multiline message, and multiline notes on one line."""
        injected = ValueError("first line\nsecond line")
        injected.add_note("outer context\nmore context")

        self.assertEqual(format_error(injected), "ValueError: first line | second line | outer context | more context")


if __name__ == "__main__":
    unittest.main()
