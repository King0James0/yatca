"""smoke_secure_env — clean_env() excludes A0 runtime secrets while keeping the essentials a
spawned browser/subprocess needs (PATH/HOME/LANG/DISPLAY/proxy), and honours extra/allow/proxy.

Standalone: `python tests/smoke_secure_env.py` (no A0, no camoufox/playwright needed)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # plugin root
from helpers import secure_env  # noqa: E402


def main():
    fails = []
    saved = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update({
            # essentials a child legitimately needs
            "PATH": "/usr/bin", "HOME": "/home/x", "LANG": "en_US.UTF-8",
            "DISPLAY": ":121", "HTTPS_PROXY": "http://proxy:3128",
            # the secrets A0 injects into os.environ at runtime — MUST NOT leak to a child
            "API_KEY_ANTHROPIC": "sk-secret", "API_KEY_OPENAI": "sk-2",
            "AUTH_PASSWORD": "pw", "ROOT_PASSWORD": "rootpw", "RFC_PASSWORD": "rfc",
            "BRAVE_TOKEN": "bt", "GITHUB_TOKEN": "ght",
        })
        env = secure_env.clean_env()
        for k in ("PATH", "HOME", "LANG", "DISPLAY", "HTTPS_PROXY"):
            if env.get(k) != os.environ[k]:
                fails.append(f"essential {k} missing/wrong")
        for k in ("API_KEY_ANTHROPIC", "API_KEY_OPENAI", "AUTH_PASSWORD", "ROOT_PASSWORD",
                  "RFC_PASSWORD", "BRAVE_TOKEN", "GITHUB_TOKEN"):
            if k in env:
                fails.append(f"SECRET LEAKED: {k}")

        # extra overrides an allowlisted value, adds a needed credential, drops None
        env2 = secure_env.clean_env(extra={"HOME": "/run/rt", "GH_TOKEN": "tok", "DROP": None})
        if env2.get("HOME") != "/run/rt":
            fails.append("extra did not override HOME")
        if env2.get("GH_TOKEN") != "tok":
            fails.append("extra credential not added")
        if "DROP" in env2:
            fails.append("None-valued extra not dropped")

        # proxy=False excludes proxy vars
        if "HTTPS_PROXY" in secure_env.clean_env(proxy=False):
            fails.append("proxy=False still included HTTPS_PROXY")

        # allow= passes an extra name through
        os.environ["CUSTOM_X"] = "v"
        if secure_env.clean_env(allow=("CUSTOM_X",)).get("CUSTOM_X") != "v":
            fails.append("allow= passthrough failed")
    finally:
        os.environ.clear()
        os.environ.update(saved)

    if fails:
        print("smoke_secure_env: FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("smoke_secure_env: OK (secrets excluded; PATH/HOME/LANG/DISPLAY/proxy kept; extra/allow/proxy honoured)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
