"""The legacy-compatible lifecycle vocabulary for the P03 worker boundary."""

TERMINAL = frozenset({"complete", "failed", "cancelled"})
ACTIVE = frozenset({"probing", "downloading", "postprocessing"})
RUNNABLE = ACTIVE | {"queued"}
FINISH_FROM = {
    "complete": RUNNABLE | {"pausing"},
    "failed": RUNNABLE | {"pausing"},
    "queued": RUNNABLE,
    "paused": RUNNABLE | frozenset({"pausing", "scheduled", "retry_wait"}),
    "cancelled": frozenset({"cancelling", "pausing"}),
    "waiting_for_auth": RUNNABLE | {"pausing"},
    "retry_wait": RUNNABLE,
}


def accepts_progress(status):
    return status not in TERMINAL and status not in (
        "cancelling", "pausing", "paused", "scheduled", "waiting_for_auth", "retry_wait"
    )
