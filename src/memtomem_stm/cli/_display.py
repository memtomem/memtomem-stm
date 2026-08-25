"""Display escaping for config- and host-derived values printed to a terminal.

Lives in its own leaf module (imports nothing from the package) because every
CLI command that renders such a value needs it, and ``cli.proxy`` — where this
started life in #754 — registers ``mms_import``, ``mms_host``, ``mms_project``,
``config_cmd``, ``hook_cmd`` and ``selection_cmd`` as its Click subgroups
(module-scope imports until #862, lazily resolved since). Any of them importing
back from ``proxy`` at module scope risks a cycle, which is why #758 had to
close its ``mms project route`` sites with a *function-local*
``from memtomem_stm.cli.proxy import _disp``. Same constraint ``utils/json_out``
was split out for, and the same fix (#760).

This is the display half of the pair. The machine-readable half — what a
``--json`` leg emits, where a consumer decodes the value back — is
``utils/json_out``, and the two must not be swapped: escaping a ``--json``
payload for a *terminal* would corrupt the data a consumer reads.
"""

from __future__ import annotations

# The Trojan-Source bidirectional controls — embeddings, overrides, isolates
# and the terminators that close them. They reorder how the rest of the line
# *renders* without changing its bytes, so a server name carrying one can forge
# a second ``Note:`` out of the surrounding prose. The terminators are included
# conservatively alongside the openers they close: this app never emits a
# directional embedding or isolate of its own, so a lone PDF or PDI has nothing
# to terminate here, but its rendering is a property of whatever the enclosing
# terminal and any neighbouring value have already opened, which is not ours to
# reason about. The plain marks (LRM/RLM/ALM) are deliberately absent. They are
# not inert — under UAX #9 a mark supplies a strong direction that can change
# how *adjacent neutrals* (punctuation, spaces, digits) resolve — but they
# cannot override a strong run or open a span the way the controls above can,
# and they are what makes a legitimate RTL name render correctly, so escaping
# them would regress the case #754 exists to protect. Spelled as escapes: these
# characters are invisible, and writing them literally would make this source
# line itself a Trojan-Source hazard.
_BIDI_CONTROLS = frozenset(
    "\u202a\u202b\u202c\u202d\u202e"  # LRE RLE PDF LRO RLO
    "\u2066\u2067\u2068\u2069"  # LRI RLI FSI PDI
)


def _disp_escapes(ch: str) -> bool:
    """Whether ``ch`` must not reach the terminal as itself (#754).

    **This predicate, together with ``_BIDI_CONTROLS``, is the one canonical
    definition of the display-hostile set** — the branches below give the ranges
    and that frozenset gives the bidirectional members. Everything else —
    :func:`_disp`, ``proxy._shell_join``, the CHANGELOG, the docstrings below —
    refers to "the set :func:`_disp_escapes` defines" and gives examples rather
    than restating the membership, because four hand-synchronized copies of it
    drifted apart across review rounds. Read the code here for the exact answer;
    treat any inventory elsewhere as illustrative.

    The members, and why each is in:

    - **C0** (U+0000–U+001F), which covers CR, LF and NUL — unrenderable or
      line-breaking — and ESC, the ANSI/CSI introducer.
    - **DEL and C1** (U+007F–U+009F); U+009B is a single-byte CSI, so it opens
      an escape sequence on its own.
    - **Zl/Zp** (U+2028, U+2029) — a line break to enough renderers to split a
      hint that is supposed to be one line.
    - **Lone surrogates** (U+D800–U+DFFF) — not encodable to UTF-8 at all, so
      they raise on output rather than rendering. ``_load`` reads the config as
      strict UTF-8, so one arrives by the other route JSON allows: a
      ``\\ud800`` escape in an otherwise-ASCII file.
    - **``_BIDI_CONTROLS``** — reorder the rendered line without changing bytes.

    Deliberately *not* members: everything else, notably CJK, emoji, ZWJ
    sequences and variation selectors, plus the plain directional marks
    LRM/RLM/ALM (see ``_BIDI_CONTROLS``).
    """
    return (
        ch <= "\x1f"  # C0: CR/LF/NUL and ESC, the ANSI/CSI introducer
        or "\x7f" <= ch <= "\x9f"  # DEL and C1 (U+009B is a one-byte CSI)
        or "\u2028" <= ch <= "\u2029"  # LINE/PARAGRAPH SEPARATOR (Zl/Zp)
        or "\ud800" <= ch <= "\udfff"  # lone surrogates: unencodable, not renderable
        or ch in _BIDI_CONTROLS
    )


def _disp(value: str) -> str:
    """Escape a config-derived value for display inside human-facing prose (#754).

    The companion to ``proxy._shell_join``, for the other half of the same
    hints. Both act on the same character class — :func:`_disp_escapes` is the
    single definition of "must not reach the terminal as itself" — but they act
    on it differently: a runnable command is refused wholesale, because an
    escaped command would paste as a *different* command, while prose is escaped
    in place so the sentence around the value still renders. Config is plain
    ``json.loads`` with no character validation on server names, so an imported
    or hand-edited config can carry any of these.

    Escapes a member of :func:`_disp_escapes`'s set as ``\\uXXXX``, an inert
    ASCII representation (every member is BMP, so four hex digits always
    suffice), and preserves everything else, notably CJK, emoji
    and ZWJ sequences. That preservation is why the set is written as explicit
    ranges rather than ``unicodedata.category(ch) in {"Cc","Cf","Cs"}`` (the
    test :meth:`SurfacingFormatter._sanitize` uses for Markdown): ``Cf``
    contains ZWJ, and escaping it would split a family emoji apart.

    Two properties the call sites depend on:

    - **Quote-neutral.** Only the value is escaped, never a quote or delimiter,
      so each site keeps its own convention (``'{name}'`` prose, a
      ``json.dumps``-encoded key). This is why ``repr()`` was rejected as the
      primitive: it adds quotes, and it escapes backslashes.
    - **Identity on clean input.** A value with nothing to escape is returned
      unchanged, so every ordinary name renders byte-for-byte as before (the
      same guarantee ``_cmd_quote`` carries since #750).

    Residual (documented, not fixed): ``\\`` is never escaped, which keeps
    Windows paths readable and leaves any backslash escape ``json.dumps``
    already produced intact, but makes the encoding non-injective — a name
    containing the literal text ``\\u001B`` renders like one containing a real
    ESC. Acceptable on a display-only surface. Apply to the interpolated value, never to an
    assembled line: the ``_warn``/``_ok``/``_err`` styling wraps *around* these
    values and its escape codes must stay real.
    """
    if not any(_disp_escapes(ch) for ch in value):
        return value
    return "".join(f"\\u{ord(ch):04X}" if _disp_escapes(ch) else ch for ch in value)
