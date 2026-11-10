#!/usr/bin/env python3
"""
ModDeck desktop shell.

Runs the same local backend as server.py, but shows it in a native window using
Edge WebView2 (via pywebview) instead of a browser tab. This is what the packaged
ModDeck.exe runs.

    python desktop.py            # native window (if pywebview is installed)
    python desktop.py --debug    # opens WebView2 devtools

If pywebview is not installed it falls back to the Edge app window, so the .bat
workflow keeps working exactly as before.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from pathlib import Path


def _write_crash() -> None:
    """Leave a breadcrumb when the windowed exe fails before it can log."""
    try:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ModDeck"
        base.mkdir(parents=True, exist_ok=True)
        (base / "crash.log").write_text(traceback.format_exc(), encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    import server

    try:
        srv, port = server.build_server(7319)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{port}/"
    try:
        server.PORT_PATH.write_text(str(port), encoding="utf-8")
    except Exception:
        pass
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    server.log(f"desktop shell -> {url}")

    try:
        import webview
    except ImportError:
        server.log("pywebview not available; falling back to the Edge app window")
        proc = server.open_window(url)
        try:
            if proc is not None:
                proc.wait()
            else:
                while True:
                    time.sleep(1)
        except (KeyboardInterrupt, Exception):
            pass
        finally:
            srv.shutdown()
        return 0

    window = webview.create_window(
        server.APP_NAME,
        url,
        width=1500,
        height=980,
        min_size=(1024, 640),
        background_color="#0a0e13",
        text_select=True,
        confirm_close=False,
    )

    def quit_app(*_args):
        """Called from the UI's Quit button."""
        try:
            window.destroy()
        except Exception:
            pass
        return True

    window.expose(quit_app)
    try:
        webview.start(
            debug="--debug" in sys.argv,
            private_mode=False,
            storage_path=str(server.DATA_DIR / "webview"),
        )
    except Exception as exc:
        # Missing WebView2 runtime or a pywebview backend problem: keep the app
        # usable by falling back to the Edge app window.
        server.log(f"pywebview failed ({exc}); falling back to the Edge app window")
        server.open_window(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    finally:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        _write_crash()
        raise
