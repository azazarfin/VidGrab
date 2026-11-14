"""A Windows launcher must not create children before Job Object assignment."""

import io
from types import SimpleNamespace
from unittest.mock import patch

from tests.support import OfflineTestCase


class WorkerStartupTests(OfflineTestCase):
    def test_executing_identity_requires_membership_in_the_exact_windows_job(self):
        from backend.engine import worker_owner
        from backend.engine.process_identity import ProcessIdentity
        self.assertTrue(callable(getattr(worker_owner, "executing_identity", None)),
                        "The venv interpreter needs a verified private startup identity")
        actual = ProcessIdentity.capture()
        tree = SimpleNamespace(process=SimpleNamespace(pid=10), job=SimpleNamespace(contains=lambda pid: False))
        with self.assertRaisesRegex(RuntimeError, "worker|Job"):
            worker_owner.executing_identity(tree, {"type": "identity", "identity": actual.to_json()})
        tree.job.contains = lambda pid: pid == actual.pid
        self.assertEqual(worker_owner.executing_identity(tree, {"type": "identity", "identity": actual.to_json()}), actual)

    def test_linux_execution_identity_must_equal_the_created_worker_process(self):
        from backend.engine import worker_owner
        from backend.engine.process_identity import ProcessIdentity
        self.assertTrue(callable(getattr(worker_owner, "executing_identity", None)))
        actual = ProcessIdentity.capture()
        tree = SimpleNamespace(process=SimpleNamespace(pid=actual.pid+1), job=None)
        with self.assertRaisesRegex(RuntimeError, "worker|identity"):
            worker_owner.executing_identity(tree, {"type": "identity", "identity": actual.to_json()})

    def fixture(self, *, resume_error=False):
        events = []
        test = self

        class Process:
            pid = 10

            def __init__(self, *args, **options):
                self.stdin, self.stdout = io.BytesIO(), io.BytesIO()
                self.started = not bool(options["creationflags"] & 4)
                test.assertFalse(self.started, "An eager launcher can create children before the Job Object owns it")
                test.assertTrue(options["creationflags"] & 8, "Private workers need no implicit console process")
                events.append("suspended")

            def kill(self):
                events.append("killed")

            def wait(self, **options):
                events.append("joined")
                return 0

        class Job:
            def assign(self, process):
                test.assertFalse(process.started)
                events.append("assigned")

            def resume(self, process):
                test.assertIn("assigned", events)
                events.append("resumed")
                if resume_error:
                    raise OSError("resume failed")
                process.started = True

            def close(self):
                events.append("closed")

            def stop(self):
                events.append("job_stopped")

        return events, Process, Job

    def test_windows_worker_is_assigned_before_its_primary_thread_resumes(self):
        from backend.engine.worker_owner import ProcessTree
        events, process, job = self.fixture()
        with patch("backend.engine.worker_owner.sys.platform", "win32"), \
                patch("backend.engine.worker_owner.subprocess.CREATE_NO_WINDOW", 0x08000000, create=True), \
                patch("backend.engine.worker_owner.subprocess.Popen", process), \
                patch("backend.engine.windows_job.WindowsJob", job):
            tree = ProcessTree(["owned-worker"], io.BytesIO())
        self.assertEqual(events, ["suspended", "assigned", "resumed"])
        tree.process.stdin.close()
        tree.process.stdout.close()

    def test_failed_thread_resume_joins_the_suspended_process_and_closes_its_pipes(self):
        from backend.engine.worker_owner import ProcessTree
        events, process, job = self.fixture(resume_error=True)
        created = []

        def launch(*args, **options):
            result = process(*args, **options)
            created.append(result)
            return result

        with patch("backend.engine.worker_owner.sys.platform", "win32"), \
                patch("backend.engine.worker_owner.subprocess.CREATE_NO_WINDOW", 0x08000000, create=True), \
                patch("backend.engine.worker_owner.subprocess.Popen", launch), \
                patch("backend.engine.windows_job.WindowsJob", job):
            with self.assertRaisesRegex(OSError, "resume failed"):
                ProcessTree(["owned-worker"], io.BytesIO())
        self.assertEqual(events, ["suspended", "assigned", "resumed", "killed", "joined", "job_stopped", "closed"])
        self.assertTrue(created[0].stdin.closed)
        self.assertTrue(created[0].stdout.closed)
