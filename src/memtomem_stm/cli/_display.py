"""Display escaping for the CLI; the implementation is :mod:`memtomem_stm.utils.display`.

Kept so every CLI module keeps importing from the CLI package. It imports only
that leaf, so it cannot form a cycle with ``cli.proxy`` either (#760).
"""

from __future__ import annotations

from memtomem_stm.utils.display import _disp, _disp_escapes

__all__ = ["_disp", "_disp_escapes"]
