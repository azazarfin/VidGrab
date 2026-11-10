"""One synchronous download attempt, without an ASGI app or database startup."""

import json
import logging
import sys
import threading

from backend.engine.worker_owner import BOOTSTRAP_LIMIT, read_message, send_message


def main():
    bootstrap = read_message(sys.stdin.buffer, BOOTSTRAP_LIMIT)
    if bootstrap is None:
        return
    stopped = threading.Event()

    def control():
        sys.stdin.buffer.readline(BOOTSTRAP_LIMIT + 1)
        stopped.set()

    threading.Thread(target=control, daemon=True).start()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        if bootstrap.get("fixture_only"):
            from backend.engine.network_guard import fixture_network_guard
            sys.addaudithook(fixture_network_guard)
        from backend.downloader import run_download
        from backend.runtime import RuntimeConfig
        if stopped.is_set():
            raise RuntimeError("Worker stopped before execution")
        options = dict(bootstrap["options"])
        options.update(
            runtime=RuntimeConfig(**bootstrap["runtime"]), components=bootstrap.get("components"),
            is_cancelled_cb=stopped.is_set,
            progress_callback=lambda _, data: send_message({"type": "progress", "data": data}),
        )
        result = run_download(**options)
        send_message({"type": "result", "data": result})
    except Exception as error:
        send_message({"type": "error", "message": str(error)[:4096]})


if __name__ == "__main__":
    main()
