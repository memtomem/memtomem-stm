"""Unit tests for the FastMCP compatibility layer.

Focused on the ``_tag_annotations_title`` helper that prepends a ``[server]``
scope tag to ``ToolAnnotations.title`` for ``/mcp`` picker disambiguation.
"""

from __future__ import annotations

from mcp.types import ToolAnnotations

from memtomem_stm.proxy._fastmcp_compat import _tag_annotations_title


def test_tag_title_prepends_server_when_title_present() -> None:
    annotations = ToolAnnotations(title="Close browser", destructiveHint=True)
    tagged = _tag_annotations_title(annotations, "playwright")
    assert tagged is not annotations  # copy-on-write
    assert tagged.title == "[playwright] Close browser"


def test_tag_title_preserves_other_hint_fields() -> None:
    annotations = ToolAnnotations(
        title="Read file",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    tagged = _tag_annotations_title(annotations, "fs")
    assert tagged.readOnlyHint is True
    assert tagged.destructiveHint is False
    assert tagged.idempotentHint is True
    assert tagged.openWorldHint is False


def test_tag_title_returns_none_unchanged_when_annotations_is_none() -> None:
    assert _tag_annotations_title(None, "playwright") is None


def test_tag_title_passthrough_when_title_missing() -> None:
    annotations = ToolAnnotations(readOnlyHint=True)
    tagged = _tag_annotations_title(annotations, "Context7")
    assert tagged is annotations


def test_tag_title_passthrough_when_title_empty_string() -> None:
    annotations = ToolAnnotations(title="", readOnlyHint=True)
    tagged = _tag_annotations_title(annotations, "Context7")
    assert tagged is annotations


def test_tag_title_passthrough_on_unknown_shape() -> None:
    class _Opaque:
        title = "Close browser"

    opaque = _Opaque()
    tagged = _tag_annotations_title(opaque, "playwright")
    assert tagged is opaque


def test_tag_title_falls_back_to_original_when_model_copy_raises() -> None:
    class _BrokenCopy:
        title = "Close browser"

        def model_copy(self, update: dict | None = None) -> object:
            raise RuntimeError("simulated copy failure")

    broken = _BrokenCopy()
    tagged = _tag_annotations_title(broken, "playwright")
    assert tagged is broken
