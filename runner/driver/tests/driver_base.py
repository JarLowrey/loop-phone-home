"""What every driver test file shares: the fixture config, the driver's modules on the path, a slot that is never made on
disk, and DriverTest, whose tests get a temp folder for the driver's files with the phone and the log replaced.

The config is tests/fixtures/worker.json, whose project is tests/fixtures/project: a project laid out under game/, so
every project key is exercised. Nothing here starts a session, calls ntfy or pushes."""
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
os.environ["WORKER_CONFIG"] = str(FIXTURES / "worker.json")
sys.path.insert(0, str(HERE.parent))
import commands as K  # noqa: E402
import core as C  # noqa: E402
import monitor as MO  # noqa: E402
import slots as SL  # noqa: E402

# why: DriverTest replaces print, so a test of text a function prints has to put the real one back
REAL_PRINT = print
# why: a test may run while a live driver holds the real slots folder, and no test may read or claim that one
TEST_SLOTS = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
SL.SLOTS_DIR = pathlib.Path(TEST_SLOTS.name) / "slots"


def fake_slot(name="1", root=None, base="base0"):
    """A slot that is never made on disk: its root is a folder the test chooses."""
    slot = SL.Slot(name)
    slot.root = pathlib.Path(root) if root else slot.root
    slot.base = base
    return slot


class DriverTest(unittest.TestCase):
    """Every test gets a temp folder for the driver's files, and the phone and the log are replaced."""

    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp())
        self.posts = []
        patches = [
            mock.patch.object(C, "LOGS", self.temp),
            mock.patch.object(C, "DRIVER_STATE", self.temp / "driver.json"),
            mock.patch.object(C, "DRIVER_LOG", self.temp / "driver.log"),
            mock.patch.object(C, "REQUEST_FILE", self.temp / "driver-request.txt"),
            mock.patch.object(C, "SEEN", self.temp / "seen.txt"),
            mock.patch.object(C, "USAGE_LOG", self.temp / "usage.jsonl"),
            mock.patch.object(SL, "SLOTS_DIR", self.temp / "slots"),
            mock.patch.object(SL, "LEASES", self.temp / "leases.json"),
            mock.patch.object(MO, "HISTORY", self.temp / "monitor-history.jsonl"),
            mock.patch.object(MO, "LOCK", self.temp / "monitor.lock"),
            mock.patch.object(C, "post", lambda title, body, priority="default", actions="":
                              self.posts.append((title, body, actions))),
            mock.patch("builtins.print"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        C.RUNTIME.clear()
        C.RUNTIME.update(state={})
        C.ctx().clear()

    def phone(self, *texts):
        """handle_commands with these phone messages and no PC requests."""
        messages = [(f"id{index}", text) for index, text in enumerate(texts)]
        with mock.patch.object(K, "poll_messages", return_value=messages):
            K.handle_commands(C.RUNTIME["state"])

    def pc(self, *texts):
        C.REQUEST_FILE.write_text("\n".join(texts) + "\n", encoding="utf-8")
        with mock.patch.object(K, "poll_messages", return_value=[]):
            K.handle_commands(C.RUNTIME["state"])
