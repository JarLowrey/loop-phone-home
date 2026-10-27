"""Tests for the loop driver. Run from the runner folder: python -m unittest discover -s driver/tests -v

Each test gets a throwaway project with its own worker.json. Nothing here starts a session or calls ntfy: the parts
that would are replaced."""
import datetime
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parent.parent)]
import budget as B  # noqa: E402
import commands as K  # noqa: E402
import mainloop as M  # noqa: E402
import phone as P  # noqa: E402
import runners as RN  # noqa: E402
import runtime as R  # noqa: E402
import sessions as X  # noqa: E402
import settings as S  # noqa: E402
import tasks as T  # noqa: E402
import workspace as WS  # noqa: E402
import worker_task as W  # noqa: E402

FIXTURES = HERE / "fixtures"


def git(root, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, check=True,
                   capture_output=True)


class DriverTest(unittest.TestCase):
    """A throwaway git project with a config, a task file and a loop file; the phone and print are replaced."""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        (self.root / "src").mkdir()
        (self.root / "loop").mkdir()
        (self.root / "loop" / "TASKS.md").write_text("### [ ] T-1 First\n**Done when.** test `test_one`\n",
                                                    encoding="utf-8")
        (self.root / "loop" / "LOOP.md").write_text("```driver-settings\npoll_seconds: 60\n```\n", encoding="utf-8")
        (self.root / ".gitignore").write_text("loop/logs/\n", encoding="utf-8")
        (self.root / "worker.json").write_text(json.dumps({
            "project_root": ".", "scope": ["src/"], "worker_model": "unused",
            "test_command": [sys.executable, "-c", "print('1 passed, 0 failed')"],
            "topic_file": "no-topic.txt", "token_file": "no-token.txt", "haiku_turns": 12,
        }), encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "start")
        self.message_count = 0
        self.posts = []
        patches = [
            mock.patch.dict(os.environ, {"WORKER_CONFIG": str(self.root / "worker.json")}),
            mock.patch.object(P, "post", lambda title, body, priority="default", actions="":
                              self.posts.append((title, body, actions))),
            mock.patch("builtins.print"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        W.configure()
        R.init_paths()
        R.RUNTIME.clear()
        R.RUNTIME.update(state={}, settings=S.parse_settings(S.SETTING_DEFAULTS), last=None)

    def phone(self, *texts, ids=None):
        if ids is None:
            ids = [f"id{self.message_count + index}" for index in range(len(texts))]
            self.message_count += len(texts)
        with mock.patch.object(W, "poll_inbox", return_value=list(zip(ids, texts))):
            K.handle_commands(R.RUNTIME["state"])

    def machine(self, *texts):
        R.PATHS["request"].write_text("\n".join(texts) + "\n", encoding="utf-8")
        with mock.patch.object(W, "poll_inbox", return_value=[]):
            K.handle_commands(R.RUNTIME["state"])


class ConfigTest(DriverTest):
    def test_old_key_names_are_still_read(self):
        self.assertEqual(W.C.task_turns, 12)
        self.assertEqual(W.C.task_model, "sonnet")

    def test_token_is_sent_when_present(self):
        self.assertEqual(W.ntfy_headers({"Title": "x"}), {"Title": "x"})
        W.C.token = "tk_abc"
        self.assertEqual(W.ntfy_headers()["Authorization"], "Bearer tk_abc")

    def test_read_only_git_is_not_denied(self):
        self.assertNotIn("Bash(git stash:*)", W.C.deny_tools)
        self.assertIn("Bash(git stash pop:*)", W.C.deny_tools)


class StreamParserTest(DriverTest):
    def test_every_recorded_event_parses_without_an_error(self):
        parts = []
        for line in (FIXTURES / "stream.jsonl").read_text(encoding="utf-8").splitlines():
            parts += X.event_parts(json.loads(line))
        self.assertEqual([kind for kind, _ in parts], ["say", "step", "system", "tool_result", "result"])

    def test_permission_denied_is_reported_as_a_system_part(self):
        event = {"type": "system", "subtype": "permission_denied", "message": "denied."}
        self.assertEqual(X.event_parts(event), [("system", "permission_denied: denied.")])


class TaskModelTest(DriverTest):
    def test_default_comes_from_the_config(self):
        self.assertEqual(K.task_model({}), ("sonnet", None))

    def test_model_and_effort(self):
        state = {}
        K.set_model(state, ["MODEL", "HAIKU", "HIGH"])
        self.assertEqual(K.task_model(state), ("haiku", "high"))
        K.set_model(state, ["EFFORT", "MAX"])
        self.assertEqual(K.task_model(state), ("haiku", "max"))
        K.set_model(state, ["MODEL", "OPUS", "DEFAULT"])
        self.assertEqual(K.task_model(state), ("opus", None))

    def test_unknown_model_changes_nothing(self):
        state = {"task_model": "haiku"}
        self.assertIn("Not changed", K.set_model(state, ["MODEL", "GPT"]))
        self.assertEqual(K.task_model(state), ("haiku", None))


class ApprovalTest(DriverTest):
    def test_which_calls_need_approval(self):
        self.assertTrue(K.needs_approval("fable", "low"))
        self.assertTrue(K.needs_approval("opus", "medium"))
        self.assertTrue(K.needs_approval("opus", None))
        self.assertFalse(K.needs_approval("opus", "low"))
        self.assertFalse(K.needs_approval("sonnet", "max"))

    def answer_with(self, answer):
        state = R.RUNTIME["state"]

        def tap(_state):
            for code in state["approvals"]:
                state["approvals"][code]["answer"] = answer
        with mock.patch.object(K, "handle_commands", tap):
            return K.approved(state, "M2-R review")

    def test_approve_and_deny_buttons(self):
        self.assertTrue(self.answer_with("approve"))
        self.assertIn("APPROVE ", self.posts[0][2])
        self.assertIn("DENY ", self.posts[0][2])
        self.assertFalse(self.answer_with("deny"))
        self.assertEqual(R.RUNTIME["state"]["approvals"], {})

    def test_no_answer_is_a_denial(self):
        settings = dict(R.RUNTIME["settings"], approval_hours=0)
        with mock.patch.dict(R.RUNTIME, settings=settings), mock.patch.object(K, "handle_commands"):
            self.assertFalse(K.approved(R.RUNTIME["state"], "x"))

    def test_approvals_off_asks_nothing(self):
        settings = dict(R.RUNTIME["settings"], approvals=False)
        with mock.patch.dict(R.RUNTIME, settings=settings):
            self.assertTrue(K.approved(R.RUNTIME["state"], "an Opus task"))
        self.assertEqual(self.posts, [])
        self.assertEqual(S.parse_settings(dict(S.SETTING_DEFAULTS, approvals="off"))["approvals"], False)
        with self.assertRaises(ValueError):
            S.parse_settings(dict(S.SETTING_DEFAULTS, approvals="maybe"))

    def test_phone_answer_needs_the_right_code(self):
        state = R.RUNTIME["state"]
        state["approvals"] = {"abc123": {"what": "x", "answer": None}}
        self.phone("APPROVE ffffff")
        self.assertIsNone(state["approvals"]["abc123"]["answer"])
        self.phone("deny abc123")
        self.assertEqual(state["approvals"]["abc123"]["answer"], "deny")

    def test_machine_approve_answers_every_request(self):
        state = R.RUNTIME["state"]
        state["approvals"] = {"a": {"what": "x", "answer": None}, "b": {"what": "y", "answer": None}}
        self.machine("APPROVE")
        self.assertEqual([r["answer"] for r in state["approvals"].values()], ["approve", "approve"])


class CommandTest(DriverTest):
    def test_model_from_the_phone_and_the_machine(self):
        self.phone("model haiku low")
        self.assertEqual(K.task_model(R.RUNTIME["state"]), ("haiku", "low"))
        self.machine("MODEL opus high")
        self.assertEqual(K.task_model(R.RUNTIME["state"]), ("opus", "high"))
        self.assertFalse(R.PATHS["request"].exists())

    def test_pause_and_resume(self):
        self.phone("pause")
        self.assertTrue(R.RUNTIME["state"]["paused"])
        self.phone("RESUME")
        self.assertFalse(R.RUNTIME["state"]["paused"])

    def test_a_message_is_read_once(self):
        self.phone("pause", ids=["same"])
        R.RUNTIME["state"]["paused"] = False
        self.phone("pause", ids=["same"])
        self.assertFalse(R.RUNTIME["state"]["paused"])


class EscalationTest(DriverTest):
    def run_task(self, results, approve=True, model="sonnet"):
        calls = []

        def attempt(task_id, title, spec, who, level, feedback):
            calls.append((who, feedback))
            return results[len(calls) - 1]
        with mock.patch.object(RN, "model_attempt", attempt), mock.patch.object(K, "approved", return_value=approve), \
                mock.patch.object(T, "revert_unfinished"), mock.patch.object(T, "block") as block:
            RN.model_task("T-1", "First", "spec", {"task_model": model})
        return calls, block

    def test_failure_is_retried_by_the_stronger_model_with_feedback(self):
        calls, block = self.run_task(["tests fail", None])
        self.assertEqual(calls, [("sonnet", None), ("opus", ("sonnet", "tests fail"))])
        block.assert_not_called()

    def test_two_failures_block(self):
        calls, block = self.run_task(["tests fail", "still failing"])
        self.assertEqual(block.call_args[0][1], "still failing")

    def test_denied_retry_blocks_with_the_first_reason(self):
        calls, block = self.run_task(["tests fail"], approve=False)
        self.assertEqual(len(calls), 1)
        self.assertIn("not approved", block.call_args[0][1])

    def test_no_retry_by_an_equal_model(self):
        calls, block = self.run_task(["tests fail"], model="opus")
        self.assertEqual(len(calls), 1)

    def test_denied_first_attempt_pauses_and_leaves_the_task_open(self):
        state = {"task_model": "fable"}
        with mock.patch.object(K, "approved", return_value=False), mock.patch.object(RN, "model_attempt") as attempt, \
                mock.patch.object(T, "block") as block:
            RN.model_task("T-1", "x", "spec", state)
        attempt.assert_not_called()
        block.assert_not_called()
        self.assertTrue(state["paused"])


class CrashTest(DriverTest):
    def test_a_task_error_blocks_the_task(self):
        state = {}
        with mock.patch.object(T, "block") as block:
            RN.task_crashed("T-1", ValueError("boom"), 1, state)
        self.assertIn("boom", block.call_args[0][1])
        self.assertFalse(state.get("paused"))

    def test_a_failed_block_pauses(self):
        state = {}
        with mock.patch.object(T, "block", side_effect=RuntimeError("git broke")):
            RN.task_crashed("T-1", ValueError("boom"), 1, state)
        self.assertTrue(state["paused"])

    def test_errors_in_a_row_pause(self):
        state = {}
        with mock.patch.object(T, "block"):
            RN.task_crashed("T-1", ValueError("boom"), S.MAX_TASK_ERRORS, state)
        self.assertTrue(state["paused"])


class ChecksTest(DriverTest):
    def test_done_when_test_is_found_in_any_language(self):
        (self.root / "src" / "test_app.py").write_text("def test_one():\n    pass\n", encoding="utf-8")
        self.assertEqual(WS.check_done_when("**Done when.** test `test_one`", ""), "")
        self.assertIn("no test function", WS.check_done_when("**Done when.** test `test_two`", ""))

    def test_missing_done_when_is_refused_only_when_required(self):
        self.assertIn("no **Done when.**", WS.check_done_when("no line", ""))
        self.assertEqual(WS.check_done_when("no line", "", required=False), "")

    def test_removed_tests_are_caught_by_count(self):
        W.C.test_count_glob, W.C.test_count_marker = "src/**/test_*.py", "def test_"
        (self.root / "src" / "test_app.py").write_text("def test_one():\n    pass\n", encoding="utf-8")
        R.RUNTIME["tests_before"] = 2
        reason, _ = RN.check_passed({}, "")
        self.assertIn("tests were removed", reason)

    def test_files_outside_the_scope_are_undone_with_their_folders(self):
        stray = self.root / "src2" / "deep" / "file.py"
        stray.parent.mkdir(parents=True)
        stray.write_text("x", encoding="utf-8")
        self.assertEqual(WS.outside_scope(None), ["src2/deep/file.py"])
        WS.undo_changes(WS.outside_scope(None))
        self.assertFalse((self.root / "src2").exists())


class SnapshotTest(DriverTest):
    BEFORE = ("def test_a():\n    assert x == 1\n    self.assertTrue(y)\n\n\n"
              "def test_b():\n    assert z\n\n\ndef helper():\n    assert q\n")

    def commit_test_file(self, text):
        (self.root / "src" / "test_app.py").write_text(text, encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "tests")

    def test_assert_counts_per_test_function(self):
        self.assertEqual(WS.assert_counts(self.BEFORE), {"test_a": 2, "test_b": 1})

    def test_a_loosened_test_the_task_does_not_name_fails_the_gate(self):
        self.commit_test_file(self.BEFORE)
        snap = WS.snapshot()
        R.RUNTIME["snap"] = snap
        (self.root / "src" / "test_app.py").write_text(self.BEFORE.replace("    self.assertTrue(y)\n", ""),
                                                      encoding="utf-8")
        self.assertEqual(WS.weakened_tests(snap, "spec"), ["test_app.test_a (2 to 1 asserts)"])
        self.assertEqual(WS.weakened_tests(snap, "rewrite test_a"), [])
        self.assertIn("fewer asserts", RN.check_passed({}, "spec")[0])

    def test_a_removed_test_function_is_named(self):
        self.commit_test_file(self.BEFORE)
        snap = WS.snapshot()
        (self.root / "src" / "test_app.py").write_text("def test_a():\n    assert x\n", encoding="utf-8")
        self.assertEqual(WS.removed_tests(snap), ["test_b"])

    def test_loop_files_the_task_does_not_name_fail_the_gate(self):
        snap = WS.snapshot()
        R.RUNTIME["snap"] = snap
        (self.root / "loop" / "LOOP.md").write_text("changed\n", encoding="utf-8")
        (self.root / "loop" / "NOTES.md").write_text("named\n", encoding="utf-8")
        spec = "**Files.** Create `loop/NOTES.md`.\n"
        self.assertEqual(WS.loop_changes(snap, WS.allowed_loop_files(spec, False)), ["loop/LOOP.md"])
        self.assertIn("loop/TASKS.md", WS.allowed_loop_files(spec, True))
        self.assertIn("the task does not name", RN.check_passed({}, spec)[0])

    def test_revert_leaves_the_owners_earlier_edits(self):
        (self.root / "loop" / "LOOP.md").write_text("owner's edit\n", encoding="utf-8")
        R.set_snap(R.RUNTIME["state"], WS.snapshot())
        (self.root / "src" / "new.py").write_text("task's file\n", encoding="utf-8")
        T.revert_unfinished()
        self.assertFalse((self.root / "src" / "new.py").exists())
        self.assertEqual((self.root / "loop" / "LOOP.md").read_text(encoding="utf-8"), "owner's edit\n")

    def test_a_commit_during_a_task_pauses_and_leaves_it_open(self):
        R.RUNTIME["state"] = {}
        R.RUNTIME["snap"] = WS.snapshot()
        self.assertFalse(RN.commit_made_during("T-1"))
        self.commit_test_file("x = 1\n")
        self.assertTrue(RN.commit_made_during("T-1"))
        self.assertTrue(R.RUNTIME["state"]["paused"])

    def test_the_snapshot_survives_in_driver_json(self):
        state = {}
        R.set_snap(state, {"head": "abc", "dirty": {"b", "a"}})
        R.RUNTIME.pop("snap")
        self.assertEqual(R.current_snap(R.read_state()), {"head": "abc", "dirty": {"a", "b"}})


class ReliabilityTest(DriverTest):
    def test_a_hung_test_run_is_killed_and_has_no_totals(self):
        W.C.test_command = [sys.executable, "-c", "import time; print('1 passed, 0 failed', flush=True); time.sleep(60)"]
        W.C.test_timeout = 2
        started = time.time()
        output = X.test_output()
        self.assertLess(time.time() - started, 30)
        self.assertEqual(W.totals_line(output), "")
        self.assertIn("timed out", output)

    def test_claude_is_the_newest_extension_copy(self):
        for version in ("2.1.99", "2.1.311", "2.1.310"):
            exe = self.root / f"anthropic.claude-code-{version}-x" / "resources" / "native-binary" / "claude.exe"
            exe.parent.mkdir(parents=True)
            exe.write_text("")
        W.C.claude_exe = "claude"
        with mock.patch.object(X, "EXTENSIONS", self.root), mock.patch.object(X.shutil, "which", return_value=None):
            self.assertIn("2.1.311", X.claude_exe())
        W.C.claude_exe = "/opt/claude"
        self.assertEqual(X.claude_exe(), "/opt/claude")

    def test_a_session_gets_what_is_left_of_the_day(self):
        with mock.patch.object(B, "spend_on", return_value=110.0):
            self.assertEqual(B.session_budget(30.0), 10.0)
            R.RUNTIME["state"] = {}
            self.assertTrue(B.cut_by_daily_budget("error_max_budget_usd"))
        with mock.patch.object(B, "spend_on", return_value=10.0):
            self.assertEqual(B.session_budget(30.0), 30.0)
            self.assertFalse(B.cut_by_daily_budget("error_max_budget_usd"))

    def test_blocks_in_a_row_queue_a_triage_then_pause(self):
        state = R.RUNTIME["state"]
        state["block_streak"] = [{"task": "T-1", "reason": "pinned"}, {"task": "T-2", "reason": "pinned"}]
        T.after_blocks(state)
        self.assertIn("T-2: pinned", state["triage"])
        self.assertFalse(state.get("paused"))
        state["block_streak"] += [{"task": "T-3", "reason": "x"}, {"task": "T-4", "reason": "x"}]
        T.after_blocks(state)
        self.assertTrue(state["paused"])
        self.assertEqual(state["block_streak"], [])

    def test_a_triage_with_no_change_commits_nothing(self):
        state = R.RUNTIME["state"]
        state["triage"] = "- T-1: pinned"
        with mock.patch.object(X, "run_session", return_value=("No shared cause.", "")):
            RN.run_triage(state, state["triage"])
        self.assertNotIn("triage", state)
        self.assertEqual(self.posts[-1][0], "Triage: no change")
        self.assertIsNone(state["snap"])

    def test_autostart_respects_an_owner_stop(self):
        R.write_state({"ended_by": "owner"})
        with mock.patch.object(M, "supervise") as supervise:
            self.assertEqual(M.autostart([]), 0)
            supervise.assert_not_called()
            R.write_state({"ended_by": None})
            M.autostart([])
            supervise.assert_called_once()

    def test_runners_pick_the_kind_of_task(self):
        def pick(spec, solo):
            return next(run for kind, run in RN.RUNNERS if kind(spec, solo))
        self.assertIs(pick("### [ ] T-1 x\n**Who.** Opus\n", True), RN.do_opus_task)
        self.assertIs(pick("### [ ] T-1 x\n", True), RN.model_task)
        self.assertIs(pick("### [ ] T-1 x\n", False), RN.worker_task)


class FilesTest(DriverTest):
    def test_state_is_written_whole(self):
        R.write_state({"paused": True, "task": "T-1"})
        self.assertEqual(json.loads(R.PATHS["state"].read_text(encoding="utf-8"))["task"], "T-1")
        self.assertFalse(R.PATHS["state"].with_suffix(".tmp").exists())

    def test_log_drops_control_characters_and_starts_a_new_file_each_day(self):
        R.PATHS["log"].write_text("old\n", encoding="utf-8")
        yesterday = time.time() - 86400
        os.utime(R.PATHS["log"], (yesterday, yesterday))
        R.log("a\x00b\x1bc")
        day = datetime.date.fromtimestamp(yesterday).isoformat()
        self.assertEqual(R.PATHS["log"].with_name(f"driver-{day}.log").read_text(encoding="utf-8"), "old\n")
        self.assertTrue(R.PATHS["log"].read_text(encoding="utf-8").endswith(" abc\n"))

    def test_spend_on_adds_up_one_day(self):
        (W.C.logs / W.C.usage_log).write_text(
            json.dumps({"time": "2026-10-26 10:00:00", "cost_usd": 1.5}) + "\n"
            + json.dumps({"time": "2026-10-26 11:00:00", "cost_usd": None}) + "\n"
            + json.dumps({"time": "2026-10-25 11:00:00", "cost_usd": 9}) + "\nnot json\n", encoding="utf-8")
        self.assertAlmostEqual(B.spend_on("2026-10-26"), 1.5)

    def test_set_heading(self):
        T.set_heading("T-1", "x")
        self.assertIsNone(T.next_task())


class SettingsTest(DriverTest):
    def test_defaults_and_a_bad_model(self):
        settings = S.parse_settings(S.SETTING_DEFAULTS)
        self.assertEqual(settings["escalate_model"], "opus")
        self.assertEqual(settings["daily_budget_usd"], 120.0)
        self.assertEqual(S.parse_settings(dict(S.SETTING_DEFAULTS, escalate_model="none"))["escalate_model"], "")
        with self.assertRaises(ValueError):
            S.parse_settings(dict(S.SETTING_DEFAULTS, escalate_model="gpt"))

    def test_reset_time_is_within_a_day(self):
        reset = B.reset_time("You've hit your session limit · resets 1:20pm (America/Los_Angeles)")
        self.assertTrue(time.time() < reset <= time.time() + 86400)


if __name__ == "__main__":
    unittest.main()
