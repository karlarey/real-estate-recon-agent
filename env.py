"""Minimal .env loader.

Reads KEY=VALUE lines from a .env file next to this module and puts them into
os.environ, without overwriting anything already set in the real environment.

Exists because agent_chat.py and agent.py read their config with
os.environ.get but nothing ever loaded the .env file, so the settings
documented in .env.example were silently ignored.

Deliberately dependency-free -- python-dotenv would be one more package to
install and pin for a dozen lines of parsing.

    import env; env.load()
"""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, ".env")

_loaded = False


def load(path=None):
    """Load .env into os.environ. Idempotent; never overwrites real env vars.

    Returns the number of variables set.
    """
    global _loaded
    if _loaded:
        return 0

    target = path or DEFAULT_PATH
    if not os.path.exists(target):
        _loaded = True
        return 0

    count = 0
    with open(target, encoding="utf-8-sig") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not key:
                continue
            # A real environment variable wins over the file.
            if key not in os.environ:
                os.environ[key] = value
                count += 1

    _loaded = True
    return count
