"""Tests for the slots, the scheduler, the leases and the task hook. Run from the driver's folder:
python -m unittest discover -s tests -p test_slots.py

The slot tests make a real git repository in a temp folder, with real worktrees, and land work on it. Nothing here
starts a session, calls ntfy or pushes. The project is the fixture config's: laid out under game/, with res:// paths
on Files lines (files_strip), a status file, and slot_files that give each slot its own user folder."""
import contextlib
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from driver_base import DriverTest, fake_slot  # noqa: E402
import budget as B  # noqa: E402
import commands as K  # noqa: E402
import core as C  # noqa: E402
import leases as L  # noqa: E402
import mainloop as M  # noqa: E402
import runners as RN  # noqa: E402
import scheduler as SC  # noqa: E402
import sessions as X  # noqa: E402
import slots as SL  # noqa: E402
import task_hook as H  # noqa: E402
import tasks as T  # noqa: E402

TASKS_MD = """# Tasks

## How to read a task
- read it

## M3

### [ ] M3-01 · First
**Files.** Change `src/a.gd`.

**Done when.** test `test_a`

### [ ] M3-02 · Second
**Files.** Change `src/b.gd`.

**Done when.** test `test_b`

### [ ] M3-03 · Third
**Files.** Change `src/a.gd`.

**Done when.** test `test_c`
"""
PROGRESS_MD = """# Progress

| Date | Task | Result | Tests | Notes |
|---|---|---|---|---|
| 2026-10-26 | M2-01 | done | 1 passed, 0 failed | x |

## Blocked

(task id: what failed, what was tried)
"""
A_GD = "extends RefCounted\n\nsignal one\n\n\nfunc f() -> void:\n\tpass\n"
ITEMS_CSV = "id,text\nstart,Start here\n"
PASSING = "3 passed, 0 failed\nsmoke: PASS\n"
WORLD = "game/src/core/world.gd"


def git(root, *args):
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr}")
    return done.stdout.strip()


