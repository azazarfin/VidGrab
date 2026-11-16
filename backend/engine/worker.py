"""One synchronous download attempt, without an ASGI app or database startup."""

import json
import logging
import sys
import threading

from backend.engine.worker_owner import BOOTSTRAP_LIMIT, read_message, send_message
from backend.services.diagnostics import redact_diagnostic


def main():
    control_input = getattr(sys.stdin.buffer, "raw", sys.stdin.buffer)
    bootstrap = read_message(control_input, BOOTSTRAP_LIMIT)
    if bootstrap is None:
        return
    stopped = threading.Event()

    def control():
        control_input.read(1)
        stopped.set()

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        if bootstrap.get("fixture_only"):
            from backend.engine.network_guard import fixture_network_guard
            sys.addaudithook(fixture_network_guard)
        from backend.engine.process_identity import ProcessIdentity
        send_message({"type": "identity", "identity": ProcessIdentity.capture().to_json()})
        if read_message(control_input) != {"type": "execute"}:
            raise RuntimeError("Worker execution was not acknowledged by its private owner")
        threading.Thread(target=control, daemon=True).start()
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
        from backend.services.resume import requires_fresh_auth
        reply = {"type": "error", "message": redact_diagnostic(error)[:4096]}
        if requires_fresh_auth(error):
            reply["code"] = "AUTH_REQUIRED"
            reply["message"] = "Fresh browser authentication is required"
        else:
            from backend.runtime import OutputPathError, RuntimeOwnershipError
            from backend.services.playlists import PlaylistError
            if isinstance(error, (OutputPathError, RuntimeOwnershipError)):
                reply["code"] = "OWNERSHIP_REJECTED"
            elif isinstance(error, PlaylistError):
                reply["code"] = "PLAYLIST_REJECTED"
            else:
                import time
                from backend.services.retry_policy import REQUEST_CODES, RequestFailure, decide, nested_errors
                classified = next((value for value in nested_errors(error) if isinstance(value, RequestFailure)), None)
                decision = decide(error, 0, 1, time.time())
                if classified is None and decision.code in REQUEST_CODES:
                    classified = RequestFailure(decision.code, retry_at=decision.retry_at)
                if classified is not None:
                    reply["message"] = classified.code
                    reply["request_failure"] = {"code": classified.code, "retry_at": classified.retry_at,
                                                "consumes_retry": classified.consumes_retry}
        send_message(reply)


if __name__ == "__main__":
    main()
