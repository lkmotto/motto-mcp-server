import sys as _sys, pathlib as _pathlib  # noqa: E402
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent.parent))
import sentry_init  # noqa: E402,F401

from servers.cloudflare.server import mcp

if __name__ == "__main__":
    import sentry_sdk as _sentry_sdk
    try:
        mcp.run()
    except Exception as _exc:
        _sentry_sdk.capture_exception(_exc)
        raise