class RepoTest(DriverTest):
    """A main tree on branch main, with the files the driver marks, and slots in a temp folder."""

    def setUp(self):
        super().setUp()
        self.main = self.temp / "main"
        for path, text in {"loop/TASKS.md": TASKS_MD, "loop/PROGRESS.md": PROGRESS_MD,
                           "game/src/a.gd": A_GD, "game/src/b.gd": "extends Node\n",
                           "game/content/text/items.csv": ITEMS_CSV,
                           "game/.gitignore": ".godot/\n"}.items():
            (self.main / path).parent.mkdir(parents=True, exist_ok=True)
            (self.main / path).write_text(text, encoding="utf-8", newline="\n")
        git(self.main, "init", "-q", "-b", "main")
        git(self.main, "config", "user.email", "t@example.com")
        git(self.main, "config", "user.name", "t")
        git(self.main, "config", "core.autocrlf", "false")
        git(self.main, "add", "-A")
        git(self.main, "commit", "-q", "-m", "start")
        patches = [mock.patch.object(C, "PROJECT", self.main), mock.patch.object(SL, "SLOTS_DIR", self.temp / "slots"),
                   mock.patch.object(SL, "LEASES", self.temp / "leases.json"), mock.patch.object(SL, "sync"),
                   mock.patch.object(C, "test_output", return_value=PASSING)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        C.RUNTIME["branch"] = "main"
        self.addCleanup(self.remove_worktrees)

    def remove_worktrees(self):
        for root in (self.temp / "slots").glob("*"):
            subprocess.run(["git", "worktree", "remove", "-f", str(root)], cwd=self.main, capture_output=True)

    def slot(self, name="1"):
        slot = SL.Slot(name)
        SL.prepare(slot)
        return slot

    def work(self, slot, path, text, task_id, scope=("game/src",)):
        """The task writes path in its slot, and its work is committed there as a passing task's would be."""
        (slot.root / path).write_text(text, encoding="utf-8", newline="\n")
        SL.commit_work(slot, f"{task_id}: work\n", list(scope))

    def land(self, slot, task_id, test=True):
        return SL.integrate(slot, f"{task_id}: work\n", T.done_marks(task_id, f"| 2026-10-27 | {task_id} | done |"),
                            test=test)

    def main_text(self, path):
        return git(self.main, "show", f"HEAD:{path}")

    def commit_rules(self):
        (self.main / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
        git(self.main, "add", "-A")
        git(self.main, "commit", "-q", "-m", "rules")


class LandingTest(RepoTest):
    def test_two_slots_that_change_different_files_both_land(self):
        one, two = self.slot("1"), self.slot("2")
        self.work(one, "game/src/a.gd", A_GD + "\n\nfunc g() -> void:\n\tpass\n", "M3-01")
        self.work(two, "game/src/b.gd", "extends Node\n\nvar x := 1\n", "M3-02")
        self.assertEqual(self.land(one, "M3-01"), "")
        C.test_output.reset_mock()
        self.assertEqual(self.land(two, "M3-02"), "")
        self.assertIn("func g()", self.main_text("game/src/a.gd"))
        self.assertIn("var x := 1", self.main_text("game/src/b.gd"))
        self.assertIn("### [x] M3-01", self.main_text("loop/TASKS.md"))
        self.assertIn("### [x] M3-02", self.main_text("loop/TASKS.md"))
        self.assertIn("var x := 1", (self.main / "game/src/b.gd").read_text(encoding="utf-8"))
        C.test_output.assert_called_once()

    def test_the_status_file_lands_with_the_marks(self):
        self.assertIn(C.STATUS_REL, SL.BOOKKEEPING)
        one = self.slot("1")
        self.work(one, "game/src/b.gd", "extends Node\n\nvar x := 1\n", "M3-02")
        self.assertEqual(self.land(one, "M3-02"), "")
        status = self.main_text(C.STATUS_REL)
        self.assertIn("## Left to do (2)", status)
        self.assertIn("**M3-01** · First  ← next", status)
        self.assertIn("- 2026-10-27 · M3-02", status)
        self.assertIn(C.STATUS_REL, git(self.main, "show", "--name-only", "--format=", "HEAD"))

    def test_two_signals_added_at_one_place_both_land(self):
        one, two = self.slot("1"), self.slot("2")
        self.work(one, "game/src/a.gd", A_GD.replace("signal one\n", "signal one\nsignal two\n"), "M3-01")
        self.work(two, "game/src/a.gd", A_GD.replace("signal one\n", "signal one\nsignal three\n"), "M3-03")
        self.assertEqual(self.land(one, "M3-01"), "")
        self.assertEqual(self.land(two, "M3-03"), "")
        self.assertIn("signal one\nsignal two\nsignal three\n", self.main_text("game/src/a.gd"))

    def test_two_slots_appending_the_same_keyed_row_both_land(self):
        one, two = self.slot("1"), self.slot("2")
        path = "game/content/text/items.csv"
        self.work(one, path, ITEMS_CSV + "menu.open,Open the menu\n", "M3-01", ["game/content"])
        self.work(two, path, ITEMS_CSV + "menu.open,Open the menu\n", "M3-03", ["game/content"])
        self.assertEqual(self.land(one, "M3-01"), "")
        self.assertEqual(self.land(two, "M3-03"), "")
        self.assertEqual(self.main_text(path).count("menu.open,Open the menu"), 1)

    def test_a_changed_line_conflict_does_not_land(self):
        one, two = self.slot("1"), self.slot("2")
        self.work(one, "game/src/a.gd", A_GD.replace("signal one", "signal uno"), "M3-01")
        self.work(two, "game/src/a.gd", A_GD.replace("signal one", "signal eins"), "M3-03")
        self.assertEqual(self.land(one, "M3-01"), "")
        reason = self.land(two, "M3-03")
        self.assertIn("conflict", reason)
        self.assertIn("signal uno", self.main_text("game/src/a.gd"))
        self.assertNotIn("[x] M3-03", self.main_text("loop/TASKS.md"))
        self.assertFalse((pathlib.Path(git(two.root, "rev-parse", "--git-dir")) / "rebase-merge").exists())

    def test_a_name_both_slots_declared_does_not_land(self):
        one, two = self.slot("1"), self.slot("2")
        self.work(one, "game/src/a.gd", A_GD.replace("signal one\n", "signal one\nsignal hit\n"), "M3-01")
        self.work(two, "game/src/a.gd", A_GD + "\nsignal hit\n", "M3-03")
        self.assertEqual(self.land(one, "M3-01"), "")
        reason = self.land(two, "M3-03")
        self.assertIn("hit twice", reason)
        self.assertEqual(self.main_text("game/src/a.gd").count("signal hit"), 1)

    def test_a_failed_test_run_on_the_rebased_tree_does_not_land(self):
        one, two = self.slot("1"), self.slot("2")
        self.work(one, "game/src/a.gd", A_GD + "\n\nfunc g() -> void:\n\tpass\n", "M3-01")
        self.work(two, "game/src/b.gd", "extends Node\n\nvar x := 1\n", "M3-02")
        self.assertEqual(self.land(one, "M3-01"), "")
        C.test_output.return_value = "2 passed, 1 failed\n"
        self.assertIn("1 failed", self.land(two, "M3-02"))
        self.assertNotIn("var x", self.main_text("game/src/b.gd"))

    def test_a_fail_marker_on_the_rebased_tree_does_not_land(self):
        one, two = self.slot("1"), self.slot("2")
        self.work(one, "game/src/a.gd", A_GD + "\n\nfunc g() -> void:\n\tpass\n", "M3-01")
        self.work(two, "game/src/b.gd", "extends Node\n\nvar x := 1\n", "M3-02")
        self.assertEqual(self.land(one, "M3-01"), "")
        C.test_output.return_value = "3 passed, 0 failed\nsmoke: FAIL the main scene did not load\n"
        self.assertIn("smoke: FAIL", self.land(two, "M3-02"))
        self.assertNotIn("var x", self.main_text("game/src/b.gd"))

    def test_owner_edits_in_the_main_tree_are_never_the_tasks(self):
        one = self.slot("1")
        (self.main / "loop" / "PLAN.md").write_text("the owner's plan\n", encoding="utf-8")
        (self.main / "game/src/a.gd").write_text(A_GD + "# owner\n", encoding="utf-8")
        (one.root / "game/src/b.gd").write_text("extends Node\n\nvar y := 2\n", encoding="utf-8")
        self.assertEqual(SL.task_changes(one.snap(), one.root), ["game/src/b.gd"])
        self.assertEqual(SL.loop_changes(one.snap(), set(), one.root), [])
        SL.commit_work(one, "M3-02: work\n", ["game/src"])
        self.assertEqual(self.land(one, "M3-02"), "")
        self.assertEqual((self.main / "loop" / "PLAN.md").read_text(encoding="utf-8"), "the owner's plan\n")
        self.assertTrue((self.main / "game/src/a.gd").read_text(encoding="utf-8").endswith("# owner\n"))
        self.assertIn("var y := 2", self.main_text("game/src/b.gd"))
        self.assertNotIn("PLAN.md", git(self.main, "show", "--name-only", "--format=", "HEAD"))

    def test_an_uncommitted_owner_edit_in_the_way_waits_then_lands(self):
        one = self.slot("1")
        self.work(one, "game/src/b.gd", "extends Node\n\nvar x := 1\n", "M3-02")
        tasks = self.main / "loop" / "TASKS.md"
        tasks.write_text(TASKS_MD + "\n### [ ] M3-04 · Owner's new task\n", encoding="utf-8", newline="\n")
        waits = []

        def owner_commits(seconds):
            waits.append(seconds)
            git(self.main, "commit", "-q", "-am", "owner: a new task")
        with mock.patch.object(C, "POLL_SECONDS", 1), mock.patch.object(SL.time, "sleep", owner_commits):
            self.assertEqual(self.land(one, "M3-02"), "")
        self.assertEqual(len(waits), 1)
        self.assertEqual(self.posts[0][0], "Driver waiting for the main tree")
        landed = self.main_text("loop/TASKS.md")
        self.assertIn("### [x] M3-02", landed)
        self.assertIn("M3-04 · Owner's new task", landed)

    def test_a_block_lands_its_mark_without_the_work(self):
        one = self.slot("1")
        (one.root / "game/src/b.gd").write_text("broken\n", encoding="utf-8")
        T.block(one, "M3-02", "Second", "tests fail", "worker_failed")
        self.assertIn("### [!] M3-02", self.main_text("loop/TASKS.md"))
        self.assertIn("- M3-02: tests fail", self.main_text("loop/PROGRESS.md"))
        self.assertIn("## Blocked (1)", self.main_text(C.STATUS_REL))
        self.assertEqual(self.main_text("game/src/b.gd"), "extends Node")
        self.assertEqual(C.RUNTIME["state"]["block_streak"][-1]["task"], "M3-02")

    def test_prepare_saves_and_clears_leftovers_and_gives_each_slot_its_own_user_folder(self):
        one = self.slot("1")
        (one.root / "game/src/b.gd").write_text("left over\n", encoding="utf-8")
        SL.prepare(one)
        self.assertEqual(C.changed_paths(one.root), set())
        self.assertEqual(list(self.temp.glob("revert-*/game/src/b.gd"))[0].read_text(encoding="utf-8"),
                         "left over\n")
        self.assertIn('custom_user_dir_name="game_slot_1"',
                      (one.root / "game" / "override.cfg").read_text(encoding="utf-8"))

    def test_a_requeued_task_runs_alone_after_max_requeues(self):
        one = self.slot("1")
        state = C.RUNTIME["state"]
        scheduler = SC.Scheduler(state)
        spec = C.find_spec("M3-02", TASKS_MD)
        for _ in range(C.MAX_REQUEUES):
            self.assertFalse(scheduler.solo("M3-02", spec))
            T.requeue(one, "M3-02", "a conflict", state)
        self.assertTrue(scheduler.solo("M3-02", spec))
        T.task_committed(state, "M3-02")
        self.assertFalse(scheduler.solo("M3-02", spec))


class RivalDriverTest(RepoTest):
    """One driver per slots folder, and none started from a slot or from a session the driver made."""

    def other_process(self):
        """A live process that is not this one, and its pid."""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return child

    def hold(self, pid):
        SL.SLOTS_DIR.mkdir(parents=True, exist_ok=True)
        SL.owner_file().write_text(str(pid), encoding="utf-8", newline="\n")

    def start_problem(self, root):
        """C.start_problem() for a driver started from root, with no session marker in the environment."""
        with mock.patch.dict(os.environ):
            os.environ.pop(C.SESSION_ENV, None)
            with mock.patch.object(C, "PROJECT", root):
                return C.start_problem()

    def test_a_live_driver_in_the_slots_is_a_rival(self):
        child = self.other_process()
        self.hold(child.pid)
        self.assertEqual(SL.other_owner(), child.pid)

    def test_this_process_and_its_parent_are_not_rivals(self):
        self.hold(os.getpid())
        self.assertEqual(SL.other_owner(), 0)
        self.hold(os.getppid())
        self.assertEqual(SL.other_owner(), 0)

    def test_a_driver_that_ended_is_not_a_rival(self):
        child = self.other_process()
        child.kill()
        child.wait()
        self.hold(child.pid)
        self.assertEqual(SL.other_owner(), 0)

    def test_a_pid_that_belongs_to_another_program_is_not_a_rival(self):
        command = ["cmd", "/c", "ping -n 30 127.0.0.1 > nul"] if os.name == "nt" else ["sleep", "30"]
        other = subprocess.Popen(command, stdout=subprocess.DEVNULL)
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        self.hold(other.pid)
        self.assertEqual(SL.other_owner(), 0)

    def test_a_shutdown_frees_the_slots(self):
        self.hold(os.getpid() + 1)
        with mock.patch.object(C, "kill_tree"), mock.patch.object(C, "refresh"), mock.patch.object(
                C, "SUPERVISOR_PID", self.temp / "supervisor.pid"), mock.patch.object(
                C, "DRIVER_PID", self.temp / "driver.pid"):
            K.cli_shutdown()
        self.assertFalse(SL.owner_file().exists())

    def test_a_claim_is_released_by_its_owner_only(self):
        SL.claim_slots()
        self.assertEqual(C.read_pid(SL.owner_file()), os.getpid())
        self.hold(os.getpid() + 1)
        SL.release_slots()
        self.assertTrue(SL.owner_file().exists())
        self.hold(os.getpid())
        SL.release_slots()
        self.assertFalse(SL.owner_file().exists())

    def test_a_second_supervisor_is_refused_and_claims_nothing(self):
        child = self.other_process()
        self.hold(child.pid)
        with mock.patch.object(C, "SUPERVISOR_PID", self.temp / "supervisor.pid"), mock.patch.object(
                C, "DRIVER_PID", self.temp / "driver.pid"), mock.patch.object(
                M.subprocess, "call", side_effect=AssertionError("a driver was started")):
            self.assertEqual(M.supervise(), M.ALREADY_RUNNING)
            self.assertFalse(C.SUPERVISOR_PID.exists())
        self.assertEqual(C.read_pid(SL.owner_file()), child.pid)

    def test_a_worktree_is_no_place_to_start_a_driver(self):
        slot = self.slot()
        self.assertIn("worktree", self.start_problem(slot.root))
        self.assertEqual(self.start_problem(self.main), "")

    def test_a_session_the_driver_started_may_not_start_one(self):
        self.assertEqual(C.helper_env()[C.SESSION_ENV], "1")
        with mock.patch.dict(os.environ, {C.SESSION_ENV: "1"}), mock.patch.object(C, "PROJECT", self.main):
            self.assertIn("session", C.start_problem())

    def test_an_argument_no_command_knows_is_refused_not_run_as_a_driver(self):
        problem = M.start_refusal(["--stauts"])
        self.assertIn("--stauts", problem)
        self.assertIn("unittest discover", problem)
        with mock.patch.dict(os.environ):
            os.environ.pop(C.SESSION_ENV, None)
            with mock.patch.object(C, "PROJECT", self.main):
                self.assertEqual(M.start_refusal([]), "")

    def test_the_alert_names_the_commit_the_slot_holds(self):
        slot = self.slot()
        self.work(slot, "game/src/a.gd", A_GD + "# why: x\n", "M3-01")
        self.assertTrue(RN.commit_made_during(slot, "M3-01"))
        self.assertIn("M3-01: work", self.posts[-1][1])


class MergeTest(unittest.TestCase):
    def test_additions_on_both_sides_are_both_kept(self):
        text = "a\n<<<<<<< HEAD\nsignal x\n||||||| base\n=======\nsignal y\n>>>>>>> M3-07\nb\n"
        self.assertEqual(SL.additive_merge(text), "a\nsignal x\nsignal y\nb\n")

    def test_an_identical_appended_row_is_kept_once(self):
        row = "menu.open,Open the menu\n"
        text = f"a\n<<<<<<< HEAD\n{row}||||||| base\n=======\n{row}>>>>>>> M3-07\n"
        self.assertEqual(SL.additive_merge(text, dedupe=True), f"a\n{row}")
        self.assertEqual(SL.additive_merge(text), f"a\n{row}{row}")

    def test_a_changed_line_is_not_merged(self):
        self.assertIsNone(SL.additive_merge("<<<<<<< a\nx\n||||||| b\nold\n=======\ny\n>>>>>>> c\n"))

    def test_doubled_names_are_found(self):
        folder = pathlib.Path(__file__).resolve().parent / "fixtures"
        self.assertEqual(SL.TOP_NAME.findall("@export var a := 1\nstatic func b():\n\tvar c\nsignal d(x: int)\n"),
                         ["a", "b", "d"])
        self.assertEqual(SL.duplicates(folder, []), [])


class KeyedDuplicatesTest(DriverTest):
    def write(self, path, text):
        file = self.temp / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")

    def test_a_key_twice_in_a_keyed_file_is_doubled(self):
        path = "game/content/text/items.csv"
        self.write(path, 'id,text\na,One\nb,"Two, more"\n')
        self.assertEqual(SL.duplicates(self.temp, [path]), [])
        self.write(path, "id,text\na,One\nb,Two\na,Again\n")
        self.assertEqual(SL.duplicates(self.temp, [path]), [f"{path}: a twice"])

    def test_a_name_twice_in_a_source_file_is_doubled_and_files_outside_the_scope_are_not_read(self):
        self.write("game/src/x.gd", "func a() -> void:\n\tpass\n\n\nfunc a() -> void:\n\tpass\n")
        self.assertEqual(SL.duplicates(self.temp, ["game/src/x.gd"]), ["game/src/x.gd: a twice"])
        self.write("notes/items.csv", "id,text\na,One\na,Two\n")
        self.assertEqual(SL.duplicates(self.temp, ["notes/items.csv", "game/src/missing.gd"]), [])


class LeaseTest(DriverTest):
    def test_a_lease_covers_its_file_its_folder_and_what_a_tool_makes_beside_it(self):
        self.assertTrue(L.covers("game/src/a.gd", "game/src/a.gd.uid"))
        self.assertTrue(L.covers("game/content/text/strings.csv", "game/content/text/strings.en.translation"))
        self.assertTrue(L.covers("game/src/", "game/src/core/x.gd"))
        self.assertFalse(L.covers("game/src/a.gd", "game/src/ab.gd"))

    def test_a_file_another_slot_holds_is_refused_unless_shared(self):
        path = self.temp / "leases.json"
        L.start(path, "1", "M3-01", ["game/src/a.gd"], ["game/src/shared.gd"])
        L.start(path, "2", "M3-02", ["game/src/b.gd"], ["game/src/shared.gd"])
        self.assertIn("M3-01", L.acquire(path, "2", "game/src/a.gd"))
        self.assertEqual(L.acquire(path, "2", "game/src/new.gd"), "")
        self.assertIn("M3-02", L.acquire(path, "1", "game/src/new.gd"))
        self.assertEqual(L.acquire(path, "1", "game/src/shared.gd"), "")
        self.assertEqual(L.overlap(L.read(path), ["game/src/b.gd", "game/src/shared.gd"]),
                         [("game/src/b.gd", "2", "M3-02")])
        L.release(path, "2")
        self.assertEqual(L.acquire(path, "1", "game/src/b.gd"), "")


class HookTest(DriverTest):
    """decide() with the arguments the driver gives the hook: the slot, the trees it must not touch, the test call."""

    def setUp(self):
        super().setUp()
        self.root = self.temp / "slots" / "1"
        self.other = self.temp / "slots" / "2"
        self.main = self.temp / "main"
        self.leases = self.temp / "leases.json"
        L.start(self.leases, "2", "M3-02", ["game/src/b.gd"], [])

    def args(self, *more):
        return H.parse(["--root", str(self.root), "--slot", "1", "--leases", str(self.leases),
                        "--forbid", str(self.main), "--forbid", str(self.other), "--logs", C.LOGS_REL,
                        f"--test-call={C.TEST_CALL.pattern}", "--partial=--only", "--full-timeout-ms=600000",
                        "--no-whole-read", C.TASKS_REL, "--no-whole-read", C.PROGRESS_REL, *more])

    def decide(self, tool, **given):
        return H.decide({"tool_name": tool, "tool_input": given}, self.args())

    def test_the_session_stays_in_its_own_slot(self):
        self.assertIn("cd is refused", self.decide("Bash", command="cd game && ls"))
        forward = str(self.main).replace("\\", "/")
        drive = "/" + forward[0].lower() + forward[2:]
        self.assertIn("another copy", self.decide("Bash", command=f"cat {forward}/loop/TASKS.md"))
        self.assertIn("another copy", self.decide("Bash", command=f"sed -i s/a/b/ {drive}/game/src/a.gd"))
        self.assertIn("another copy", self.decide("Write", file_path=str(self.main / "game" / "src" / "a.gd")))
        self.assertIn("another copy", self.decide("Edit", file_path=str(self.other / "x.gd")))

    def test_a_full_test_run_needs_a_long_timeout_and_a_partial_one_does_not(self):
        # why: a full suite run without a long Bash timeout is cut off and its output lost, so the hook sends it back
        full = "python game/tools/run_tests.py"
        self.assertEqual(self.decide("Bash", command=full), H.TIMEOUT_REASON.format(timeout=600000))
        self.assertEqual(self.decide("Bash", command=full, timeout=60000), H.TIMEOUT_REASON.format(timeout=600000))
        self.assertEqual(self.decide("Bash", command=full, timeout=600000), "")
        self.assertEqual(self.decide("Bash", command=full + " --only test_world"), "")
        self.assertEqual(self.decide("Bash", command="grep -n run_tests.py loop/LOOP.md"), "")
        no_check = H.decide({"tool_name": "Bash", "tool_input": {"command": full}}, self.args("--full-timeout-ms=0"))
        self.assertEqual(no_check, "")

    def test_a_no_op_cd_is_stripped(self):
        self.assertEqual(H.strip_root_cd("cd . && ls", str(self.root)), "ls")
        self.assertEqual(H.strip_root_cd("cd ./; git status", str(self.root)), "git status")
        self.assertEqual(H.strip_root_cd(f'cd "{self.root}" && ls', str(self.root)), "ls")
        self.assertIsNone(H.strip_root_cd("cd game && ls", str(self.root)))

    def test_a_read_only_cd_is_rewritten_and_a_writing_one_refused(self):
        rewritten = H.rewrite_cd("cd game; cat src/a.gd")
        self.assertEqual(rewritten, "(cd game && cat src/a.gd)")
        for writing in ("cd game; sed -i s/a/b/ src/a.gd", "cd game && python - <<'E'\nx\nE",
                        "cd loop; cat x > y"):
            self.assertIsNone(H.rewrite_cd(writing))
        event = {"tool_name": "Bash", "tool_input": {"command": rewritten}}
        self.assertEqual(H.decide(event, self.args(), allow_cd=True), "")
        self.assertIn("cd is refused", H.decide(event, self.args()))

    def test_a_whole_file_read_of_the_task_file_is_refused(self):
        self.assertEqual(self.decide("Read", file_path="loop/TASKS.md"), H.BIG_FILE_REASON.format(name="loop/TASKS.md"))
        self.assertEqual(self.decide("Read", file_path="C:\\x\\loop\\PROGRESS.md"),
                         H.BIG_FILE_REASON.format(name="loop/PROGRESS.md"))
        self.assertEqual(self.decide("Read", file_path="loop/TASKS.md", offset=9425, limit=60), "")
        self.assertEqual(self.decide("Read", file_path="loop/LOOP.md"), "")

    def test_writes_take_free_files_and_refuse_held_ones(self):
        self.assertEqual(self.decide("Edit", file_path="game/src/a.gd"), "")
        self.assertIn("game/src/a.gd", L.files_of(self.leases, "1"))
        refused = self.decide("Write", file_path=str(self.root / "game" / "src" / "b.gd"))
        self.assertIn("M3-02", refused)
        self.assertIn("stop now and say that you need", refused)
        self.assertEqual(L.refusals(self.leases, "1"), [("game/src/b.gd", "M3-02")])
        self.assertEqual(self.decide("Read", file_path=str(self.other / "x.gd")), "")
        self.assertEqual(self.decide("Write", file_path=C.LOGS_REL + "notes.txt"), "")
        self.assertNotIn(C.LOGS_REL + "notes.txt", L.files_of(self.leases, "1"))

    def test_past_the_deadline_every_call_is_refused(self):
        late = self.args(f"--deadline={time.time() - 1}")
        for tool, given in (("Read", {"file_path": "game/src/a.gd"}), ("Bash", {"command": "ls"})):
            self.assertEqual(H.decide({"tool_name": tool, "tool_input": given}, late), H.DEADLINE_REASON)
        early = self.args(f"--deadline={time.time() + 600}")
        self.assertEqual(H.decide({"tool_name": "Bash", "tool_input": {"command": "ls"}}, early), "")

    def test_the_hook_reads_the_arguments_the_driver_writes(self):
        slot = fake_slot("1", self.root)
        self.other.mkdir(parents=True)
        config = self.temp / "hook-1.json"
        config.write_text(json.dumps(X.hook_args(slot, 0.0)), encoding="utf-8")
        args = H.parse(["--config", str(config)])
        self.assertEqual(os.path.normcase(args.root), os.path.normcase(str(self.root)))
        self.assertEqual(args.slot, "1")

        def decide(tool, **given):
            return H.decide({"tool_name": tool, "tool_input": given}, args)
        project = str(C.PROJECT).replace("\\", "/")
        self.assertIn("another copy", decide("Bash", command=f"cat {project}/loop/TASKS.md"))
        self.assertIn("another copy", decide("Edit", file_path=str(self.other / "x.gd")))
        self.assertEqual(decide("Read", file_path=C.TASKS_REL), H.BIG_FILE_REASON.format(name=C.TASKS_REL))
        self.assertEqual(decide("Bash", command="python game/tools/run_tests.py"),
                         H.TIMEOUT_REASON.format(timeout=C.CFG.full_run_timeout_ms))
        self.assertEqual(decide("Edit", file_path="game/src/a.gd"), "")
        self.assertIn("game/src/a.gd", L.files_of(SL.LEASES, "1"))
        # what the command line gives wins over the file
        self.assertEqual(H.parse(["--config", str(config), "--slot", "9"]).slot, "9")


class SchedulerTest(DriverTest):
    TEXT = TASKS_MD + """
### [ ] M3-R · Review
**Who.** Opus

**Files.** Change any file under `game/src`.

**Done when.** file `loop/reviews/M3.md`

### [ ] M3-05 · Fifth
**After.** M3-02

**Files.** Change `src/c.gd`.

**Done when.** test `test_e`
"""

    def setUp(self):
        super().setUp()
        settings = [mock.patch.object(C, "PARALLEL_SLOTS", 3), mock.patch.object(C, "PARALLEL_WINDOW", 3),
                    mock.patch.object(C, "SHARED_FILES", [])]
        for setting in settings:
            setting.start()
            self.addCleanup(setting.stop)
        self.scheduler = SC.Scheduler(C.RUNTIME["state"])

    def running(self, *tasks, text=None):
        for number, task_id in enumerate(tasks, 1):
            self.scheduler.slots[str(number)].task = task_id
            self.scheduler.slots[str(number)].spec = C.find_spec(task_id, text or self.TEXT)
            L.start(SL.LEASES, str(number), task_id, SL.task_files(C.find_spec(task_id, text or self.TEXT)), [])

    def pick(self, text=None):
        found = self.scheduler.next_start(text or self.TEXT)
        return found[0] if found else None

    def test_tasks_start_in_file_order(self):
        self.assertEqual(self.pick(), "M3-01")
        self.running("M3-01")
        self.assertEqual(self.pick(), "M3-02")

    def test_a_task_that_shares_a_file_with_a_running_one_waits(self):
        self.running("M3-01", "M3-02")
        self.assertIsNone(self.pick())
        self.assertIn("M3-03: shares a.gd with M3-01", self.scheduler.wait_reason)
        with mock.patch.object(C, "SHARED_FILES", ["game/src/a.gd"]):
            L.start(SL.LEASES, "1", "M3-01", ["game/src/a.gd"], ["game/src/a.gd"])
            self.assertEqual(self.pick(), "M3-03")

    def test_an_opus_task_runs_alone(self):
        text = self.TEXT.replace("### [ ] M3-01", "### [x] M3-01").replace("### [ ] M3-03", "### [x] M3-03")
        self.running("M3-02", text=text)
        self.assertIsNone(self.pick(text))
        self.scheduler.slots["1"].task = None
        text = text.replace("### [ ] M3-02", "### [x] M3-02")
        self.assertEqual(self.pick(text), "M3-R")
        self.running("M3-R", text=text)
        self.assertIsNone(self.pick(text))
        self.assertIn("M3-R runs alone", self.scheduler.wait_reason)

    def test_an_escalation_drains_the_slots_then_runs_alone(self):
        C.RUNTIME["state"]["escalations"] = {"M3-03": {"from": "sonnet", "reason": "tests fail"}}
        self.running("M3-01")
        self.assertIsNone(self.pick(), "nothing new starts while a slot is busy")
        self.assertIn("M3-03 waits for every slot to be idle", self.scheduler.wait_reason)
        self.scheduler.slots["1"].task = None
        L.release(SL.LEASES, "1")
        self.assertEqual(self.pick(), "M3-03", "it goes before earlier open tasks")
        self.running("M3-03")
        self.assertIsNone(self.pick(), "and runs alone")

    def test_the_window_and_the_after_line(self):
        text = self.TEXT.replace("### [ ] M3-03", "### [x] M3-03").replace("**Who.** Opus\n", "")
        text = text.replace("Change any file under `game/src`", "Change `src/r.gd`")
        self.running("M3-01", "M3-02", "M3-R", text=text)
        self.assertIsNone(self.pick(text))
        self.assertIn("M3-05: outside the window", self.scheduler.wait_reason)
        with mock.patch.object(C, "PARALLEL_WINDOW", 5):
            self.assertIsNone(self.pick(text))
            self.assertIn("M3-05: waits for M3-02 to land", self.scheduler.wait_reason)
            self.scheduler.slots["2"].task = None
            L.release(SL.LEASES, "2")
            self.assertEqual(self.pick(text.replace("### [ ] M3-02", "### [x] M3-02")), "M3-05")

    def test_task_files_read_the_files_line(self):
        spec = ("**Files.** Change `src/core/bindings.gd`, `content/text/strings.csv`, `loop/PLAYTEST.md`,\n"
                "`game/tests/test_text_table.gd` and `res://project.godot`. Change any file under `game/src`.\n")
        self.assertEqual(SL.task_files(spec), ["game/src/core/bindings.gd",
                                              "game/content/text/strings.csv", "loop/PLAYTEST.md",
                                              "game/tests/test_text_table.gd", "game/project.godot",
                                              "game/src/"])
        self.assertEqual(SC.after("**After.** M3-01, M3-30\n**Files.** x M3-99\n"), ["M3-01", "M3-30"])

    def test_a_class_constant_on_the_files_line_is_no_file(self):
        spec = ("**Files.** Change `src/core/bindings.gd`: the `Bindings.LABELS` const, and `World.step` reads "
                "it.\n")
        self.assertEqual(SL.task_files(spec), ["game/src/core/bindings.gd"])

    def fake_root(self):
        root = self.temp / "root"
        for folder in ("docs", "notes", "loop", "game"):
            (root / folder).mkdir(parents=True)
        (root / "CLAUDE.md").write_text("x", encoding="utf-8")
        return root

    def test_task_files_keep_paths_from_the_project_root(self):
        spec = ("**Files.** Change `src/core/setup.gd`, `game/tools/run_tests.py`, `CLAUDE.md` and\n"
                "`docs/lib/index.ts`.\n")
        with mock.patch.object(C, "PROJECT", self.fake_root()):
            named = SL.task_files(spec)
            self.assertEqual(named, ["game/src/core/setup.gd", "game/tools/run_tests.py", "CLAUDE.md",
                                     "docs/lib/index.ts"])
            with mock.patch.object(SL, "task_changes", return_value=["CLAUDE.md", "docs/lib/index.ts"]):
                self.assertEqual(SL.outside_scope({}, C.SCOPE, None, named), [])
                self.assertEqual(SL.outside_scope({}, C.SCOPE, None, named[:2]), ["CLAUDE.md", "docs/lib/index.ts"])
            self.assertIn("CLAUDE.md", SL.landing_paths(spec))
            self.assertIn("game/tools/run_tests.py", SL.landing_paths(spec))

    def test_task_files_keep_what_exists_at_the_root(self):
        root = self.fake_root()
        spec = ("**Files.** `CLAUDE.md`, `docs/lib/x.ts`, `.gitattributes`, `notes/x.md`, `project.godot`, "
                "`src/x.gd`, `loop/notes/plan.md`\n")
        with mock.patch.object(C, "PROJECT", root):
            self.assertEqual(SL.task_files(spec), ["CLAUDE.md", "docs/lib/x.ts", ".gitattributes", "notes/x.md",
                                                   "game/project.godot", "game/src/x.gd", "loop/notes/plan.md"])

    def test_a_dotfile_is_a_root_file(self):
        root = self.fake_root()
        self.assertEqual(SL.task_files("**Files.** `.gitattributes`\n", root), [".gitattributes"])
        self.assertEqual(SL.task_files("**Files.** `.editorconfig`\n", root), [".editorconfig"])

    def test_a_mixed_files_line_is_a_fault(self):
        done = "\n\n**Done when.** test `test_a`\n"
        mixed = "**Files.** `res://src/a.gd`, `game/tests/test_a.gd`" + done
        self.assertIn("mixes res://", SL.precheck(mixed))
        self.assertEqual(SL.precheck("**Files.** `res://src/a.gd`, `res://tests/test_a.gd`" + done), "")
        self.assertEqual(SL.precheck("**Files.** `game/src/a.gd`, `game/tests/test_a.gd`" + done), "")


class PrecheckTest(DriverTest):
    def test_a_done_when_the_gate_cannot_see_is_refused(self):
        blind = {"bench:": "only tools/bench.py prints it"}
        with mock.patch.object(C.CFG, "blind_outputs", blind):
            self.assertEqual(SL.precheck("**Files.** Change `src/a.gd`.\n\n**Done when.** output `bench: run`\n"),
                             "Done when: output 'bench: run' is not printed by the gate's test run: only "
                             "tools/bench.py prints it")
            self.assertEqual(SL.precheck("**Files.** Change `src/a.gd`.\n\n**Done when.** output `smoke: PASS`\n"),
                             "")
        self.assertEqual(SL.precheck("**Done when.** test `test_x`\n"), "")
        self.assertIn("no **Done when.**", SL.precheck("**Files.** Change `src/a.gd`.\n"))
        self.assertIn("loop/NOTES.md", SL.precheck("**Done when.** file `loop/NOTES.md`\n"))
        named = "**Files.** Change `loop/NOTES.md`.\n\n**Done when.** file `loop/NOTES.md`\n"
        self.assertEqual(SL.precheck(named), "")
        # why: the generic driver knows no reviews folder, so a review's report in the loop folder is named like any
        review = "**Files.** Write `loop/reviews/M7-review.md`.\n\n**Done when.** file `loop/reviews/M7-review.md`\n"
        self.assertEqual(SL.precheck(review), "")
        self.assertEqual(SL.precheck("**Done when.** file `game/src/new.gd`\n"), "")


class BudgetTest(DriverTest):
    def test_sessions_side_by_side_reserve_their_caps(self):
        caps = {}

        def other():
            caps["other"] = B.session_budget(30.0)
        with mock.patch.object(C, "spend_on", return_value=70.0), mock.patch.object(C, "DAILY_BUDGET_USD", 120.0):
            thread = threading.Thread(target=other)
            thread.start()
            thread.join()
            self.assertEqual(caps["other"], 30.0)
            self.assertEqual(B.session_budget(30.0), 20.0)
            B.release_reservation()
            B.RESERVED.clear()
            self.assertEqual(B.session_budget(30.0), 30.0)
            B.release_reservation()


class RunnerTest(RepoTest):
    def test_work_that_cannot_land_is_requeued_not_escalated(self):
        slot = self.slot("1")
        state = C.RUNTIME["state"]
        with mock.patch.object(RN, "worker_attempt", return_value=RN.Requeue("a conflict")) as attempt, \
                mock.patch.object(RN.K, "approved", return_value=True):
            RN.do_worker_task(slot, "M3-02", "Second", C.find_spec("M3-02", TASKS_MD), state)
        attempt.assert_called_once()
        self.assertEqual(state["requeues"], {"M3-02": 1})
        self.assertIn("### [ ] M3-02", self.main_text("loop/TASKS.md"))

    def test_a_passing_task_lands_with_its_marks(self):
        slot = self.slot("1")
        (slot.root / "game/src/b.gd").write_text("extends Node\n\nvar z := 3\n", encoding="utf-8")
        failed = RN.land(slot, C.RUNTIME["state"], "M3-02", "Second", "M3-02: Second\n", ["game/src"],
                         "| 2026-10-27 | M3-02 | done | 3 passed, 0 failed | x |")
        self.assertEqual(failed, "")
        self.assertIn("### [x] M3-02", self.main_text("loop/TASKS.md"))
        self.assertIn("| M3-02 | done |", self.main_text("loop/PROGRESS.md"))
        self.assertIn("- 2026-10-26 · M2-01", self.main_text(C.STATUS_REL))
        self.assertEqual(git(self.main, "log", "-1", "--format=%s"), "M3-02: Second")
        self.assertEqual(json.loads(C.DRIVER_STATE.read_text(encoding="utf-8"))["monitor_pending"], ["M3-02"])

    def test_a_misnamed_done_when_test_requeues(self):
        self.commit_rules()
        slot = self.slot("1")
        spec = "### [ ] M3-02 · Second\n**Files.** Change `src/b.gd`.\n\n**Done when.** test `test_c`\n"

        def session(prompt, task_id, model, effort, turns, timeout, budget, kind, slot=None):
            (slot.root / "game/src/test_c.gd").write_text("func test_c():\n\tpass\n", encoding="utf-8",
                                                          newline="\n")
            return "done", ""

        def attempt(name):
            gate = (f"Done when: no test function {name} in the test files", "3 passed, 0 failed")
            with mock.patch.object(RN, "check_passed", return_value=gate), \
                    mock.patch.object(RN.X, "run_session", session), \
                    mock.patch.object(RN, "commit_made_during", return_value=False), \
                    mock.patch.object(B, "session_budget", return_value=5.0):
                return RN.worker_attempt(slot, "M3-02", "Second", spec, "sonnet", "", None)
        misplaced = attempt("test_c")
        self.assertIsInstance(misplaced, RN.Requeue)
        self.assertIn("test_c", misplaced)
        self.assertEqual(misplaced.wait_for, [])
        absent = attempt("test_zz")
        self.assertNotIsInstance(absent, RN.Requeue)
        self.assertIsInstance(absent, str)
        self.assertIn("no test function test_zz", absent)

    def test_a_misplaced_test_is_requeued_once_and_then_fails_like_any_other(self):
        slot = self.slot("1")
        body = "func test_c():" + chr(10) + chr(9) + "pass" + chr(10)
        (slot.root / "game/src/test_c.gd").write_text(body, encoding="utf-8", newline=chr(10))
        reason = "Done when: no test function test_c in the test files"
        first = RN.misplaced_requeue("M3-02", slot, reason)
        self.assertIsInstance(first, RN.Requeue)
        again = RN.misplaced_requeue("M3-02", slot, reason)
        self.assertNotIsInstance(again, RN.Requeue)
        self.assertEqual(again, "")
        self.assertIsInstance(RN.misplaced_requeue("M3-03", slot, reason), RN.Requeue, "another task has its own count")


class EndToEndTest(RepoTest):
    """The scheduler, the runners and the slots together, with a fake session in place of Claude."""

    def setUp(self):
        super().setUp()
        self.commit_rules()
        self.active = 0
        self.most = 0
        self.lock = threading.Lock()
        self.both_started = threading.Barrier(2, timeout=10)
        settings = [mock.patch.object(C, "PARALLEL_SLOTS", 2), mock.patch.object(C, "PARALLEL_WINDOW", 3),
                    mock.patch.object(C, "SHARED_FILES", []), mock.patch.object(C, "ESCALATE_MODEL", ""),
                    mock.patch.object(C, "SCOPE", ["game/src", "game/tests", "game/content"]),
                    mock.patch.object(RN.X, "run_session", self.session)]
        for setting in settings:
            setting.start()
            self.addCleanup(setting.stop)

    def session(self, prompt, task_id, model, effort, turns, timeout, budget, kind, slot=None):
        """Writes the task's file and its test in the slot, as a worker would. The first two wait for each other, so
        the test fails unless they really run side by side."""
        with self.lock:
            self.active += 1
            self.most = max(self.most, self.active)
        if task_id in ("M3-01", "M3-02"):
            self.both_started.wait()
        target = {"M3-01": "a.gd", "M3-02": "b.gd", "M3-03": "a.gd"}[task_id]
        path = slot.root / "game" / "src" / target
        path.write_text(path.read_text(encoding="utf-8") + f"\n\nfunc from_{task_id.replace('-', '_')}() -> void:"
                        "\n\tpass\n", encoding="utf-8", newline="\n")
        name = {"M3-01": "test_a", "M3-02": "test_b", "M3-03": "test_c"}[task_id]
        test = slot.root / "game" / "tests" / f"{name}.gd"
        test.parent.mkdir(parents=True, exist_ok=True)
        test.write_text(f"func {name}() -> void:\n\tassert_true(true)\n", encoding="utf-8", newline="\n")
        with self.lock:
            self.active -= 1
        RN.X.session_event(RN.X.SessionStats(), {"num_turns": 3, "total_cost_usd": 0.25, "usage": {}}, model, effort,
                           kind, 1, pathlib.Path(f"{task_id}.fake.jsonl"))
        return "done", ""

    def test_two_slots_run_side_by_side_and_every_task_lands(self):
        state = C.RUNTIME["state"]
        scheduler = SC.Scheduler(state)
        for _ in range(20):
            scheduler.fill()
            for slot in list(scheduler.slots.values()):
                if slot.thread:
                    slot.thread.join(30)
            if T.next_task() is None:
                break
        landed = self.main_text("loop/TASKS.md")
        for task_id in ("M3-01", "M3-02", "M3-03"):
            self.assertIn(f"### [x] {task_id}", landed)
        self.assertEqual(self.most, 2)
        a_gd = self.main_text("game/src/a.gd")
        self.assertLess(a_gd.index("from_M3_01"), a_gd.index("from_M3_03"))
        self.assertEqual(git(self.main, "status", "--porcelain"), "")
        self.assertEqual(state.get("requeues") or {}, {})
        self.assertIn("## Left to do (0)", self.main_text(C.STATUS_REL))

    def run_driver(self):
        """The real driver loop until the queue is done, then WIND DOWN. Returns its exit code."""
        patches = [mock.patch.object(C, "DRIVER_PID", self.temp / "driver.pid"), mock.patch.object(C, "refresh"),
                   mock.patch.object(C, "POLL_SECONDS", 1), mock.patch.object(C, "MONITOR_EVERY", 0),
                   mock.patch.object(K, "poll_messages", return_value=[]),
                   mock.patch.object(M.B, "over_budget", return_value=False)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        result = {}
        driver = threading.Thread(target=lambda: result.update(code=M.main()), daemon=True)
        driver.start()
        for _ in range(60):
            if any(title == "Loop finished" for title, _, _ in self.posts):
                break
            driver.join(0.5)
        C.RUNTIME["state"]["exit_after"] = True
        driver.join(20)
        return result.get("code")

    def test_every_task_run_leaves_a_timed_record_the_report_reads(self):
        import report
        self.assertEqual(self.run_driver(), 0)
        recorded = report.load(C.log_path())
        kinds = [event["event"] for event in recorded]
        for kind in ("driver_start", "task_start", "session", "gate", "land", "task_end", "driver_stop"):
            self.assertIn(kind, kinds)
        ends = {event["task"]: event for event in recorded if event["event"] == "task_end"}
        self.assertEqual({task: end["outcome"] for task, end in ends.items()},
                         {"M3-01": "landed", "M3-02": "landed", "M3-03": "landed"})
        self.assertTrue(all(end["slot"] in ("1", "2") and end["run"] == 1 and end["seconds"] >= 0
                            for end in ends.values()))
        waits = [event for event in recorded if event["event"] == "schedule_wait"]
        reasons = [wait["reason"] for wait in waits]
        self.assertIn("no open task is waiting", reasons)
        empty = waits[len(reasons) - 1 - reasons[::-1].index("no open task is waiting")]
        self.assertEqual(empty["running"], [])
        # why: once the queue finishes the driver pauses, and the pause itself is recorded as the idle reason
        self.assertEqual(waits[-1]["reason"], "paused")
        sessions = [event for event in recorded if event["event"] == "session"]
        self.assertEqual(len(sessions), 3)
        table = report.runs(recorded)
        self.assertEqual(sorted(task for (task, _), row in table.items() if row.get("outcome") == "landed"),
                         ["M3-01", "M3-02", "M3-03"])
        with mock.patch("builtins.print") as printed:
            report.report(recorded)
        text = "\n".join(" ".join(str(part) for part in call.args) for call in printed.call_args_list)
        self.assertIn("3 landed", text)
        self.assertIn("Why free slots sat idle", text)
        self.assertIn("no open task is left to start", text)

    def test_the_driver_loop_runs_the_queue_then_winds_down(self):
        state_file = self.temp / "driver.json"
        self.assertEqual(self.run_driver(), 0)
        landed = self.main_text("loop/TASKS.md")
        for task_id in ("M3-01", "M3-02", "M3-03"):
            self.assertIn(f"### [x] {task_id}", landed)
        self.assertEqual(json.loads(state_file.read_text(encoding="utf-8"))["slots"], [])
        self.assertEqual(self.most, 2)


class FrictionTest(DriverTest):
    """The scheduler's look-ahead and its reasons to wait, on a queue of its own."""
    QUEUE = """## How to read a task

### [ ] X-01 · First
**Files.** Change `src/a.gd`.

### [ ] X-02 · Second
**Files.** Change `src/a.gd`.

### [ ] X-03 · Third
**Files.** Change `src/b.gd`.

### [ ] X-04 · Fourth
**Files.** Change `src/c.gd`.
"""

    def setUp(self):
        super().setUp()
        patches = [mock.patch.object(C, "PARALLEL_SLOTS", 2), mock.patch.object(C, "PARALLEL_WINDOW", 4),
                   mock.patch.object(C, "SHARED_FILES", [WORLD])]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.scheduler = SC.Scheduler(C.RUNTIME["state"])

    def run_first(self, text):
        slot = self.scheduler.slots["1"]
        slot.task, slot.spec = "X-01", C.find_spec("X-01", text)
        L.start(SL.LEASES, "1", "X-01", SL.task_files(slot.spec), C.SHARED_FILES)

    def pick(self, text):
        self.run_first(text)
        found = self.scheduler.next_start(text)
        return found[0] if found else None

    def test_a_later_task_passes_one_that_must_wait_for_a_file(self):
        self.assertEqual(self.pick(self.QUEUE), "X-03")

    def test_it_never_passes_a_task_that_shares_its_file_nor_a_solo_one(self):
        text = self.QUEUE.replace("Change `src/b.gd`.", "Change `src/b.gd` and `src/a.gd`.")
        self.assertEqual(self.pick(text), "X-04")
        text = self.QUEUE.replace("### [ ] X-03 · Third\n", "### [ ] X-03 · Third\n**Who.** Opus\n")
        self.assertIsNone(self.pick(text))

    def test_it_waits_for_an_after_line_and_for_tasks_it_names(self):
        self.assertEqual(self.pick(self.QUEUE.replace("### [ ] X-03 · Third\n", "### [ ] X-03 · Third\n**After.** X-02\n")),
                         "X-04")
        self.assertEqual(self.pick(self.QUEUE.replace("Change `src/b.gd`.", "Change `src/b.gd`. It uses X-02's helper.")),
                         "X-04")
        self.assertEqual(self.pick(self.QUEUE.replace("Change `src/a.gd`.\n\n### [ ] X-03",
                                                      "Change `src/a.gd`. X-03 builds on this.\n\n### [ ] X-03")),
                         "X-04")
        C.RUNTIME["state"]["waits"] = {"X-03": ["X-01"]}
        self.assertEqual(self.pick(self.QUEUE), "X-04")

    def test_a_shared_file_is_shared_only_for_different_functions(self):
        text = self.QUEUE.replace("Change `src/a.gd`.\n\n### [ ] X-02",
                                  "Change `src/core/world.gd`: `end_round(winner, reason)` stops.\n\n### [ ] X-02")
        text = text.replace("Change `src/a.gd`.\n\n### [ ] X-03", "Change {}.\n\n### [ ] X-03")
        same = text.replace("Change {}.", "Change `src/core/world.gd`: `World.end_round` waits.")
        other = text.replace("Change {}.", "Change `src/core/world.gd`: `_move_workers` skips the idle.")
        self.assertEqual(self.pick(same), "X-03")
        self.assertEqual(self.pick(other), "X-02")

    def test_the_files_line_drops_what_it_says_not_to_change(self):
        spec = "**Files.** Under `content/` only: rows in `content/csv/items.csv`. Change no file under `src/`."
        self.assertEqual(SL.task_files(spec), ["game/content/", "game/content/csv/items.csv"])

    def test_functions_a_spec_names_in_a_file(self):
        spec = ("`World.end_round`, then `_move_workers` and `step()`; `Route.close(world, reason)` and the "
                "word `units` are not functions of the file")
        self.assertEqual(SL.named_functions(spec, WORLD), {"end_round", "_move_workers", "step"})

    def test_the_files_line_brackets_name_the_functions_a_task_changes(self):
        spec = ("**Files.** Change `src/core/world.gd` (`_check_pair`, one call in `_run_systems`; adds a\n"
                "field), `src/core/body.gd`.\n\n**Build.** `World.end_round` is only read here.")
        self.assertEqual(SL.named_functions(spec, WORLD), {"_check_pair"})
        self.assertEqual(SL.named_functions("**Files.** Change `src/core/world.gd` (adds a signal only).\n\n"
                                            "**Build.** `Timer.step` counts down.", WORLD), set())
        self.assertEqual(SL.named_functions("**Files.** Change `res://src/core/world.gd` (`add_unit`).\n", WORLD),
                         {"add_unit"})
        self.assertEqual(SL.named_functions("**Files.** Change `src/core/world.gd`.\n\n**Build.** "
                                            "`step` of the timer, then `World.end_round`.", WORLD), {"end_round"})

    def test_a_refused_file_makes_the_task_wait_for_its_holder(self):
        L.start(SL.LEASES, "1", "X-01", ["game/src/a.gd"], [])
        self.assertIn("X-01", L.acquire(SL.LEASES, "2", "game/src/a.gd"))
        self.assertEqual(L.refusals(SL.LEASES, "2"), [("game/src/a.gd", "X-01")])
        state = C.RUNTIME["state"]
        with mock.patch.object(SL, "revert"):
            T.requeue(fake_slot("2"), "X-03", "needed a.gd", state, ["X-01"])
        self.assertEqual(state["waits"], {"X-03": ["X-01"]})
        T.task_committed(state, "X-03")
        self.assertEqual(state["waits"], {})


class CatchUpTest(RepoTest):
    SECOND = ("### [ ] M3-02 · Second\n**Files.** Change `src/b.gd`.\n\n**Done when.** test `test_b`\n")

    def attempt(self, slot, tests):
        """worker_attempt for M3-02 in the slot, with a session that writes the task's file and its test, and tests as
        the gate's test run."""
        (self.temp / "logs").mkdir(exist_ok=True)

        def session(prompt, task_id, model, effort, turns, timeout, budget, kind, slot=None):
            (slot.root / "game/src/b.gd").write_text("extends Node\n\nvar x := 1\n", encoding="utf-8")
            test = slot.root / "game/tests/test_b.gd"
            test.parent.mkdir(parents=True, exist_ok=True)
            test.write_text("func test_b() -> void:\n\tassert_true(true)\n", encoding="utf-8", newline="\n")
            return "done", ""
        patches = [mock.patch.object(RN.X, "run_session", session), mock.patch.object(RN.X, "run_tests", tests),
                   mock.patch.object(RN, "commit_made_during", return_value=False),
                   mock.patch.object(B, "session_budget", return_value=5.0),
                   mock.patch.object(C, "LOGS", self.temp / "logs"),
                   mock.patch.object(C, "SCOPE", ["game/src", "game/tests", "game/content"])]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            return RN.worker_attempt(slot, "M3-02", "Second", self.SECOND, "sonnet", "", None)

    def test_the_lock_is_held_from_the_gate_to_the_landing(self):
        self.commit_rules()
        slot = self.slot("1")
        got = []

        def tests(root):
            grab = threading.Thread(target=lambda: got.append(SL.INTEGRATE_LOCK.acquire(timeout=0.2)))
            grab.start()
            grab.join()
            if got[-1]:
                SL.INTEGRATE_LOCK.release()
            return PASSING
        self.assertIsNone(self.attempt(slot, tests))
        self.assertEqual(got, [False])
        self.assertIn("### [x] M3-02", self.main_text("loop/TASKS.md"))
        self.assertTrue(SL.INTEGRATE_LOCK.acquire(blocking=False))
        SL.INTEGRATE_LOCK.release()

    def test_no_retest_when_nothing_landed_during_the_gate(self):
        self.commit_rules()
        one, two = self.slot("1"), self.slot("2")
        self.work(one, "game/src/a.gd", A_GD + "\n\nfunc g() -> void:\n\tpass\n", "M3-01")
        self.assertEqual(self.land(one, "M3-01"), "")
        with mock.patch.object(SL, "recheck", mock.Mock(return_value="")) as recheck:
            self.assertIsNone(self.attempt(two, lambda root: PASSING))
        recheck.assert_not_called()
        self.assertIn("### [x] M3-02", self.main_text("loop/TASKS.md"))
        self.assertIn("func g()", self.main_text("game/src/a.gd"))

    def test_the_gate_tests_work_already_moved_onto_the_newest_commit(self):
        one, two = self.slot("1"), self.slot("2")
        self.work(one, "game/src/a.gd", A_GD + "\n\nfunc g() -> void:\n\tpass\n", "M3-01")
        self.assertEqual(self.land(one, "M3-01"), "")
        (two.root / "game/src/b.gd").write_text("extends Node\n\nvar x := 1\n", encoding="utf-8")
        self.assertEqual(SL.catch_up(two), "")
        self.assertEqual(two.base, git(self.main, "rev-parse", "HEAD"))
        self.assertEqual(SL.task_changes(two.snap(), two.root), ["game/src/b.gd"])
        self.assertIn("func g()", (two.root / "game/src/a.gd").read_text(encoding="utf-8"))
        SL.commit_work(two, "M3-02: work\n", ["game/src"])
        C.test_output.reset_mock()
        self.assertEqual(self.land(two, "M3-02"), "")
        C.test_output.assert_not_called()

    def test_a_conflict_is_found_before_the_gate_and_the_work_is_kept(self):
        one, two = self.slot("1"), self.slot("2")
        self.work(one, "game/src/a.gd", A_GD.replace("signal one", "signal uno"), "M3-01")
        self.assertEqual(self.land(one, "M3-01"), "")
        old = two.base
        (two.root / "game/src/a.gd").write_text(A_GD.replace("signal one", "signal eins"), encoding="utf-8")
        self.assertIn("conflict", SL.catch_up(two))
        self.assertEqual(two.base, old)
        self.assertEqual(C.head(two.root), old)
        self.assertIn("signal eins", (two.root / "game/src/a.gd").read_text(encoding="utf-8"))
        self.assertEqual(SL.task_changes(two.snap(), two.root), ["game/src/a.gd"])


class ReviewPacketTest(RepoTest):
    def test_a_review_packet_names_its_range_and_tasks(self):
        self.assertTrue(T.is_review("M3-V1") and T.is_review("M3-R") and T.is_review("M5-A"))
        self.assertFalse(T.is_review("M3-N") or T.is_review("M3-07"))
        one = self.slot("1")
        self.work(one, "game/src/a.gd", A_GD + "\n\nfunc g() -> void:\n\tpass\n", "M3-01")
        self.assertEqual(self.land(one, "M3-01"), "")
        git(self.main, "commit", "-q", "--allow-empty", "-m", "M3-R: review of M3")
        two = self.slot("2")
        self.work(two, "game/src/b.gd", "extends Node\n\nvar x := 1\n", "M3-02")
        self.assertEqual(self.land(two, "M3-02"), "")
        text = T.build("M3-V1", "### [ ] M3-V1 · Interim review\n**Who.** Opus\n")
        self.assertIn("Range: ", text)
        self.assertIn("the last review's commit (M3-R: review of M3)", text)
        self.assertIn("M3-02: work", text)
        self.assertNotIn("M3-01: work", text)
        self.assertIn("| task | cost |", text)
        self.assertIn("| M3-02 |", text)
        self.assertIn("- game/src/b.gd: x", text)


class SimulateTest(DriverTest):
    def test_the_simulation_drives_the_real_scheduler(self):
        import simulate
        text = FrictionTest.QUEUE.replace("X-0", "M9-0")
        with mock.patch.object(C, "SHARED_FILES", []):
            one, _ = simulate.run(text, 1, 4)
            two, retests = simulate.run(text, 2, 4)
        self.assertEqual(one, 4 * (simulate.WORKER_MINUTES + simulate.GATE_MINUTES))
        self.assertLess(two, one)
        self.assertGreaterEqual(retests, 0)


class ReportTest(DriverTest):
    def test_idle_slot_time_is_charged_to_the_reason_it_waited(self):
        import report
        line = [{"t": 0, "event": "driver_start", "settings": {"parallel_slots": "2"}},
                {"t": 0, "event": "task_start", "slot": "1", "task": "A", "run": 1},
                {"t": 0, "event": "schedule_wait", "reason": "B: shares a.gd with A"},
                {"t": 100, "event": "task_end", "slot": "1", "task": "A", "run": 1, "outcome": "landed"},
                {"t": 100, "event": "task_start", "slot": "1", "task": "B", "run": 1},
                {"t": 100, "event": "task_start", "slot": "2", "task": "C", "run": 1},
                {"t": 160, "event": "task_end", "slot": "1", "task": "B", "run": 1, "outcome": "landed"},
                {"t": 160, "event": "schedule_wait", "reason": "M3-R: runs alone, so it waits for every slot to be idle"},
                {"t": 200, "event": "task_end", "slot": "2", "task": "C", "run": 1, "outcome": "landed"},
                {"t": 200, "event": "pause", "title": "Loop finished"},
                {"t": 260, "event": "driver_stop", "reason": "WIND DOWN"}]
        buckets, observed = report.idle_time(line)
        self.assertEqual(observed, 2 * 260)
        self.assertEqual(buckets["the next tasks share a file with a running one"], 100)
        self.assertEqual(buckets["a task that runs alone (Opus, no Files line, or requeued too often)"], 40)
        self.assertEqual(buckets["paused"], 2 * 60)

    def test_a_session_stream_is_counted(self):
        stats = X.SessionStats()
        stream = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "python game/tools/run_tests.py"}},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.gd"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "content": "Command did not complete within its 120s timeout and was moved"},
                {"type": "tool_result", "is_error": True,
                 "content": "game/src/a.gd is being changed by task M3-01 in another copy of the project"}]}},
            {"type": "system", "subtype": "permission_denied", "message": "Permission to use Bash has been denied."}]
        for event in stream:
            stats.read(event)
        self.assertEqual(stats.tools, {"Bash": 1, "Edit": 1})
        self.assertEqual((stats.test_runs, stats.bash_timeouts, stats.tool_errors, len(stats.denials)), (1, 1, 1, 2))


