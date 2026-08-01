from __future__ import annotations


class FakeSocket:
    """Minimal socket timeout recorder shared by transport contract tests."""

    def settimeout(self, value):
        self.timeout = value
