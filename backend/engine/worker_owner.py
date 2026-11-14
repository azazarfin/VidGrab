"""Private per-attempt owner; control EOF means the engine stopped owning us."""

import ctypes
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time

from backend.services.job_state import ACTIVE
from backend.services.artifacts import result_artifacts

MESSAGE_LIMIT = 65536
BOOTSTRAP_LIMIT = 262144


def validate_worker_message(message):
    if not isinstance(message, dict):
        raise ValueError("Worker messages must be objects")
    kind = message.get("type")
    data = message.get("data")
    if kind == "progress":
        status = data.get("status") if isinstance(data, dict) else None
        if not isinstance(status, str) or status not in ACTIVE:
            raise ValueError("Invalid worker progress state")
    elif kind == "result":
        result_artifacts(data)
    elif kind == "error":
        error = message.get("message")
        if not isinstance(error, str) or len(error) > 4096:
            raise ValueError("Invalid worker error message")
    else:
        raise ValueError("Invalid worker response")


def read_message(stream, limit=MESSAGE_LIMIT):
    line = stream.readline(limit + 1)
    if not line:
        return None
    if len(line) > limit or not line.endswith(b"\n"):
        raise ValueError("Worker message exceeds its framing limit")
    message = json.loads(line)
    if not isinstance(message, dict):
        raise ValueError("Worker messages must be objects")
    return message


def send_message(message):
    data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
    if len(data) > MESSAGE_LIMIT:
        raise ValueError("Worker message exceeds its framing limit")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


class ProcessTree:
    def __init__(self, command, log):
        self.job = None
        if sys.platform == "linux":
            # Adopt the attempt's orphaned media processes so stop can reap them.
            if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
                raise OSError(ctypes.get_errno(), "Unable to become worker subreaper")
        elif sys.platform == "win32":
            from backend.engine.windows_job import WindowsJob
            self.job = WindowsJob()
        else:
            raise RuntimeError("Owned workers currently support Linux and Windows")
        self.process = None
        try:
            self.process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                close_fds=True, start_new_session=sys.platform == "linux",
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            if self.job:
                self.job.assign(self.process)
        except BaseException:
            if self.process:
                self.process.kill()
                self.process.wait()
            if self.job:
                self.job.close()
            raise

    def stop(self):
        process = self.process
        try:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()  # Cooperative stop, observed by the worker's input reader.
            if process.poll() is None:
                try:
                    process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    pass
            if self.job:
                self.job.stop()
            else:
                # Always signal the group: an exited leader may leave live FFmpeg.
                for signum in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(process.pid, signum)
                    except ProcessLookupError:
                        break
                    if signum == signal.SIGTERM:
                        time.sleep(0.05)
            process.wait(timeout=3)
            if sys.platform == "linux":
                deadline = time.monotonic() + 1
                while True:
                    try:
                        pid, _ = os.waitpid(-process.pid, os.WNOHANG)
                    except ChildProcessError:
                        break
                    if pid == 0:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Worker process group has not exited")
                        time.sleep(0.01)
        finally:
            if self.job:
                self.job.close()
            if process.stdout:
                process.stdout.close()


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    if len(arguments) < 2 or arguments[0] != "--" or not os.path.isabs(arguments[1]):
        raise ValueError("Use -- followed by an absolute trusted worker command")
    # Raw input avoids daemon-reader BufferedReader locks at interpreter exit,
    # and avoids reading ahead past the bootstrap into a batched stop request.
    control_input = getattr(sys.stdin.buffer, "raw", sys.stdin.buffer)
    bootstrap = read_message(control_input, BOOTSTRAP_LIMIT)
    if bootstrap is None:
        return
    stopping = threading.Event()
    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, lambda *_: stopping.set())
    messages = queue.Queue(maxsize=16)
    tree = None

    def control():
        # Any second message or EOF requests stop. The private pipe is never inherited.
        control_input.read(1)
        stopping.set()

    def output():
        try:
            while True:
                message = read_message(tree.process.stdout)
                messages.put(message)
                if message is None:
                    return
        except Exception as error:
            messages.put({"type": "error", "message": str(error)})

    terminal = None
    tree_stopped = False
    try:
        with tempfile.TemporaryFile() as log:
            tree = ProcessTree(arguments[1:], log)
            options = bootstrap.get("options", {})
            ownership = options.pop("ownership", None)
            if ownership is not None:
                from backend.db.ownership import register_worker
                from backend.engine.process_identity import ProcessIdentity
                from backend.runtime import RuntimeConfig
                if not isinstance(ownership, dict) or ownership.keys() != {"attempt_id", "owner_id"}:
                    raise ValueError("Invalid durable worker ownership")
                register_worker(RuntimeConfig(**bootstrap["runtime"]), ownership["attempt_id"], ownership["owner_id"],
                                options["task_id"], options["attempt_dir"], ProcessIdentity.capture(),
                                ProcessIdentity.capture(tree.process.pid))
            send_message({"type": "started", "pid": tree.process.pid})
            tree.process.stdin.write((json.dumps(bootstrap, ensure_ascii=False) + "\n").encode("utf-8"))
            tree.process.stdin.flush()
            threading.Thread(target=control, daemon=True).start()
            threading.Thread(target=output, daemon=True).start()
            while not stopping.is_set():
                try:
                    message = messages.get(timeout=0.02)
                except queue.Empty:
                    if not tree_stopped and tree.process.poll() is not None:
                        # A descendant can hold stdout open after its leader
                        # exits with a partial frame. Reap the tree to obtain EOF.
                        tree.stop()
                        tree_stopped = True
                    continue
                if message is None:
                    terminal = {"type": "error", "message": "Worker exited without a result"}
                    break
                kind = message.get("type")
                try:
                    validate_worker_message(message)
                except ValueError as error:
                    terminal = {"type": "error", "message": str(error)}
                    break
                if kind == "progress":
                    send_message(message)
                elif kind in {"result", "error"}:
                    terminal = message
                    if kind == "result":
                        try:
                            code = tree.process.wait(timeout=3)
                            if code != 0:
                                terminal = {"type": "error", "message": f"Worker exited with code {code}"}
                        except subprocess.TimeoutExpired:
                            terminal = {"type": "error", "message": "Worker did not exit after its result"}
                    break
            if not tree_stopped:
                tree.stop()
            tree = None
            if terminal and not stopping.is_set():
                send_message(terminal)
    finally:
        if tree and not tree_stopped:
            tree.stop()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    main()
