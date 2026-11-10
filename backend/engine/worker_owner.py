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

MESSAGE_LIMIT = 65536
BOOTSTRAP_LIMIT = 262144


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
    bootstrap = read_message(sys.stdin.buffer, BOOTSTRAP_LIMIT)
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
        sys.stdin.buffer.readline(BOOTSTRAP_LIMIT + 1)
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
    try:
        with tempfile.TemporaryFile() as log:
            tree = ProcessTree(arguments[1:], log)
            send_message({"type": "started", "pid": tree.process.pid})
            tree.process.stdin.write((json.dumps(bootstrap, ensure_ascii=False) + "\n").encode("utf-8"))
            tree.process.stdin.flush()
            threading.Thread(target=control, daemon=True).start()
            threading.Thread(target=output, daemon=True).start()
            while not stopping.is_set():
                try:
                    message = messages.get(timeout=0.02)
                except queue.Empty:
                    continue
                if message is None:
                    terminal = {"type": "error", "message": "Worker exited without a result"}
                    break
                kind = message.get("type")
                if kind == "progress" and isinstance(message.get("data"), dict):
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
                else:
                    terminal = {"type": "error", "message": "Invalid worker response"}
                    break
            tree.stop()
            tree = None
            if terminal and not stopping.is_set():
                send_message(terminal)
    finally:
        if tree:
            tree.stop()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    main()
