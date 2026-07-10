"""Tests for ``utils.redact.sanitize_secrets`` — the central free-form
string sanitizer (⑧).

Pins the three normalized substitution rules from the ratified plan: empty
values are dropped (a naive ``replace("", …)`` poisons the whole message),
duplicate values apply once, and longer values substitute before shorter
ones (short-first would leak the longer secret's suffix).
"""

from memtomem_stm.utils.redact import sanitize_secrets


class TestSanitizeSecrets:
    def test_replaces_raw_token(self) -> None:
        out = sanitize_secrets("401 Unauthorized: Bearer sekrit-token-123", ["sekrit-token-123"])
        assert "sekrit-token-123" not in out
        assert out == "401 Unauthorized: Bearer <REDACTED>"

    def test_replaces_all_occurrences(self) -> None:
        out = sanitize_secrets("tok=abc; retry with abc", ["abc"])
        assert out == "tok=<REDACTED>; retry with <REDACTED>"

    def test_empty_value_leaves_message_intact(self) -> None:
        msg = "connection refused by host"
        assert sanitize_secrets(msg, [""]) == msg

    def test_empty_value_alongside_real_value(self) -> None:
        out = sanitize_secrets("key=hunter2", ["", "hunter2"])
        assert out == "key=<REDACTED>"

    def test_overlapping_values_longest_first_no_suffix_leak(self) -> None:
        # If "abc" were substituted before "abcdef", the message would
        # degrade to "<REDACTED>def" — leaking the longer secret's suffix.
        out = sanitize_secrets("secret=abcdef", ["abc", "abcdef"])
        assert out == "secret=<REDACTED>"
        assert "def" not in out

    def test_duplicate_values_apply_once(self) -> None:
        out = sanitize_secrets("v=xyz", ["xyz", "xyz"])
        assert out == "v=<REDACTED>"

    def test_multiple_distinct_secrets(self) -> None:
        out = sanitize_secrets("user=alice pw=hunter2", ["alice", "hunter2"])
        assert out == "user=<REDACTED> pw=<REDACTED>"

    def test_empty_text_passthrough(self) -> None:
        assert sanitize_secrets("", ["hunter2"]) == ""

    def test_no_secrets_passthrough(self) -> None:
        msg = "timeout (10s)"
        assert sanitize_secrets(msg, []) == msg

    def test_custom_placeholder(self) -> None:
        out = sanitize_secrets("t=abc", ["abc"], placeholder="***")
        assert out == "t=***"

    def test_secret_overlapping_placeholder_text_single_pass(self) -> None:
        # Each value is substituted in a single pass — a secret that happens
        # to be a substring of the placeholder ("RED") must not cascade into
        # rewriting the placeholder just inserted.
        out = sanitize_secrets("v=RED", ["RED"])
        assert out == "v=<REDACTED>"

    def test_second_secret_does_not_cascade_into_first_placeholder(self) -> None:
        # Regression: sequential str.replace let a later short secret ("RED")
        # rewrite the "<REDACTED>" a previous pass inserted for a longer
        # secret, producing "<<REDACTED>ACTED>". A single regex pass over the
        # original text consumes each span once and never re-scans inserts.
        out = sanitize_secrets("x=long-secret", ["long-secret", "RED"])
        assert out == "x=<REDACTED>"

    def test_placeholder_with_backslash_not_treated_as_group_ref(self) -> None:
        # A custom placeholder containing regex-replacement metacharacters
        # (\1, \g<...>) must be inserted literally, not interpreted.
        out = sanitize_secrets("t=abc", ["abc"], placeholder=r"\1<X>")
        assert out == r"t=\1<X>"
