"""secure_env — least-privilege environment for spawned subprocesses / browsers.

Agent Zero dotenv-injects secrets into ``os.environ`` at RUNTIME (model API keys, AUTH_/ROOT_/RFC_
passwords). A spawned child — a browser, ffmpeg, yt-dlp, a CLI, Xvfb — inherits the FULL parent env
by default (``subprocess`` and Playwright/Camoufox both default to ``os.environ`` when no ``env=`` is
given), so it silently receives secrets it never needs. A browser that then visits attacker-influenced
pages is the worst case (a browser-RCE reads the keys out of its own env).

``clean_env()`` returns an allowlist-only copy of ``os.environ`` to hand to the spawn instead, so the
child gets exactly what it needs and nothing secret. Pure stdlib (no Agent Zero imports) → unit-testable
standalone.
"""

from __future__ import annotations

import os

# Non-secret essentials a child legitimately needs: paths, locale, display, temp.
# NEVER an API key / password / token / session credential.
_BASE_ALLOW = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ",
    "DISPLAY", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "XDG_CACHE_HOME", "XDG_DATA_HOME",
    "TMPDIR", "TMP", "TEMP",
)
# Proxy / egress config the child needs to reach the network the same way the host does.
# Harmless when unset; REQUIRED if the host routes outbound through a proxy — so we pass it through
# by default and the env-trim never breaks browsing/downloads.
_PROXY_ALLOW = (
    "HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "ftp_proxy", "all_proxy", "no_proxy",
)


def clean_env(extra=None, *, allow=(), proxy=True):
    """An allowlist-only copy of ``os.environ`` — secrets are excluded by construction.

    extra: dict of explicit additions (e.g. ``{"HOME": rt}`` or the single credential a tool needs).
           ``None`` values are dropped; ``extra`` OVERRIDES allowlisted values.
    allow: extra env-var NAMES to pass through beyond the base set.
    proxy: include proxy/egress vars (default True) so network access is unaffected.
    """
    keys = set(_BASE_ALLOW)
    keys.update(allow)
    if proxy:
        keys.update(_PROXY_ALLOW)
    env = {k: os.environ[k] for k in keys if k in os.environ}
    if extra:
        env.update({k: v for k, v in extra.items() if v is not None})
    return env
