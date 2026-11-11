"""The legacy-compatible lifecycle vocabulary for the P03 worker boundary."""

TERMINAL = frozenset({"complete", "failed", "cancelled"})
ACTIVE = frozenset({"probing", "downloading", "postprocessing"})
RUNNABLE = ACTIVE | {"queued"}
FINISH_FROM = {
    "complete": RUNNABLE,
    "failed": RUNNABLE,
    "queued": RUNNABLE,
    "cancelled": frozenset({"cancelling"}),
}


def accepts_progress(status):
    return status not in TERMINAL and status != "cancelling"
