"""Shared defaults for memtomem CLI commands.

Keep path defaults outside the command modules so help text and runtime
resolution cannot drift when commands live in modules that import each other.
``ProxyConfig.config_path`` keeps an intentionally independent runtime-model
default in ``proxy/config.py`` because the proxy layer must not import CLI code.
"""

from pathlib import Path


DEFAULT_PROXY_CONFIG = Path("~/.memtomem/stm_proxy.json")