class TokenTest(DriverTest):
    def test_landed_tasks_reach_the_phone_in_digests(self):
        state = C.RUNTIME["state"]
        with mock.patch.object(C, "COMMIT_DIGEST", 2), mock.patch.object(C, "spend_on", return_value=3.5):
            T.announce(state, "M3-01 First: 3 passed, 0 failed (sonnet)")
            self.assertEqual(self.posts, [])
            T.note_for_digest(state, "M3-02 requeued: a conflict")
            T.announce(state, "M3-03 Third: 3 passed, 0 failed (sonnet)")
            self.assertEqual(len(self.posts), 1)
            title, body, _ = self.posts[0]
            self.assertEqual(title, "2 task(s) landed")
            for part in ("M3-01 First", "M3-03 Third", "M3-02 requeued", "$3.50"):
                self.assertIn(part, body)
            T.send_digest(state)
            self.assertEqual(len(self.posts), 1)

    def test_the_prompt_carries_the_design_parts_a_task_names(self):
        design = "# Doc\n\n## Data import\n\nRows load.\n\n### Formats\n\nSmall.\n\n## Power systems\n\nPower.\n"
        spec = 'Design: "Data\nimport", "Power systems" (Grid), "Nowhere".'
        text = C.design_sections(spec, design)
        self.assertIn("## Data import\n\nRows load.\n\n### Formats", text)
        self.assertIn("## Power systems\n\nPower.", text)
        self.assertNotIn("Nowhere", text)


if __name__ == "__main__":
    unittest.main()
