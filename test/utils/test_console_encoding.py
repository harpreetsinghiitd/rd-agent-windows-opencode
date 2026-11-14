"""Regression tests for Windows console encoding handling.

A cp1252 console makes any log line or script print containing a character such
as `→` (U+2192) raise

    UnicodeEncodeError: 'charmap' codec can't encode character '\u2192'

which aborts the whole RD-Agent loop and, when raised inside a loguru sink,
replaces the real error with the encoding error. `RDAgentLog` now reconfigures
the console streams with ``errors="replace"``, so the run survives.
"""

import sys
import unittest
from unittest import mock

from rdagent.log.logger import RDAgentLog


class _FakeConsoleStream:
    """Minimal stand-in for `sys.stdout` on a non-UTF-8 console."""

    def __init__(self, encoding: str = "cp1252", errors: str = "strict") -> None:
        self.encoding = encoding
        self.errors = errors
        self.reconfigured: list[dict] = []

    def reconfigure(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.reconfigured.append(kwargs)
        self.errors = kwargs.get("errors", self.errors)

    def write(self, text: str) -> int:
        return len(text.encode(self.encoding, errors=self.errors))


class ConsoleEncodingTests(unittest.TestCase):
    def _run_with_streams(self, stdout, stderr) -> None:
        with mock.patch.object(sys, "stdout", stdout), mock.patch.object(sys, "stderr", stderr):
            RDAgentLog._make_console_streams_encoding_safe()

    def test_cp1252_streams_are_made_error_tolerant(self):
        out, err = _FakeConsoleStream(), _FakeConsoleStream()
        self._run_with_streams(out, err)
        self.assertEqual(out.errors, "replace")
        self.assertEqual(err.errors, "replace")

    def test_utf8_streams_are_left_alone(self):
        out = _FakeConsoleStream(encoding="utf-8")
        self._run_with_streams(out, _FakeConsoleStream(encoding="utf-8"))
        self.assertEqual(out.reconfigured, [])

    def test_wrapped_colorama_stream_is_reconfigured(self):
        real = _FakeConsoleStream()
        wrapper = mock.Mock(spec=["wrapped", "encoding"])
        wrapper.wrapped = real
        wrapper.encoding = "cp1252"
        self._run_with_streams(wrapper, real)
        self.assertEqual(real.errors, "replace")

    def test_streams_without_reconfigure_are_skipped(self):
        # pytest capture and StringIO provide no reconfigure().
        class _Bare:
            encoding = "cp1252"

            def write(self, text):
                return len(text)

        self._run_with_streams(_Bare(), _Bare())  # must not raise

    def test_glyph_survives_cp1252_console(self):
        out = _FakeConsoleStream()
        self._run_with_streams(out, out)
        text = "Sampling complete \u2192 Output in: /tmp/x"
        # Before the fix this raised UnicodeEncodeError.
        self.assertEqual(out.write(text), len(text))


if __name__ == "__main__":
    unittest.main()
