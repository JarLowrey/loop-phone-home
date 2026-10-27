"""Tests for the loop driver. Run from the runner folder: python -m unittest discover -s tests -v

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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import driver as D  # noqa: E402
import worker_task as W  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


class DriverTest(unittest.TestCase):
    """A throwaway git project with a config, a task file and a loop file; the phone and print are replaced."""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        (self.root / "src").mkdir()
        (self.root / "loop").mkdir()
        (self.root / "loop" / "TASKS.md").write_text("### [ ] T-1 First\n**Done when.** test `test_one`\n",
                                                    encoding="utf-8")
        (self.root / "loop" / "LOOP.md").write_text("```driver-settings\npoll_seconds: 60\n```\n", encoding="utf-8")
        (self.root / "worker.json").write_text(json.dumps({
            "project_root": ".", "scope": ["src/"], "worker_model": "unused",
            "test_command": [sys.executable, "-c", "print('1 passed, 0 failed')"],
            "topic_file": "no-topic.txt", "token_file": "no-token.txt", "haiku_turns": 12,
        }), encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "start"],
                       cwd=self.root, check=True)
        self.message_count = 0
        self.posts = []
        patches = [
            mock.patch.dict(os.environ, {"WORKER_CONFIG": str(self.root / "worker.json")}),
            mock.patch.object(D, "post", lambda title, body, priority="default", actions="":
                              self.posts.append((title, body, actions))),
            mock.patch("builtins.print"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        W.configure()
        D.init_paths()
        D.RUNTIME.clear()
        D.RUNTIME.update(state={}, settings=D.parse_settings(D.SETTING_DEFAULTS), last=None)

    def phone(self, *texts, ids=None):
        if ids is None:
            ids = [f"id{self.message_count + index}" for index in range(len(texts))]
            self.message_count += len(texts)
        with mock.patch.object(W, "poll_inbox", return_value=list(zip(ids, texts))):
            D.handle_commands(D.RUNTIME["state"])

    def machine(self, *texts):
        D.PATHS["request"].write_text("\n".join(texts) + "\n", encoding="utf-8")
        with mock.patch.object(W, "poll_inbox", return_value=[]):
            D.handle_commands(D.RUNTIME["state"])


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
            parts += D.event_parts(json.loads(line))
        self.assertEqual([kind for kind, _ in parts], ["say", "step", "system", "tool_result", "result"])

    def test_permission_denied_is_reported_as_a_system_part(self):
        event = {"type": "system", "subtype": "permission_denied", "message": "denied."}
        self.assertEqual(D.event_parts(event), [("system", "permission_denied: denied.")])


class TaskModelTest(DriverTest):
    def test_default_comes_from_the_config(self):
        self.assertEqual(D.task_model({}), ("sonnet", None))

    def test_model_and_effort(self):
        state = {}
        D.set_model(state, ["MODEL", "HAIKU", "HIGH"])
        self.assertEqual(D.task_model(state), ("haiku", "high"))
        D.set_model(state, ["EFFORT", "MAX"])
        self.assertEqual(D.task_model(state), ("haiku", "max"))
        D.set_model(state, ["MODEL", "OPUS", "DEFAULT"])
        self.assertEqual(D.task_model(state), ("opus", None))

    def test_unknown_model_changes_nothing(self):
        state = {"task_model": "haiku"}
        self.assertIn("Not changed", D.set_model(state, ["MODEL", "GPT"]))
        self.assertEqual(D.task_model(state), ("haiku", None))


class ApprovalTest(DriverTest):
    def test_which_calls_need_approval(self):
        self.assertTrue(D.needs_approval("fable", "low"))
        self.assertTrue(D.needs_approval("opus", "medium"))
        self.assertTrue(D.needs_approval("opus", None))
        self.assertFalse(D.needs_approval("opus", "low"))
        self.assertFalse(D.needs_approval("sonnet", "max"))

    def answer_with(self, answer):
        state = D.RUNTIME["state"]

        def tap(_state):
            for code in state["approvals"]:
                state["approvals"][code]["answer"] = answer
        with mock.patch.object(D, "handle_commands", tap):
            return D.approved(state, "M2-R review")

    def test_approve_and_deny_buttons(self):
        self.assertTrue(self.answer_with("approve"))
        self.assertIn("APPROVE ", self.posts[0][2])
        self.assertIn("DENY ", self.posts[0][2])
        self.assertFalse(self.answer_with("deny"))
        self.assertEqual(D.RUNTIME["state"]["approvals"], {})

    def test_no_answer_is_a_denial(self):
        settings = dict(D.RUNTIME["settings"], approval_hours=0)
        with mock.patch.dict(D.RUNTIME, settings=settings), mock.patch.object(D, "handle_commands"):
            self.assertFalse(D.approved(D.RUNTIME["state"], "x"))

    def test_phone_answer_needs_the_right_code(self):
        state = D.RUNTIME["state"]
        state["approvals"] = {"abc123": {"what": "x", "answer": None}}
        self.phone("APPROVE ffffff")
        self.assertIsNone(state["approvals"]["abc123"]["answer"])
        self.phone("deny abc123")
        self.assertEqual(state["approvals"]["abc123"]["answer"], "deny")

    def test_machine_approve_answers_every_request(self):
        state = D.RUNTIME["state"]
        state["approvals"] = {"a": {"what": "x", "answer": None}, "b": {"what": "y", "answer": None}}
        self.machine("APPROVE")
        self.assertEqual([r["answer"] for r in state["approvals"].values()], ["approve", "approve"])


class CommandTest(DriverTest):
    def test_model_from_the_phone_and_the_machine(self):
        self.phone("model haiku low")
        self.assertEqual(D.task_model(D.RUNTIME["state"]), ("haiku", "low"))
        self.machine("MODEL opus high")
        self.assertEqual(D.task_model(D.RUNTIME["state"]), ("opus", "high"))
        self.assertFalse(D.PATHS["request"].exists())

    def test_pause_and_resume(self):
        self.phone("pause")
        self.assertTrue(D.RUNTIME["state"]["paused"])
        self.phone("RESUME")
        self.assertFalse(D.RUNTIME["state"]["paused"])

    def test_a_message_is_read_once(self):
        self.phone("pause", ids=["same"])
        D.RUNTIME["state"]["paused"] = False
        self.phone("pause", ids=["same"])
        self.assertFalse(D.RUNTIME["state"]["paused"])


class EscalationTest(DriverTest):
    def run_task(self, results, approve=True, model="sonnet"):
        calls = []

        def attempt(task_id, title, spec, who, level, feedback):
            calls.append((who, feedback))
            return results[len(calls) - 1]
        with mock.patch.object(D, "model_attempt", attempt), mock.patch.object(D, "approved", return_value=approve), \
                mock.patch.object(D, "revert_unfinished"), mock.patch.object(D, "block") as block:
            D.model_task("T-1", "First", "spec", {"task_model": model})
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
        with mock.patch.object(D, "approved", return_value=False), mock.patch.object(D, "model_attempt") as attempt, \
                mock.patch.object(D, "block") as block:
            D.model_task("T-1", "x", "spec", state)
        attempt.assert_not_called()
        block.assert_not_called()
        self.assertTrue(state["paused"])


class CrashTest(DriverTest):
    def test_a_task_error_blocks_the_task(self):
        state = {}
        with mock.patch.object(D, "block") as block:
            D.task_crashed("T-1", ValueError("boom"), 1, state)
        self.assertIn("boom", block.call_args[0][1])
        self.assertFalse(state.get("paused"))

    def test_a_failed_block_pauses(self):
        state = {}
        with mock.patch.object(D, "block", side_effect=RuntimeError("git broke")):
            D.task_crashed("T-1", ValueError("boom"), 1, state)
        self.assertTrue(state["paused"])

    def test_errors_in_a_row_pause(self):
        state = {}
        with mock.patch.object(D, "block"):
            D.task_crashed("T-1", ValueError("boom"), D.MAX_TASK_ERRORS, state)
        self.assertTrue(state["paused"])


class ChecksTest(DriverTest):
    def test_done_when_test_is_found_in_any_language(self):
        (self.root / "src" / "test_app.py").write_text("def test_one():\n    pass\n", encoding="utf-8")
        self.assertEqual(D.check_done_when("**Done when.** test `test_one`", ""), "")
        self.assertIn("no test function", D.check_done_when("**Done when.** test `test_two`", ""))

    def test_missing_done_when_is_refused_only_when_required(self):
        self.assertIn("no **Done when.**", D.check_done_when("no line", ""))
        self.assertEqual(D.check_done_when("no line", "", required=False), "")

    def test_removed_tests_are_caught_by_count(self):
        W.C.test_count_glob, W.C.test_count_marker = "src/**/test_*.py", "def test_"
        (self.root / "src" / "test_app.py").write_text("def test_one():\n    pass\n", encoding="utf-8")
        D.RUNTIME["tests_before"] = 2
        reason, _ = D.check_passed({}, "")
        self.assertIn("tests were removed", reason)

    def test_files_outside_the_scope_are_undone_with_their_folders(self):
        stray = self.root / "src2" / "deep" / "file.py"
        stray.parent.mkdir(parents=True)
        stray.write_text("x", encoding="utf-8")
        self.assertEqual(D.outside_scope(), ["src2/deep/file.py"])
        D.undo_outside(D.outside_scope())
        self.assertFalse((self.root / "src2").exists())


class FilesTest(DriverTest):
    def test_state_is_written_whole(self):
        D.write_state({"paused": True, "task": "T-1"})
        self.assertEqual(json.loads(D.PATHS["state"].read_text(encoding="utf-8"))["task"], "T-1")
        self.assertFalse(D.PATHS["state"].with_suffix(".tmp").exists())

    def test_log_drops_control_characters_and_starts_a_new_file_each_day(self):
        D.PATHS["log"].write_text("old\n", encoding="utf-8")
        yesterday = time.time() - 86400
        os.utime(D.PATHS["log"], (yesterday, yesterday))
        D.log("a\x00b\x1bc")
        day = datetime.date.fromtimestamp(yesterday).isoformat()
        self.assertEqual(D.PATHS["log"].with_name(f"driver-{day}.log").read_text(encoding="utf-8"), "old\n")
        self.assertTrue(D.PATHS["log"].read_text(encoding="utf-8").endswith(" abc\n"))

    def test_spend_on_adds_up_one_day(self):
        (W.C.logs / W.C.usage_log).write_text(
            json.dumps({"time": "2026-10-26 10:00:00", "cost_usd": 1.5}) + "\n"
            + json.dumps({"time": "2026-10-26 11:00:00", "cost_usd": None}) + "\n"
            + json.dumps({"time": "2026-10-25 11:00:00", "cost_usd": 9}) + "\nnot json\n", encoding="utf-8")
        self.assertAlmostEqual(D.spend_on("2026-10-26"), 1.5)

    def test_set_heading(self):
        D.set_heading("T-1", "x")
        self.assertIsNone(D.next_task())


class SettingsTest(DriverTest):
    def test_defaults_and_a_bad_model(self):
        settings = D.parse_settings(D.SETTING_DEFAULTS)
        self.assertEqual(settings["escalate_model"], "opus")
        self.assertEqual(settings["daily_budget_usd"], 120.0)
        self.assertEqual(D.parse_settings(dict(D.SETTING_DEFAULTS, escalate_model="none"))["escalate_model"], "")
        with self.assertRaises(ValueError):
            D.parse_settings(dict(D.SETTING_DEFAULTS, escalate_model="gpt"))

    def test_reset_time_is_within_a_day(self):
        reset = D.reset_time("You've hit your session limit · resets 1:20pm (America/Los_Angeles)")
        self.assertTrue(time.time() < reset <= time.time() + 86400)


if __name__ == "__main__":
    unittest.main()
