"""Tests for the slots, the scheduler, the leases and the task hook. Run from the project root:
python -m unittest discover -s loop/driver/tests -v

The slot tests make a real git repository in a temp folder, with real worktrees, and land work on it. Nothing here
starts a session, calls ntfy or pushes."""
import json
import os
import pathlib
import subprocess
import sys
import threading
import unittest
from unittest import mock

os.environ.setdefault("WORKER_CONFIG", str(pathlib.Path(__file__).resolve().parent / "fixtures" / "worker.json"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import budget as B  # noqa: E402
import leases as L  # noqa: E402
import loop_common as C  # noqa: E402
import runners as RN  # noqa: E402
import runtime as R  # noqa: E402
import scheduler as SC  # noqa: E402
import settings as S  # noqa: E402
import slots as SL  # noqa: E402
import task_hook as H  # noqa: E402
import tasks as T  # noqa: E402
import workspace as W  # noqa: E402
from test_driver import DriverTest, fake_slot  # noqa: E402

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
PASSING = "3 passed, 0 failed\nsmoke: PASS\n"


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
                           "game/.gitignore": ".godot/\noverride.cfg\n"}.items():
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
        R.RUNTIME["branch"] = "main"
        self.addCleanup(self.remove_worktrees)

    def remove_worktrees(self):
        for root in (self.temp / "slots").glob("*"):
            subprocess.run(["git", "worktree", "remove", "-f", str(root)], cwd=self.main, capture_output=True)

    def slot(self, name="1"):
        slot = SL.Slot(name)
        SL.prepare(slot)
        return slot

    def work(self, slot, path, text, task_id):
        """The task writes path in its slot, and its work is committed there as a passing task's would be."""
        (slot.root / path).write_text(text, encoding="utf-8", newline="\n")
        SL.commit_work(slot, f"{task_id}: work\n", ["game/src"])

    def land(self, slot, task_id, test=True):
        return SL.integrate(slot, f"{task_id}: work\n", T.done_marks(task_id, f"| 2026-10-27 | {task_id} | done |"),
                            test=test)

    def main_text(self, path):
        return git(self.main, "show", f"HEAD:{path}")


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

    def test_two_signals_added_at_one_place_both_land(self):
        one, two = self.slot("1"), self.slot("2")
        self.work(one, "game/src/a.gd", A_GD.replace("signal one\n", "signal one\nsignal two\n"), "M3-01")
        self.work(two, "game/src/a.gd", A_GD.replace("signal one\n", "signal one\nsignal three\n"), "M3-03")
        self.assertEqual(self.land(one, "M3-01"), "")
        self.assertEqual(self.land(two, "M3-03"), "")
        self.assertIn("signal one\nsignal two\nsignal three\n", self.main_text("game/src/a.gd"))

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

    def test_owner_edits_in_the_main_tree_are_never_the_tasks(self):
        one = self.slot("1")
        (self.main / "loop" / "PLAN.md").write_text("the owner's plan\n", encoding="utf-8")
        (self.main / "game/src/a.gd").write_text(A_GD + "# owner\n", encoding="utf-8")
        (one.root / "game/src/b.gd").write_text("extends Node\n\nvar y := 2\n", encoding="utf-8")
        self.assertEqual(W.task_changes(one.snap(), one.root), ["game/src/b.gd"])
        self.assertEqual(W.loop_changes(one.snap(), set(), one.root), [])
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
        with mock.patch.object(S, "POLL_SECONDS", 1), mock.patch.object(SL.time, "sleep", owner_commits):
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
        self.assertEqual(self.main_text("game/src/b.gd"), "extends Node")
        self.assertEqual(R.RUNTIME["state"]["block_streak"][-1]["task"], "M3-02")

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
        state = R.RUNTIME["state"]
        scheduler = SC.Scheduler(state)
        spec = C.find_spec("M3-02", TASKS_MD)
        for _ in range(S.MAX_REQUEUES):
            self.assertFalse(scheduler.solo("M3-02", spec))
            T.requeue(one, "M3-02", "a conflict", state)
        self.assertTrue(scheduler.solo("M3-02", spec))
        T.task_committed(state, "M3-02")
        self.assertFalse(scheduler.solo("M3-02", spec))


class MergeTest(unittest.TestCase):
    def test_additions_on_both_sides_are_both_kept(self):
        text = "a\n<<<<<<< HEAD\nsignal x\n||||||| base\n=======\nsignal y\n>>>>>>> M3-07\nb\n"
        self.assertEqual(SL.additive_merge(text), "a\nsignal x\nsignal y\nb\n")

    def test_a_changed_line_is_not_merged(self):
        self.assertIsNone(SL.additive_merge("<<<<<<< a\nx\n||||||| b\nold\n=======\ny\n>>>>>>> c\n"))

    def test_doubled_names_are_found(self):
        folder = pathlib.Path(__file__).resolve().parent / "fixtures"
        self.assertEqual(SL.TOP_NAME.findall("@export var a := 1\nstatic func b():\n\tvar c\nsignal d(x: int)\n"),
                         ["a", "b", "d"])
        self.assertEqual(SL.duplicates(folder, []), [])


class LeaseTest(DriverTest):
    def test_a_lease_covers_its_file_its_folder_and_what_godot_makes_beside_it(self):
        self.assertTrue(L.covers("game/src/a.gd", "game/src/a.gd.uid"))
        self.assertTrue(L.covers("game/content/text/strings.csv", "game/content/text/strings.en.translation"))
        self.assertTrue(L.covers("game/src/", "game/src/Sim/x.gd"))
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
    def setUp(self):
        super().setUp()
        self.root = self.temp / "slots" / "1"
        self.other = self.temp / "slots" / "2"
        self.main = self.temp / "main"
        self.leases = self.temp / "leases.json"
        L.start(self.leases, "2", "M3-02", ["game/src/b.gd"], [])

    def decide(self, tool, **given):
        return H.decide({"tool_name": tool, "tool_input": given}, str(self.root), "1", str(self.leases),
                        [str(self.main), str(self.other)])

    def test_the_session_stays_in_its_own_slot(self):
        self.assertIn("cd is refused", self.decide("Bash", command="cd game && ls"))
        self.assertEqual(self.decide("Bash", command="python game/tools/run_tests.py"), "")
        forward = str(self.main).replace("\\", "/")
        drive = "/" + forward[0].lower() + forward[2:]
        self.assertIn("another copy", self.decide("Bash", command=f"cat {forward}/loop/TASKS.md"))
        self.assertIn("another copy", self.decide("Bash", command=f"sed -i s/a/b/ {drive}/game/src/a.gd"))
        self.assertIn("another copy", self.decide("Write", file_path=str(self.main / "game" / "src" / "a.gd")))
        self.assertIn("another copy", self.decide("Edit", file_path=str(self.other / "x.gd")))

    def test_writes_take_free_files_and_refuse_held_ones(self):
        self.assertEqual(self.decide("Edit", file_path="game/src/a.gd"), "")
        self.assertIn("game/src/a.gd", L.files_of(self.leases, "1"))
        refused = self.decide("Write", file_path=str(self.root / "game" / "src" / "b.gd"))
        self.assertIn("M3-02", refused)
        self.assertIn("stop now and say that you need", refused)
        self.assertEqual(self.decide("Read", file_path=str(self.other / "x.gd")), "")


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
        patch = mock.patch.object(SL, "LEASES", self.temp / "leases.json")
        patch.start()
        self.addCleanup(patch.stop)
        settings = [mock.patch.object(S, "PARALLEL_SLOTS", 3), mock.patch.object(S, "PARALLEL_WINDOW", 3),
                    mock.patch.object(S, "SHARED_FILES", [])]
        for setting in settings:
            setting.start()
            self.addCleanup(setting.stop)
        self.scheduler = SC.Scheduler(R.RUNTIME["state"])

    def running(self, *tasks, text=None):
        for number, task_id in enumerate(tasks, 1):
            self.scheduler.slots[str(number)].task = task_id
            self.scheduler.slots[str(number)].spec = C.find_spec(task_id, text or self.TEXT)
            L.start(SL.LEASES, str(number), task_id, W.task_files(C.find_spec(task_id, text or self.TEXT)), [])

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
        with mock.patch.object(S, "SHARED_FILES", ["game/src/a.gd"]):
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

    def test_the_window_and_the_after_line(self):
        text = self.TEXT.replace("### [ ] M3-03", "### [x] M3-03").replace("**Who.** Opus\n", "")
        text = text.replace("Change any file under `game/src`", "Change `src/r.gd`")
        self.running("M3-01", "M3-02", "M3-R", text=text)
        self.assertIsNone(self.pick(text))
        with mock.patch.object(S, "PARALLEL_WINDOW", 5):
            self.assertIsNone(self.pick(text))
            self.scheduler.slots["2"].task = None
            L.release(SL.LEASES, "2")
            self.assertEqual(self.pick(text.replace("### [ ] M3-02", "### [x] M3-02")), "M3-05")

    def test_task_files_read_the_files_line(self):
        spec = ("**Files.** Change `src/Core/control_bindings.gd`, `content/text/strings.csv`, `loop/PLAYTEST.md`,\n"
                "`game/tests/test_text_table.gd`, the `ControlBindings.LABELS` const and `res://project.godot`."
                " Change any file under `game/src`.\n")
        self.assertEqual(W.task_files(spec), ["game/src/Core/control_bindings.gd",
                                              "game/content/text/strings.csv", "loop/PLAYTEST.md",
                                              "game/tests/test_text_table.gd", "game/project.godot",
                                              "game/src/"])
        self.assertEqual(SC.after("**After.** M3-01, M3-30\n**Files.** x M3-99\n"), ["M3-01", "M3-30"])


class BudgetTest(DriverTest):
    def test_sessions_side_by_side_reserve_their_caps(self):
        caps = {}

        def other():
            caps["other"] = B.session_budget(30.0)
        with mock.patch.object(C, "spend_on", return_value=70.0), mock.patch.object(S, "DAILY_BUDGET_USD", 120.0):
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
        state = R.RUNTIME["state"]
        with mock.patch.object(RN, "worker_attempt", return_value=RN.Requeue("a conflict")) as attempt, \
                mock.patch.object(RN.K, "approved", return_value=True):
            RN.do_worker_task(slot, "M3-02", "Second", C.find_spec("M3-02", TASKS_MD), state)
        attempt.assert_called_once()
        self.assertEqual(state["requeues"], {"M3-02": 1})
        self.assertIn("### [ ] M3-02", self.main_text("loop/TASKS.md"))

    def test_a_passing_task_lands_with_its_marks(self):
        slot = self.slot("1")
        (slot.root / "game/src/b.gd").write_text("extends Node\n\nvar z := 3\n", encoding="utf-8")
        failed = RN.land(slot, R.RUNTIME["state"], "M3-02", "Second", "M3-02: Second\n", ["game/src"],
                         "| 2026-10-27 | M3-02 | done | 3 passed, 0 failed | x |")
        self.assertEqual(failed, "")
        self.assertIn("### [x] M3-02", self.main_text("loop/TASKS.md"))
        self.assertIn("| M3-02 | done |", self.main_text("loop/PROGRESS.md"))
        self.assertEqual(git(self.main, "log", "-1", "--format=%s"), "M3-02: Second")
        self.assertEqual(json.loads(R.DRIVER_STATE.read_text(encoding="utf-8"))["monitor_pending"], ["M3-02"])


if __name__ == "__main__":
    unittest.main()


class EndToEndTest(RepoTest):
    """The scheduler, the runners and the slots together, with a fake session in place of Claude."""

    def setUp(self):
        super().setUp()
        (self.main / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
        git(self.main, "add", "-A")
        git(self.main, "commit", "-q", "-m", "rules")
        self.active = 0
        self.most = 0
        self.lock = threading.Lock()
        self.both_started = threading.Barrier(2, timeout=10)
        settings = [mock.patch.object(S, "PARALLEL_SLOTS", 2), mock.patch.object(S, "PARALLEL_WINDOW", 3),
                    mock.patch.object(S, "SHARED_FILES", []), mock.patch.object(S, "ESCALATE_MODEL", ""),
                    mock.patch.object(S, "SCOPE", ["game/src", "game/tests", "game/content"]),
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
        state = R.RUNTIME["state"]
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

    def run_driver(self):
        """The real driver loop until the queue is done, then WIND DOWN. Returns its exit code."""
        import commands as K
        import mainloop as M
        patches = [mock.patch.object(R, "DRIVER_PID", self.temp / "driver.pid"), mock.patch.object(S, "refresh"),
                   mock.patch.object(S, "POLL_SECONDS", 1), mock.patch.object(S, "MONITOR_EVERY", 0),
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
        R.RUNTIME["state"]["exit_after"] = True
        driver.join(20)
        return result.get("code")

    def test_every_task_run_leaves_a_timed_record_the_report_reads(self):
        import events
        import report
        self.assertEqual(self.run_driver(), 0)
        recorded = report.load(events.log_path())
        kinds = [event["event"] for event in recorded]
        for kind in ("driver_start", "task_start", "session", "gate", "land", "task_end", "driver_stop"):
            self.assertIn(kind, kinds)
        ends = {event["task"]: event for event in recorded if event["event"] == "task_end"}
        self.assertEqual({task: end["outcome"] for task, end in ends.items()},
                         {"M3-01": "landed", "M3-02": "landed", "M3-03": "landed"})
        self.assertTrue(all(end["slot"] in ("1", "2") and end["run"] == 1 and end["seconds"] >= 0
                            for end in ends.values()))
        waits = [event for event in recorded if event["event"] == "schedule_wait"]
        self.assertEqual(waits[-1]["reason"], "no open task is waiting")
        self.assertEqual(waits[-1]["running"], [])
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
        patches = [mock.patch.object(SL, "LEASES", self.temp / "leases.json"),
                   mock.patch.object(S, "PARALLEL_SLOTS", 2), mock.patch.object(S, "PARALLEL_WINDOW", 4),
                   mock.patch.object(S, "SHARED_FILES", ["game/src/Sim/tactical_sim.gd"]),
                   mock.patch.object(W, "TASK_ID", __import__("re").compile(r"\b([MX]\d*-\w+)\b"))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.scheduler = SC.Scheduler(R.RUNTIME["state"])

    def run_first(self, text):
        slot = self.scheduler.slots["1"]
        slot.task, slot.spec = "X-01", C.find_spec("X-01", text)
        L.start(SL.LEASES, "1", "X-01", W.task_files(slot.spec), S.SHARED_FILES)

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
        R.RUNTIME["state"]["waits"] = {"X-03": ["X-01"]}
        self.assertEqual(self.pick(self.QUEUE), "X-04")

    def test_a_shared_file_is_shared_only_for_different_functions(self):
        text = self.QUEUE.replace("Change `src/a.gd`.\n\n### [ ] X-02",
                                  "Change `src/Sim/tactical_sim.gd`: `end_fight(winner, reason)` stops.\n\n### [ ] X-02")
        text = text.replace("Change `src/a.gd`.\n\n### [ ] X-03", "Change {}.\n\n### [ ] X-03")
        same = text.replace("Change {}.", "Change `src/Sim/tactical_sim.gd`: `TacticalSim.end_fight` waits.")
        other = text.replace("Change {}.", "Change `src/Sim/tactical_sim.gd`: `_move_crew` skips the dead.")
        self.assertEqual(self.pick(same), "X-03")
        self.assertEqual(self.pick(other), "X-02")

    def test_the_files_line_drops_what_it_says_not_to_change(self):
        spec = "**Files.** Under `content/` only: rows in `content/csv/items.csv`. Change no file under `src/`."
        self.assertEqual(W.task_files(spec), ["game/content/", "game/content/csv/items.csv"])

    def test_functions_a_spec_names_in_a_file(self):
        spec = ("`TacticalSim.end_fight`, then `_move_crew` and `step()`; `Crossing.strand(sim, reason)` and the "
                "word `ships` are not functions of the file")
        self.assertEqual(W.named_functions(spec, "game/src/Sim/tactical_sim.gd"),
                         {"end_fight", "_move_crew", "step"})

    def test_the_files_line_brackets_name_the_functions_a_task_changes(self):
        path = "game/src/Sim/tactical_sim.gd"
        spec = ("**Files.** Change `src/Sim/tactical_sim.gd` (`_remove_dead`, one call in `_run_interiors`; adds a\n"
                "field), `src/Crew/crew_body.gd`.\n\n**Build.** `TacticalSim.end_fight` is only read here.")
        self.assertEqual(W.named_functions(spec, path), {"_remove_dead"})
        self.assertEqual(W.named_functions("**Files.** Change `src/Sim/tactical_sim.gd` (adds a signal only).\n\n"
                                           "**Build.** `TeleporterBehaviour.step` counts down.", path), set())
        self.assertEqual(W.named_functions("**Files.** Change `src/Sim/tactical_sim.gd`.\n\n**Build.** "
                                           "`step` of the behaviour, then `TacticalSim.end_fight`.", path), {"end_fight"})

    def test_a_refused_file_makes_the_task_wait_for_its_holder(self):
        L.start(SL.LEASES, "1", "X-01", ["game/src/a.gd"], [])
        self.assertIn("X-01", L.acquire(SL.LEASES, "2", "game/src/a.gd"))
        self.assertEqual(L.refusals(SL.LEASES, "2"), [("game/src/a.gd", "X-01")])
        state = R.RUNTIME["state"]
        with mock.patch.object(SL, "revert"):
            T.requeue(fake_slot("2"), "X-03", "needed a.gd", state, ["X-01"])
        self.assertEqual(state["waits"], {"X-03": ["X-01"]})
        T.task_committed(state, "X-03")
        self.assertEqual(state["waits"], {})


class CatchUpTest(RepoTest):
    def test_the_gate_tests_work_already_moved_onto_the_newest_commit(self):
        one, two = self.slot("1"), self.slot("2")
        self.work(one, "game/src/a.gd", A_GD + "\n\nfunc g() -> void:\n\tpass\n", "M3-01")
        self.assertEqual(self.land(one, "M3-01"), "")
        (two.root / "game/src/b.gd").write_text("extends Node\n\nvar x := 1\n", encoding="utf-8")
        self.assertEqual(SL.catch_up(two), "")
        self.assertEqual(two.base, git(self.main, "rev-parse", "HEAD"))
        self.assertEqual(W.task_changes(two.snap(), two.root), ["game/src/b.gd"])
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
        self.assertEqual(W.task_changes(two.snap(), two.root), ["game/src/a.gd"])


class SimulateTest(DriverTest):
    def test_the_simulation_drives_the_real_scheduler(self):
        import simulate
        text = FrictionTest.QUEUE.replace("X-0", "M9-0")
        with mock.patch.object(S, "SHARED_FILES", []):
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
        stats = session_stats()
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


def session_stats():
    import sessions
    return sessions.SessionStats()


class TokenTest(DriverTest):
    def test_landed_tasks_reach_the_phone_in_digests(self):
        state = R.RUNTIME["state"]
        with mock.patch.object(S, "COMMIT_DIGEST", 2), mock.patch.object(C, "spend_on", return_value=3.5):
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
        design = "# Doc\n\n## Interior combat\n\nCrew fight.\n\n### Rooms\n\nSmall.\n\n## Ship systems\n\nPower.\n"
        spec = 'Design: "Interior\ncombat", "Ship systems" (Cloak), "Nowhere".'
        text = C.design_sections(spec, design)
        self.assertIn("## Interior combat\n\nCrew fight.\n\n### Rooms", text)
        self.assertIn("## Ship systems\n\nPower.", text)
        self.assertNotIn("Nowhere", text)

    def test_a_review_packet_names_its_range_and_tasks(self):
        import packet
        self.assertTrue(packet.is_review("M3-V1") and packet.is_review("M3-R"))
        self.assertFalse(packet.is_review("M3-N") or packet.is_review("M3-07"))
        with mock.patch.object(C, "PROJECT", pathlib.Path(__file__).resolve().parents[4]):
            text = packet.build("M3-V1", "**Files.** Create `loop/reviews/M3-interim-1.md`.")
        self.assertIn("Range: ", text)
        self.assertIn("| task | cost |", text)
