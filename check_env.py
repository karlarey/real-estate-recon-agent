"""Report which runtime dependencies are importable, and their versions.

Written as a file rather than `python -c` because this shell mangles inline
quotes.

    python check_env.py
"""

import importlib

for name in ["fastapi", "uvicorn", "multipart", "starlette", "pydantic"]:
    try:
        mod = importlib.import_module(name)
        version = getattr(mod, "__version__", "unknown")
        print("%-12s ok      %s" % (name, version))
    except ImportError as exc:
        print("%-12s MISSING %s" % (name, exc))
