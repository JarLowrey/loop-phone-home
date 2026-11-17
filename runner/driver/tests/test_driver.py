"""Tests for the build loop's driver. Run from runner/driver: python -m unittest discover -s tests -p test_driver.py -v

The project is tests/fixtures/project, through tests/fixtures/worker.json (driver_base sets WORKER_CONFIG). Nothing here
starts a session, calls ntfy or runs git commands that change files: the parts that would are replaced."""
import datetime
import importlib.util
import io
import json
import os
import pathlib
import re
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from driver_base import DriverTest, fake_slot, FIXTURES, REAL_PRINT  # noqa: E402

import budget as B  # noqa: E402
import check_driver  # noqa: E402
import commands as K  # noqa: E402
import core as C  # noqa: E402
import mainloop as M  # noqa: E402
import monitor as MO  # noqa: E402
import opus as O  # noqa: E402
import prompts  # noqa: E402
import report as RP  # noqa: E402
import runners as RN  # noqa: E402
import scheduler as SC  # noqa: E402
import sessions as X  # noqa: E402
import slots as SL  # noqa: E402
import task_hook as TH  # noqa: E402
import tasks as T  # noqa: E402


class StreamParserTest(DriverTest):
    def test_every_recorded_event_parses_without_an_error(self):
        parts = []
        for line in (FIXTURES / "stream.jsonl").read_text(encoding="utf-8").splitlines():
            parts += X.event_parts(json.loads(line))
        kinds = [kind for kind, _ in parts]
        self.assertEqual(kinds, ["say", "step", "system", "tool_result", "result"])

    def test_permission_denied_is_reported_as_a_system_part(self):
        event = {"type": "system", "subtype": "permission_denied", "message": "Permission to use Bash has been denied."}
        self.assertEqual(X.event_parts(event), [("system", "permission_denied: Permission to use Bash has been denied.")])

    def test_tool_result_is_one_line(self):
        event = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "a\nb"}]}}
        self.assertEqual(X.event_parts(event), [("tool_result", "a | b")])


class WorkerSettingTest(DriverTest):
    def test_default_is_the_configs_task_model(self):
        self.assertEqual(K.worker({}), (C.CFG.task_model, None))
        self.assertEqual(K.worker({}), ("sonnet", None))

    def test_model_and_effort(self):
        state = {}
        K.set_worker(state, ["MODEL", "HAIKU", "HIGH"])
        self.assertEqual(K.worker(state), ("haiku", "high"))
        K.set_worker(state, ["EFFORT", "MAX"])
        self.assertEqual(K.worker(state), ("haiku", "max"))
        K.set_worker(state, ["MODEL", "OPUS", "DEFAULT"])
        self.assertEqual(K.worker(state), ("opus", None))

    def test_unknown_model_changes_nothing(self):
        state = {"worker_model": "haiku"}
        reply = K.set_worker(state, ["MODEL", "GPT"])
        self.assertIn("Not changed", reply)
        self.assertEqual(K.worker(state), ("haiku", None))

    def test_effort_alone_asks_for_a_level(self):
        self.assertIn("Say EFFORT", K.set_worker({}, ["EFFORT"]))


class WorkerPromptTest(DriverTest):
    def test_the_worker_prompt_leads_with_never_cd(self):
        lines = prompts.WORKER_PROMPT.splitlines()
        self.assertIn("Never cd", "\n".join(lines[1:6]))
        settings = json.loads(X.task_settings(fake_slot(root=self.temp)))
        command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertIn("--config", command)
        hook_args = json.loads((C.LOGS / "hook-1.json").read_text(encoding="utf-8"))
        self.assertIn("started", hook_args, "the hook knows when its session started")
        self.assertEqual(hook_args["root"], str(self.temp))

    def test_worker_rules_drop_owner_sections(self):
        text = "# A\n\n## Stack\nx\n\n## Branches and commits (owner)\ny\n\n## Layout\nz\n"
        self.assertEqual(prompts.worker_rules(text), "# A\n\n## Stack\nx\n\n## Layout\nz\n")
        self.assertEqual(prompts.worker_rules("# A\n\n## Stack\nx\n\n## Pushing (owner)\ny\n"), "# A\n\n## Stack\nx\n")
        real = prompts.worker_rules(C.rules_text())
        self.assertEqual(real, C.rules_text().rstrip() + "\n", "the fixture's rules file has no owner section")

    def test_the_rules_file_is_read_from_the_tree_and_none_is_empty(self):
        self.assertEqual(C.rules_text(), (C.PROJECT / C.CFG.rules_file).read_text(encoding="utf-8"))
        (self.temp / "CLAUDE.md").write_text("# Slot rules\n", encoding="utf-8")
        self.assertEqual(C.rules_text(self.temp), "# Slot rules\n", "a slot's own copy is read")
        with mock.patch.object(C.CFG, "rules_file", ""):
            self.assertEqual(C.rules_text(self.temp), "")


class ApprovalTest(DriverTest):
    def test_which_calls_need_approval(self):
        self.assertTrue(K.needs_approval("fable", "low"))
        self.assertTrue(K.needs_approval("opus", "medium"))
        self.assertTrue(K.needs_approval("opus", None))
        self.assertFalse(K.needs_approval("opus", "low"))
        self.assertFalse(K.needs_approval("sonnet", "max"))
        self.assertFalse(K.needs_approval("haiku", None))

    def test_approve_button_approves(self):
        state = C.RUNTIME["state"]

        def tap(_state):
            for code in state["approvals"]:
                state["approvals"][code]["answer"] = "approve"
        with mock.patch.object(K, "handle_commands", tap):
            self.assertTrue(K.approved(state, "M2-R review"))
        self.assertEqual(state["approvals"], {})
        self.assertIn("APPROVE ", self.posts[0][2])
        self.assertIn("DENY ", self.posts[0][2])

    def test_deny_and_no_answer_are_denials(self):
        state = C.RUNTIME["state"]

        def tap(_state):
            for code in state["approvals"]:
                state["approvals"][code]["answer"] = "deny"
        with mock.patch.object(K, "handle_commands", tap):
            self.assertFalse(K.approved(state, "a"))
        with mock.patch.object(C, "APPROVAL_HOURS", 0), mock.patch.object(K, "handle_commands", lambda s: None):
            self.assertFalse(K.approved(state, "b"))

    def test_approvals_off_asks_nothing(self):
        state = C.RUNTIME["state"]
        with mock.patch.object(C, "APPROVALS_ON", False):
            self.assertTrue(K.approved(state, "M3-F4 Opus"))
        self.assertEqual(self.posts, [])
        self.assertEqual(state.get("approvals", {}), {})

    def test_phone_answer_needs_the_right_code(self):
        state = C.RUNTIME["state"]
        state["approvals"] = {"abc123": {"what": "x", "answer": None}}
        self.phone("APPROVE ffffff")
        self.assertIsNone(state["approvals"]["abc123"]["answer"])
        self.phone("deny abc123")
        self.assertEqual(state["approvals"]["abc123"]["answer"], "deny")

    def test_pc_approve_answers_every_request(self):
        state = C.RUNTIME["state"]
        state["approvals"] = {"a": {"what": "x", "answer": None}, "b": {"what": "y", "answer": None}}
        self.pc("APPROVE")
        self.assertEqual([r["answer"] for r in state["approvals"].values()], ["approve", "approve"])


class CommandTest(DriverTest):
    def test_opus_question_runs_from_the_phone(self):
        with mock.patch.object(O, "ask_opus") as ask:
            self.phone("OPUS: Why is M2-30 blocked?")
        ask.assert_called_once_with("Why is M2-30 blocked?")

    def test_model_report_and_change_from_the_phone(self):
        self.phone("MODEL")
        self.assertEqual(self.posts[-1][0], "Loop worker")
        self.phone("model haiku low")
        self.assertEqual(K.worker(C.RUNTIME["state"]), ("haiku", "low"))

    def test_pc_commands_are_read_from_the_request_file(self):
        self.pc("MODEL opus high")
        self.assertEqual(K.worker(C.RUNTIME["state"]), ("opus", "high"))
        self.assertFalse(C.REQUEST_FILE.exists())

    def test_pause_and_resume(self):
        self.phone("pause")
        self.assertTrue(C.RUNTIME["state"]["paused"])
        self.phone("RESUME")
        self.assertFalse(C.RUNTIME["state"]["paused"])


class OpusQuestionTest(DriverTest):
    def test_a_question_past_its_time_is_killed_and_reported(self):
        """A phone question that runs past OPUS_TIMEOUT is ended with everything it started, and the phone is told."""
        class Hung:
            pid = 4242

            def __init__(self, *args, **kwargs):
                self.calls = 0

            def communicate(self, input=None, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise O.subprocess.TimeoutExpired("claude", timeout)
                return "", ""

        killed = []
        with mock.patch.object(O.subprocess, "Popen", Hung), mock.patch.object(C, "kill_tree", killed.append):
            O.run_opus("Why is M2-30 blocked?")
        self.assertEqual(killed, [4242])
        self.assertEqual(self.posts[-1][0], "Opus failed")
        self.assertIn("took longer", self.posts[-1][1])
        self.assertNotIn(4242, O.OPUS_CHILDREN)


class EscalationTest(DriverTest):
    # what the failed attempt's gate saw, and what the retry is told of it
    GATE_OUTPUT = "  PASS test_toggle_reads (2 ms)\n  FAIL test_toggle_layers (3 ms)\n12 passed, 1 failed\n"
    TESTS = "  FAIL test_toggle_layers (3 ms)\n12 passed, 1 failed"

    def run_task(self, results, approve=True, escalate="opus", model="sonnet"):
        calls = []

        def attempt(slot, task_id, title, spec, who, level, feedback):
            calls.append((who, feedback))
            C.ctx()["gate_output"] = self.GATE_OUTPUT if feedback is None else ""
            return results[len(calls) - 1]
        with mock.patch.object(C, "ESCALATE_MODEL", escalate), mock.patch.object(C, "PARALLEL_SLOTS", 1), \
                mock.patch.object(RN, "worker_attempt", attempt), \
                mock.patch.object(K, "approved", return_value=approve), mock.patch.object(SL, "prepare"), \
                mock.patch.object(T, "requeue") as requeue, mock.patch.object(T, "block") as block:
            RN.do_worker_task(fake_slot(), "M2-30", "Layer toggles", "spec", {"worker_model": model})
        self.requeue = requeue
        return calls, block

    def test_success_needs_no_retry(self):
        calls, block = self.run_task([None])
        self.assertEqual([who for who, _ in calls], ["sonnet"])
        block.assert_not_called()

    def test_failure_is_retried_by_the_stronger_model_with_feedback(self):
        calls, block = self.run_task(["tests fail", None])
        self.assertEqual(calls, [("sonnet", None), ("opus", ("sonnet", "tests fail", self.TESTS))])
        block.assert_not_called()

    def test_the_retry_is_told_the_failed_runs_failures_and_totals(self):
        self.assertEqual(X.tests_summary(self.GATE_OUTPUT), self.TESTS)
        self.assertEqual(X.tests_summary("3 passed, 0 failed\nsmoke: FAIL no window\n"),
                         "3 passed, 0 failed\nsmoke: FAIL no window")
        self.assertEqual(X.tests_summary(""), "(no test output)")
        C.ctx()["gate_output"] = self.GATE_OUTPUT
        self.assertEqual(X.tests_summary(), self.TESTS, "the task's last test run by default")

    def test_two_failures_block(self):
        calls, block = self.run_task(["tests fail", "still failing"])
        self.assertEqual(block.call_args[0][3], "still failing")

    def test_denied_retry_blocks_with_the_first_reason(self):
        calls, block = self.run_task(["tests fail"], approve=False)
        self.assertEqual(len(calls), 1)
        self.assertIn("tests fail", block.call_args[0][3])
        self.assertIn("not approved", block.call_args[0][3])

    def test_no_retry_by_a_weaker_or_equal_model(self):
        calls, block = self.run_task(["tests fail"], model="opus")
        self.assertEqual(len(calls), 1)
        block.assert_called_once()

    def test_with_two_slots_the_escalation_is_queued_then_runs_with_the_feedback(self):
        state = C.RUNTIME["state"]
        state["worker_model"] = "sonnet"
        calls = []

        def attempt(slot, task_id, title, spec, who, level, feedback):
            calls.append((who, feedback))
            C.ctx()["gate_output"] = self.GATE_OUTPUT if who == "sonnet" else ""
            return "tests fail" if who == "sonnet" else None
        with mock.patch.object(C, "PARALLEL_SLOTS", 2), mock.patch.object(C, "ESCALATE_MODEL", "opus"), \
                mock.patch.object(RN, "worker_attempt", attempt), mock.patch.object(K, "approved", return_value=True), \
                mock.patch.object(SL, "revert") as revert, mock.patch.object(T, "block") as block:
            RN.do_worker_task(fake_slot(), "M2-30", "x", "spec", state)
            self.assertEqual(calls, [("sonnet", None)])
            self.assertEqual(state["escalations"]["M2-30"]["reason"], "tests fail")
            self.assertEqual(state["escalations"]["M2-30"]["tests"], self.TESTS, "the queued escalation keeps the tests")
            self.assertEqual(C.ctx()["outcome"], "escalating")
            revert.assert_called_once()
            RN.do_worker_task(fake_slot(), "M2-30", "x", "spec", state)
        self.assertEqual(calls[1], ("opus", ("sonnet", "tests fail", self.TESTS)))
        self.assertNotIn("M2-30", state["escalations"])
        block.assert_not_called()

    def test_denied_first_attempt_pauses_and_leaves_the_task_open(self):
        state = {"worker_model": "fable"}
        with mock.patch.object(K, "approved", return_value=False), mock.patch.object(RN, "worker_attempt") as attempt, \
                mock.patch.object(T, "block") as block:
            RN.do_worker_task(fake_slot(), "M2-30", "x", "spec", state)
        attempt.assert_not_called()
        block.assert_not_called()
        self.assertTrue(state["paused"])


class CrashTest(DriverTest):
    def test_a_task_error_blocks_the_task(self):
        state = {}
        with mock.patch.object(T, "block") as block:
            RN.task_crashed(fake_slot(), "M2-30", "x", ValueError("boom"), 1, state)
        self.assertIn("boom", block.call_args[0][3])
        self.assertFalse(state.get("paused"))

    def test_a_failed_block_pauses(self):
        state = {}
        with mock.patch.object(T, "block", side_effect=RuntimeError("git broke")):
            RN.task_crashed(fake_slot(), "M2-30", "x", ValueError("boom"), 1, state)
        self.assertTrue(state["paused"])

    def test_errors_in_a_row_pause(self):
        state = {}
        with mock.patch.object(T, "block"):
            RN.task_crashed(fake_slot(), "M2-30", "x", ValueError("boom"), C.MAX_TASK_ERRORS, state)
        self.assertTrue(state["paused"])


class FilesTest(DriverTest):
    def test_state_is_written_whole(self):
        C.write_driver_state({"paused": True, "task": "M2-30"})
        self.assertEqual(json.loads(C.DRIVER_STATE.read_text(encoding="utf-8"))["task"], "M2-30")
        self.assertFalse(C.DRIVER_STATE.with_suffix(".tmp").exists())

    def test_log_drops_control_characters_and_starts_a_new_file_each_day(self):
        C.DRIVER_LOG.write_text("old\n", encoding="utf-8")
        yesterday = time.time() - 86400
        os.utime(C.DRIVER_LOG, (yesterday, yesterday))
        C.log("a\x00b\x1bc")
        day = datetime.date.fromtimestamp(yesterday).isoformat()
        self.assertEqual((self.temp / f"driver-{day}.log").read_text(encoding="utf-8"), "old\n")
        self.assertTrue(C.DRIVER_LOG.read_text(encoding="utf-8").endswith(" abc\n"))

    def test_a_cp1252_console_is_made_to_take_any_character(self):
        """A model's summary with an arrow in it raised UnicodeEncodeError inside print() on a cp1252 console, which
        ended the task that had just finished and then the driver itself."""
        text = "the menu → the settings, with an em—dash"
        raw = io.BytesIO()
        console = io.TextIOWrapper(raw, encoding="cp1252", line_buffering=True)
        with mock.patch.object(sys, "stdout", console):
            C.utf8_console()
            self.assertEqual(sys.stdout.encoding, "utf-8")
            sys.stdout.write(text)
            sys.stdout.flush()
        self.assertIn(text, raw.getvalue().decode("utf-8"))

    def test_log_still_writes_when_the_console_refuses_the_line(self):
        """The log file is the record that matters, so a console that cannot be made to take the line is skipped rather
        than allowed to end the task."""
        text = "an arrow → here"
        with mock.patch("builtins.print", side_effect=UnicodeEncodeError("charmap", text, 9, 10, "no")):
            C.log(text)
        self.assertIn(text, C.DRIVER_LOG.read_text(encoding="utf-8"))

    def test_set_heading(self):
        tasks = self.temp / "TASKS.md"
        tasks.write_text("### [ ] M2-30 · Layer toggles\nbody\n### [ ] M2-31 · Search\n", encoding="utf-8")
        with mock.patch.object(C, "TASKS", tasks):
            T.set_heading("M2-31", "!")
            self.assertEqual(T.next_task(tasks.read_text(encoding="utf-8")), ("M2-30", "Layer toggles"))
            T.set_heading("M2-30", "x")
            self.assertIsNone(T.next_task(tasks.read_text(encoding="utf-8")))

    def test_picked_text_reads_the_last_message(self):
        label = ("Files: runner/driver/monitor.py\n3 passed, 0 failed\n\n**Picked.** RETRY_LIMIT = 4: four tries cover "
                 "every flaky step once\nand run under 10 minutes.\n\nNothing else to say.\n")
        self.assertEqual(T.picked_text(label), "RETRY_LIMIT = 4: four tries cover every flaky step once and run under "
                                               "10 minutes.")
        lines = "Done.\nPicked: A = 3, the smallest that works.\n3 passed\nPicked: B = 0.5.\n"
        self.assertEqual(T.picked_text(lines), "A = 3, the smallest that works.; B = 0.5.")
        self.assertEqual(T.picked_text("Files: a.py\n3 passed, 0 failed\n"), "")
        self.assertEqual(T.picked_text(""), "")

    def test_a_picked_number_is_appended_under_open_questions(self):
        root = self.temp / "tree"
        (root / "loop").mkdir(parents=True)
        path = root / C.PROGRESS_REL
        today = datetime.date.today().isoformat()
        path.write_text("# Progress\n\n## Open questions\n\n- M1-01 (2026-10-01): old\n\n### Resolved\n\nArchived.\n\n"
                        "## Next\n\n- later\n", encoding="utf-8", newline="\n")
        before = path.read_text(encoding="utf-8").split("\n")
        T.add_open_question("M14-10", "RETRY_LIMIT = 4", root)
        after = path.read_text(encoding="utf-8").split("\n")
        self.assertEqual(len(after), len(before) + 1)
        resolved = after.index("### Resolved")
        self.assertEqual(after[resolved - 2], f"- M14-10 ({today}): RETRY_LIMIT = 4")
        self.assertEqual(after[resolved - 3], "- M1-01 (2026-10-01): old")
        self.assertEqual(after[resolved - 1], "")
        self.assertEqual(after[:resolved - 2], before[:resolved - 2])
        self.assertEqual(after[resolved - 1:], before[resolved - 2:])

        path.write_text("# Progress\n\n## Open questions\n\n- M1-01 (2026-10-01): old\n\n## Next\n\n- later\n",
                        encoding="utf-8", newline="\n")
        T.add_open_question("M14-10", "RETRY_LIMIT = 4", root)
        self.assertEqual(path.read_text(encoding="utf-8"),
                         f"# Progress\n\n## Open questions\n\n- M1-01 (2026-10-01): old\n- M14-10 ({today}): "
                         "RETRY_LIMIT = 4\n\n## Next\n\n- later\n")

        path.write_text("# Progress\n\n## Open questions\n\n- M1-01 (2026-10-01): old\n", encoding="utf-8", newline="\n")
        T.add_open_question("M14-10", "RETRY_LIMIT = 4 " + "and a reason that runs on " * 8, root)
        added = path.read_text(encoding="utf-8").split("\n")[5:-1]
        self.assertGreater(len(added), 1)
        self.assertTrue(all(len(line) <= 120 for line in added))
        self.assertTrue(added[0].startswith(f"- M14-10 ({today}): RETRY_LIMIT = 4"))
        self.assertTrue(all(line.startswith("  ") for line in added[1:]))

    def test_spend_on_adds_up_one_day(self):
        usage = self.temp / "usage.jsonl"
        usage.write_text(json.dumps({"time": "2026-10-26 10:00:00", "cost_usd": 1.5}) + "\n"
                         + json.dumps({"time": "2026-10-26 11:00:00", "cost_usd": None}) + "\n"
                         + json.dumps({"time": "2026-10-25 11:00:00", "cost_usd": 9}) + "\nnot json\n",
                         encoding="utf-8")
        with mock.patch.object(C, "USAGE_LOG", usage):
            self.assertAlmostEqual(C.spend_on("2026-10-26"), 1.5)


class StatusTest(DriverTest):
    """The status file (status_file in the config): one short file that answers what is done, blocked and left."""

    QUEUE = ("# Task queue\n\nWork top to bottom.\n\n## How to read a task\n\n- **Files** lists every file.\n\n"
             "## Phase 8 · M5\n\n**Milestone check.** Release.\n\n"
             "### [x] M5-01 · Eight parsers\n**Goal.** Parsers.\n\n"
             "### [!] M5-07 · Load harness\n**Goal.** Harness.\n\n"
             "### [ ] M5-B · The five cases that miss\n**Goal.** Tune.\n\n"
             "### [ ] M5-R · Opus review\n**Goal.** Review.\n")
    LOG = ("# Progress\n\n## Log\n\n| When | Task | Result | Tests passed | Notes |\n|---|---|---|---|---|\n"
           "| 2026-10-28 | M5-01 | done | 800 passed, 0 failed | sonnet |\n"
           "| 2026-10-29 | M5-06 | done | 840 passed, 0 failed | sonnet |\n\n"
           "## Blocked\n\n- M5-07: the suite ran out of time\n\n## Open questions\n\nNone.\n")

    def setUp(self):
        super().setUp()
        self.queue = self.temp / "TASKS.md"
        self.log = self.temp / "PROGRESS.md"
        self.queue.write_text(self.QUEUE, encoding="utf-8")
        self.log.write_text(self.LOG, encoding="utf-8")
        # why: DriverTest does not replace DRIVER_PID, and cli_tidy reads it, so without this the test would ask
        # whether the owner's real driver is running
        patches = [mock.patch.object(C, "TASKS", self.queue),
                   mock.patch.object(T, "PROGRESS", self.log),
                   mock.patch.object(T, "STATUS", self.temp / "STATUS.md"),
                   mock.patch.object(C, "DRIVER_PID", self.temp / "driver.pid")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_an_unparsed_heading_is_listed(self):
        self.assertEqual(T.ANY_HEADING.pattern, C.CFG.task_pattern, "the heading pattern is the config's")
        self.queue.write_text(self.QUEUE + "\n### [ ] M14-01\n**Goal.** x.\n", encoding="utf-8")
        self.assertEqual(T.unparsed_headings(self.queue.read_text(encoding="utf-8")), ["### [ ] M14-01"])
        text = T.status_text()
        self.assertIn("## Unparsed headings (1)", text)
        self.assertIn("- ### [ ] M14-01", text)
        self.queue.write_text(self.QUEUE, encoding="utf-8")
        self.assertEqual(T.unparsed_headings(self.QUEUE), [])
        self.assertNotIn("Unparsed headings", T.status_text())

    def test_lint_tasks_names_each_fault(self):
        root = self.temp / "lint"
        (root / "loop").mkdir(parents=True)
        (root / "game").mkdir()
        queue = root / C.TASKS_REL
        good = "### [ ] M14-02 · Fine\n**Files.** Change `src/a.gd`.\n\n**Done when.** test `test_never_written`\n"
        queue.write_text(good, encoding="utf-8")
        clean = T.lint_tasks(root)
        self.assertTrue(clean and all(": files: " in line for line in clean), clean)
        self.assertIn("M14-02: files: game/src/a.gd", clean, "files_prefix goes in front of a path under no top folder")
        bad = ("### [ ] M14-01\n**Goal.** x.\n\n" + good +
               "\n### [ ] M14-03 · Bad\n**Files.** Change `src/b.gd`.\n\n**Done when.** file `loop/NOTES.md`\n")
        queue.write_text(bad, encoding="utf-8")
        found = T.lint_tasks(root)
        self.assertTrue(any("unparsed heading" in line and "M14-01" in line for line in found), found)
        self.assertTrue(any(line.startswith("M14-03:") and "loop/NOTES.md" in line for line in found), found)

    def test_status_names_what_is_left_blocked_and_done(self):
        T.write_status()
        text = (self.temp / "STATUS.md").read_text(encoding="utf-8")
        self.assertIn("## Left to do (2)", text)
        self.assertIn("1. **M5-B** · The five cases that miss  ← next", text)
        self.assertIn("2. **M5-R** · Opus review", text)
        self.assertIn("## Blocked (1)", text)
        self.assertIn("**M5-07**", text)
        self.assertIn("the suite ran out of time", text)
        self.assertIn("## Done (2)", text)
        # newest first, and the done spec that is still in the queue is not counted twice
        self.assertLess(text.index("M5-06"), text.index("M5-01"))

    def test_no_status_file_in_the_config_writes_none(self):
        with mock.patch.object(C, "STATUS_REL", ""):
            self.assertIsNone(T.status_file())
            self.assertIsNone(T.status_file(self.temp))
            T.write_status()
        self.assertFalse((self.temp / "STATUS.md").exists())
        self.assertEqual(T.status_file(self.temp), self.temp / C.STATUS_REL, "a slot's status file is at the same path")

    def test_the_counts_say_how_far_the_queue_has_come(self):
        """The owner asks the phone for status to hear how many tasks are done of the total, so the counts come from
        the queue and the log together: a task is done once it is in the log, left while its heading is open."""
        found = T.counts()
        self.assertEqual((found.done, found.left, found.blocked, found.waiting), (2, 2, 1, 0))
        self.assertEqual(found.total, 5)
        self.assertEqual(found.milestone, "M5")
        self.assertEqual((found.milestone_done, found.milestone_total), (2, 5))

    def test_the_estimate_is_the_queue_one_task_at_a_time(self):
        """The phone cannot simulate the scheduler, so its estimate adds up each open task's own time. An Opus task
        counts for SENIOR_MINUTES and a worker task for its session and its gate."""
        self.assertAlmostEqual(T.remaining_minutes(), 2 * T.WORKER_MINUTES + 2 * T.GATE_MINUTES)
        senior = self.QUEUE.replace("### [ ] M5-R · Opus review\n**Goal.** Review.",
                                    "### [ ] M5-R · Opus review\n**Who.** Opus\n\n**Goal.** Review.")
        self.queue.write_text(senior, encoding="utf-8")
        self.assertAlmostEqual(T.remaining_minutes(), T.WORKER_MINUTES + T.GATE_MINUTES + T.SENIOR_MINUTES)

    def test_the_progress_line_carries_the_counts_and_the_hours(self):
        """One line for the phone and for --check: how many are done of the total, what is left, and how long it
        takes."""
        text = T.progress_text()
        self.assertIn("Done 2/5", text)
        self.assertIn("2 left", text)
        self.assertIn("1 blocked", text)
        self.assertIn("one at a time", text)
        self.assertIn("0.3 h", text)

    def test_the_progress_line_says_when_the_queue_is_empty(self):
        self.queue.write_text(self.QUEUE.replace("### [ ]", "### [x]"), encoding="utf-8")
        text = T.progress_text()
        self.assertIn("The queue is empty", text)
        self.assertNotIn("one at a time", text)

    def test_status_says_so_when_nothing_is_left(self):
        self.queue.write_text(self.QUEUE.replace("### [ ]", "### [x]"), encoding="utf-8")
        T.write_status()
        text = (self.temp / "STATUS.md").read_text(encoding="utf-8")
        self.assertIn("## Left to do (0)", text)
        self.assertIn("The driver stops when the queue is empty", text)

    def test_the_runs_of_the_queue_lose_nothing(self):
        """--tidy rewrites the task file, so the split into header, sections and tasks has to be lossless."""
        lines = self.QUEUE.split("\n")
        parts = T.runs(lines)
        self.assertEqual([line for _, _, body in parts for line in body], lines)
        self.assertEqual([name for kind, name, _ in parts if kind == "task"],
                         ["M5-01", "M5-07", "M5-B", "M5-R"])
        self.assertEqual([name for kind, name, _ in parts if kind == "section"],
                         ["How to read a task", "Phase 8 · M5"])

    def test_tidy_moves_only_the_done_specs_out(self):
        with mock.patch.object(C, "PROJECT", self.temp):
            (self.temp / "loop" / "archive").mkdir(parents=True)
            self.assertEqual(T.cli_tidy(), 0)
            left = self.queue.read_text(encoding="utf-8")
            moved = (self.temp / C.ARCHIVE_REL).read_text(encoding="utf-8")
        self.assertNotIn("M5-01", left, "the done spec has gone")
        self.assertIn("### [x] M5-01", moved)
        self.assertIn("**Goal.** Parsers.", moved)
        for kept in ("### [!] M5-07", "### [ ] M5-B", "### [ ] M5-R", "## How to read a task", "## Phase 8"):
            self.assertIn(kept, left, f"{kept} must stay in the queue")

    def test_tidy_refuses_while_the_driver_runs(self):
        C.DRIVER_PID.write_text(str(os.getpid()), encoding="utf-8")
        with mock.patch.object(C, "PROJECT", self.temp):
            self.assertEqual(T.cli_tidy(), 1)
        self.assertIn("### [x] M5-01", self.queue.read_text(encoding="utf-8"), "nothing moved")

    def test_a_section_whose_tasks_all_went_goes_too(self):
        self.queue.write_text(self.QUEUE.replace("### [!] M5-07", "### [x] M5-07")
                              .replace("### [ ] M5-B", "### [x] M5-B").replace("### [ ] M5-R", "### [x] M5-R"),
                              encoding="utf-8")
        with mock.patch.object(C, "PROJECT", self.temp):
            (self.temp / "loop" / "archive").mkdir(parents=True)
            T.cli_tidy()
        left = self.queue.read_text(encoding="utf-8")
        self.assertNotIn("## Phase 8", left, "an emptied milestone leaves no bare heading")
        self.assertIn("## How to read a task", left, "a section that never held a task is the file's own text")


class StartCommitTest(DriverTest):
    """A rule the driver reads once takes effect only after a restart, so --check says which commit it started from."""

    def setUp(self):
        super().setUp()
        patches = [mock.patch.object(C, "DRIVER_PID", self.temp / "driver.pid"),
                   mock.patch.object(C, "SUPERVISOR_PID", self.temp / "driver-supervisor.pid"),
                   mock.patch.object(C, "pid_alive", lambda pid: pid == os.getpid()),
                   # why: the driver's own repository is not the fixture project, and its real history must not be read
                   mock.patch.object(C, "DRIVER_REPO", self.temp),
                   mock.patch.object(C, "DRIVER_REL", "runner/driver"),
                   mock.patch.object(T, "progress_text", return_value="Done 0/0")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        spec = importlib.util.spec_from_file_location("driver_main", pathlib.Path(M.__file__).parent / "__main__.py")
        self.entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.entry)

    def first_line(self):
        out = io.StringIO()
        with mock.patch("builtins.print", lambda *a, **k: REAL_PRINT(*a, **{**k, "file": out})), \
                mock.patch.object(sys, "argv", ["check_driver", "1"]):
            check_driver.main()
        return out.getvalue().splitlines()[0]

    def test_check_names_the_commit_the_driver_started_from(self):
        C.write_driver_state({"task": None})
        C.DRIVER_PID.write_text(str(os.getpid()), encoding="utf-8")
        self.assertIn("start commit unknown", self.first_line(), "a driver started before the record existed")
        with mock.patch.object(C, "git", return_value=mock.Mock(stdout="abc1234 M11-F5: the start commit\n")):
            self.entry.record_start()
        state = C.read_json(C.DRIVER_STATE)
        self.assertEqual((state["start_pid"], state["start_commit"]), (os.getpid(), "abc1234 M11-F5: the start commit"))
        self.assertEqual(state["task"], None, "the rest of the run's record is kept")
        line = self.first_line()
        self.assertTrue(line.startswith("RUNNING"), line)
        self.assertIn("started from abc1234 M11-F5: the start commit", line)
        C.write_driver_state({**state, "start_pid": os.getpid() + 1})
        self.assertIn("start commit unknown", self.first_line(), "another process's record is not this driver's")
        C.DRIVER_PID.unlink()
        self.assertIn("last started from abc1234", self.first_line(), "a stopped driver still says where it began")

    def test_the_packet_names_a_driver_older_than_its_code(self):
        """A review packet built by a driver older than its code reads with rules that may have changed since, so the
        packet says so, and names the commits it lacks."""
        state = {"start_pid": os.getpid(), "start_commit": "abc1234 M11-F2: the start commit"}
        C.write_driver_state(state)
        asked = []

        def fake_git(*args, **kwargs):
            asked.append(args)
            listed = commits if args[0] == "log" and args[-1] == C.DRIVER_REL else ""
            return mock.Mock(stdout=listed, returncode=0)

        commits = "5e8762b M11-F3: slots.duplicates reads keyed files\n"
        with mock.patch.object(C, "git", fake_git):
            self.assertEqual(check_driver.start_text(state, os.getpid()),
                             "started from abc1234 M11-F2: the start commit; 1 commits to the driver's code since: "
                             "restart the driver")
            self.assertIn(("log", "--format=%h %s", "abc1234..HEAD", "--", "runner/driver"), asked)
            first = T.build("M12-R", "**Goal.** Review.\n").splitlines()[:2]
        self.assertEqual(first[0], "Driver: started from abc1234 M11-F2: the start commit")
        self.assertIn("older than the driver's at HEAD", first[1])
        self.assertIn("5e8762b M11-F3", first[1])
        commits = ""
        with mock.patch.object(C, "git", fake_git):
            self.assertEqual(check_driver.start_text(state, os.getpid()),
                             "started from abc1234 M11-F2: the start commit")
            first = T.build("M12-R", "**Goal.** Review.\n").splitlines()[:2]
        self.assertEqual(first, ["Driver: started from abc1234 M11-F2: the start commit", ""])
        with mock.patch.object(C, "git", return_value=mock.Mock(stdout="", returncode=128)):
            self.assertEqual(check_driver.stale_driver_commits(state), [], "git failing names no commits")
        self.assertEqual(check_driver.stale_driver_commits({}), [], "no start commit, no commits")
        with mock.patch.object(C, "DRIVER_REPO", None), mock.patch.object(C, "git") as git:
            self.assertEqual(check_driver.stale_driver_commits(state), [], "a driver in no repository")
        git.assert_not_called()

    def test_a_start_beside_a_live_driver_records_nothing(self):
        C.write_driver_state({"start_pid": 1, "start_commit": "old1234 earlier"})
        C.DRIVER_PID.write_text(str(os.getpid() + 1), encoding="utf-8")
        with mock.patch.object(C, "pid_alive", return_value=True), mock.patch.object(C, "git") as git:
            self.entry.record_start()
        git.assert_not_called()
        self.assertEqual(C.read_json(C.DRIVER_STATE)["start_commit"], "old1234 earlier")


class SessionChildTest(DriverTest):
    def test_a_session_is_killed_when_reading_its_stream_raises(self):
        """A log line that raised skipped process.wait(), and the session went on editing its slot after the driver had
        undone that slot and moved on to the next task."""
        process = mock.Mock(pid=4242, stdout=["{ bad json"], poll=mock.Mock(return_value=None))
        killed = []
        with mock.patch.object(X.subprocess, "Popen", return_value=process), \
                mock.patch.object(C, "kill_tree", lambda pid: killed.append(pid)), \
                mock.patch.object(X, "task_settings", return_value="settings.json"), \
                mock.patch.object(X, "polling", mock.mock_open()), \
                mock.patch.object(C, "log", mock.Mock(side_effect=UnicodeEncodeError("charmap", "x", 0, 1, "no"))):
            with self.assertRaises(UnicodeEncodeError):
                X.run_session("do it", "M5-R", "opus", "", 10, 60, 5.0, "opus")
        self.assertEqual(killed, [4242])
        self.assertNotIn("opus", C.CHILDREN)

    def test_a_session_that_ended_on_its_own_is_not_killed(self):
        process = mock.Mock(pid=4242, stdout=[], poll=mock.Mock(return_value=0), wait=mock.Mock(return_value=0))
        killed = []
        with mock.patch.object(X.subprocess, "Popen", return_value=process), \
                mock.patch.object(C, "kill_tree", lambda pid: killed.append(pid)), \
                mock.patch.object(X, "task_settings", return_value="settings.json"), \
                mock.patch.object(X, "polling", mock.mock_open()):
            X.run_session("do it", "M5-R", "opus", "", 10, 60, 5.0, "opus")
        self.assertEqual(killed, [])


class ParsingTest(DriverTest):
    def test_done_when_wraps_to_the_next_blank_line(self):
        spec = "**Done when.** test `test_a` and file `loop/reviews/M2-review.md`\nand output `smoke: PASS`\n\nmore"
        self.assertEqual(SL.done_when(spec), [("test", "test_a"), ("file", "loop/reviews/M2-review.md"),
                                             ("output", "smoke: PASS")])

    def test_a_done_when_test_is_found_only_in_the_test_files(self):
        """A test is a test function in a file the config's test_files globs match; one anywhere else does not count."""
        (self.temp / "game" / "tests" / "unit").mkdir(parents=True)
        (self.temp / "tools").mkdir()
        (self.temp / "game" / "tests" / "unit" / "test_a.gd").write_text("func test_parser(\n", encoding="utf-8")
        (self.temp / "tools" / "test_b.gd").write_text("func test_tool(\n", encoding="utf-8")
        self.assertEqual(SL.check_done_when("**Done when.** test `test_parser`", "", self.temp), "")
        self.assertIn("no test function test_tool", SL.check_done_when("**Done when.** test `test_tool`", "", self.temp))
        self.assertIn("no test function test_none", SL.check_done_when("**Done when.** test `test_none`", "", self.temp))
        self.assertIn("no **Done when.** line", SL.check_done_when("**Goal.** x.", "", self.temp))

    def test_reset_time_is_within_a_day(self):
        reset = B.reset_time("You've hit your session limit · resets 1:20pm (America/Los_Angeles)")
        self.assertTrue(time.time() < reset <= time.time() + 86400)
        self.assertIsNone(B.reset_time("some other error"))

    def test_settings_take_old_names_and_refuse_a_bad_model(self):
        self.addCleanup(C.apply_settings, dict(C.SETTING_DEFAULTS))
        values = C.read_settings("```driver-settings\nhaiku_turns: 55\nescalate_model: none\n```")
        self.assertEqual(values["worker_turns"], "55")
        C.apply_settings(values)
        self.assertEqual(C.ESCALATE_MODEL, "")
        self.assertEqual(C.WORKER_TURNS, 55)
        values["escalate_model"] = "gpt"
        with self.assertRaises(ValueError):
            C.apply_settings(values)


class WorkspaceTest(DriverTest):
    BEFORE = "func test_a():\n\tassert_eq(1, 1)\n\tassert_true(true)\n\n\nfunc test_b():\n\tassert_true(true)\n"

    def test_assert_counts_per_test_function(self):
        text = self.BEFORE + "\n\nfunc _helper():\n\tassert_true(true)\n"
        self.assertEqual(SL.assert_counts(text), {"test_a": 2, "test_b": 1})

    def test_fewer_asserts_in_a_test_the_task_does_not_name_is_weakened(self):
        now = self.BEFORE.replace("\tassert_true(true)\n\n\nfunc test_b", "\tpass\n\n\nfunc test_b")
        found = SL.weakened(self.BEFORE, now, "spec", "game/tests/test_x.gd")
        self.assertEqual(found, ["test_x.test_a (2 to 1 asserts)"])
        self.assertEqual(SL.weakened(self.BEFORE, now, "rewrite test_a's assert", "game/tests/test_x.gd"), [])
        self.assertEqual(SL.weakened(self.BEFORE, now, "in test_x, ...", "game/tests/test_x.gd"), [])

    def test_a_removed_test_the_task_names_is_allowed(self):
        self.assertEqual(SL.removed({"test_a", "test_b"}, {"test_b"}, ""), ["test_a"])
        self.assertEqual(SL.removed({"test_a", "test_b"}, {"test_b"}, "Retire test_a: the rule moved."), [])

    def test_allowed_loop_files_come_from_the_files_and_done_when_lines(self):
        spec = ("**Files.** Change `loop/NOTES.md` and `game/src/a.gd`.\n\n"
                "**Done when.** file `loop/reviews/M3-review.md`\n")
        self.assertEqual(SL.allowed_loop_files(spec, False), {"loop/NOTES.md", "loop/reviews/M3-review.md"})
        self.assertIn("loop/TASKS.md", SL.allowed_loop_files(spec, True))

    def test_loop_changes_flag_the_driver_but_not_named_or_earlier_files(self):
        snap = {"head": "abc", "dirty": {"loop/LOOP.md"}}
        changed = {"loop/LOOP.md", "loop/driver.py", "loop/reviews/x.md", "game/src/a.gd"}
        with mock.patch.object(C, "changed_paths", return_value=changed):
            self.assertEqual(SL.loop_changes(snap, {"loop/reviews/"}), ["loop/driver.py"])
            self.assertEqual(SL.outside_scope(snap, ["game/tests"]), ["game/src/a.gd"])

    def test_a_file_the_task_names_is_in_scope(self):
        snap = {"head": "abc", "dirty": set()}
        changed = {"game/build.cfg", "game/tests/test_a.gd"}
        spec = ("### M9-01 - Title\n"
                "**Files.** Change `game/build.cfg`, `tests/test_a.gd`.\n\n"
                "**Done when.** test `test_a`\n")
        with mock.patch.object(C, "changed_paths", return_value=changed):
            named = SL.task_files(spec)
            self.assertIn("game/build.cfg", named)
            self.assertIn("game/tests/test_a.gd", named, "files_prefix goes in front of a path under no top folder")
            self.assertEqual(SL.outside_scope(snap, ["game/tests"], None, named), [])
            self.assertEqual(SL.outside_scope(snap, ["game/tests"]), ["game/build.cfg"])

    def test_a_files_line_path_loses_the_configs_strip_prefix(self):
        spec = "**Files.** Change `res://src/core/world.gd` and `res://tests/`.\n"
        self.assertEqual(SL.task_files(spec), ["game/src/core/world.gd", "game/tests/"])
        self.assertIn("mixes res://", SL.mixed_paths("**Files.** Change `res://src/a.gd` and `game/src/b.gd`.\n"))
        self.assertEqual(SL.mixed_paths(spec), "")

    def test_a_named_file_outside_the_scope_lands_with_the_task(self):
        spec = ("### M9-F3 - Title\n"
                "**Files.** Change `game/tools/run_tests.py`, `loop/notes/test_plan.md`.\n\n"
                "**Done when.** file `loop/notes/test_plan.md`\n")
        with mock.patch.object(C, "SCOPE", ["game/src", "game/tests", "game/content"]):
            paths = SL.landing_paths(spec)
        self.assertIn("game/tools/run_tests.py", paths, "the gate let the task change it, so the landing keeps it")
        self.assertIn("loop/notes/test_plan.md", paths)
        self.assertIn("game/src", paths, "the scope still lands")
        self.assertEqual(len(paths), len(set(paths)), "each path once")

    def test_a_failed_test_run_names_its_tests(self):
        output = ("res://tests/test_parser.gd\n  PASS test_parser_reads (12 ms)\n  FAIL test_parser_holds (40 ms)\n"
                  "    expected 3, got 2\nres://tests/test_planner.gd\n  FAIL test_planner_walks (5 ms)\n"
                  "  PASS test_planner_waits (3 ms)\n1688 passed, 2 failed\nsmoke: PASS\n")
        self.assertEqual(C.failing_tests(output), ["test_parser_holds", "test_planner_walks"])
        spec = "### M9-01 - Title\n**Files.** Change `src/core/world.gd`.\n\n**Done when.** test `test_a`\n"
        with mock.patch.object(C, "changed_paths", return_value={"game/src/a.gd"}), \
                mock.patch.object(C, "SCOPE", ["game/src"]), \
                mock.patch.object(X, "run_tests", return_value=output):
            reason, totals = RN.gate(fake_slot(root=self.temp), spec, False, {})
        self.assertEqual(totals, "1688 passed, 2 failed")
        self.assertIn("1688 passed, 2 failed", reason)
        self.assertTrue(reason.endswith("failing: test_parser_holds, test_planner_walks"), reason)
        many = "".join(f"  FAIL test_a_rather_long_name_for_a_test_number_{n} (1 ms)\n" for n in range(9))
        long_reason = RN.named_reason("the driver's own test run: " + "x" * 300, C.failing_tests(many))
        self.assertLessEqual(len(long_reason), 200, "the reason stays within the length it is cut to")
        self.assertIn("test_a_rather_long_name_for_a_test_number_0", long_reason, "a name is kept whole")
        self.assertIn("more", long_reason, "the names left out are counted")
        self.assertTrue(long_reason.startswith("the driver's own test run: "))
        self.assertEqual(RN.named_reason("the driver's own test run: no totals printed", []),
                         "the driver's own test run: no totals printed")

    def test_a_fail_marker_fails_the_gate_and_names_no_test(self):
        """A run whose totals pass still fails on a line that starts with one of fail_markers; that line is no test's
        name, so the reason must not offer one."""
        output = "5 passed, 0 failed\nsmoke: FAIL the window did not open\n"
        self.assertEqual(C.test_failure(output), "smoke: FAIL the window did not open")
        self.assertEqual(C.test_failure("5 passed, 0 failed\nsmoke: PASS\n"), "")
        self.assertEqual(C.test_failure("4 passed, 1 failed\n"), "4 passed, 1 failed")
        self.assertEqual(C.test_failure(""), "no totals printed")
        spec = "### M9-01 - Title\n**Files.** Change `src/core/world.gd`.\n\n**Done when.** test `test_a`\n"
        with mock.patch.object(C, "changed_paths", return_value={"game/src/a.gd"}), \
                mock.patch.object(C, "SCOPE", ["game/src"]), \
                mock.patch.object(X, "run_tests", return_value=output):
            reason, _ = RN.gate(fake_slot(root=self.temp), spec, False, {})
        self.assertTrue(reason.startswith("the driver's own test run: smoke: FAIL the window did not open"), reason)
        self.assertNotIn("failing:", reason, "the words after a fail marker are not a test's name")

    def test_failing_tests_follow_the_configs_failure_regex(self):
        output = ("..F.E.\n======================================================================\n"
                  "FAIL: test_a_gate_blocks (test_driver.GateTest.test_a_gate_blocks)\n"
                  "----------------------------------------------------------------------\nTraceback ...\n"
                  "AssertionError: 1 != 2\n======================================================================\n"
                  "ERROR: test_slots_rebase (test_slots.SlotTest.test_slots_rebase)\n"
                  "FAIL: test_a_gate_blocks (test_driver.GateTest.test_a_gate_blocks)\n"
                  "Ran 171 tests in 30.1s\n\nFAILED (failures=1, errors=1)\n")
        with mock.patch.object(C, "FAILURE", re.compile(r"^(?:FAIL|ERROR): (\w+)", re.M)):
            self.assertEqual(C.failing_tests(output), ["test_a_gate_blocks", "test_slots_rebase"], "each name once")
        with mock.patch.object(C, "FAILURE", None):
            self.assertEqual(C.failing_tests(output), [], "no failure_regex, no names")

    def commit_during(self, owner):
        """commit_made_during with a slot whose HEAD moved to a commit that git names 'abc1234 M2-07: blocked'."""
        made = RN.subprocess.CompletedProcess([], 0, stdout="abc1234 M2-07: blocked", stderr="")
        with mock.patch.object(C, "head", return_value="new"):
            with mock.patch.object(C, "git", return_value=made):
                with mock.patch.object(SL, "other_owner", return_value=owner):
                    return RN.commit_made_during(fake_slot(base="old"), "M2-08")

    def test_a_commit_during_a_task_pauses_and_leaves_it_open(self):
        state = C.RUNTIME["state"]
        self.assertTrue(self.commit_during(0))
        self.assertTrue(state["paused"])
        self.assertIn("abc1234 M2-07: blocked", self.posts[-1][1])
        self.assertNotIn("Another driver", self.posts[-1][1])
        with mock.patch.object(C, "head", return_value="old"):
            self.assertFalse(RN.commit_made_during(fake_slot(base="old"), "M2-08"))

    def test_a_commit_during_a_task_names_a_rival_driver_when_one_holds_the_slots(self):
        self.assertTrue(self.commit_during(4242))
        self.assertIn("Another driver (pid 4242)", self.posts[-1][1])

    def test_an_idle_slot_records_why(self):
        scheduler = SC.Scheduler({})
        with mock.patch.object(C, "emit") as emit:
            scheduler.record_idle("paused")
            scheduler.record_idle("paused")
            scheduler.record_idle("over the daily budget")
        reasons = [call.kwargs["reason"] for call in emit.call_args_list]
        self.assertEqual(reasons, ["paused", "over the daily budget"], "each reason is recorded once until it changes")
        self.assertTrue(all(call.args == ("schedule_wait",) for call in emit.call_args_list))

    def test_the_busy_slots_survive_in_driver_json(self):
        state = {}
        scheduler = SC.Scheduler(state)
        scheduler.slots["1"].task = "M3-07"
        scheduler.slots["1"].base = "abc"
        scheduler.save()
        saved = C.read_driver_state()
        self.assertEqual([(item["slot"], item["task"], item["base"]) for item in saved["slots"]], [("1", "M3-07", "abc")])
        self.assertTrue(saved["busy"])


class ReliabilityTest(DriverTest):
    def test_a_hung_test_run_is_killed_and_has_no_totals(self):
        script = self.temp / "hang.py"
        script.write_text("import time\nprint('1 passed, 0 failed')\ntime.sleep(60)\n", encoding="utf-8")
        with mock.patch.object(C, "TEST_COMMAND", [sys.executable, str(script)]):
            started = time.time()
            output = C.test_output(timeout=2)
        self.assertLess(time.time() - started, 30)
        self.assertEqual(C.totals_line(output), "")
        self.assertIn("timed out", output)

    def test_claude_is_the_newest_extension_copy(self):
        for version in ("2.1.99", "2.1.311", "2.1.310"):
            exe = self.temp / f"anthropic.claude-code-{version}-win32-x64" / "resources" / "native-binary" / "claude.exe"
            exe.parent.mkdir(parents=True)
            exe.write_text("")
        with mock.patch.dict(os.environ, {"CLAUDE_EXE": ""}), mock.patch.object(C, "EXTENSIONS", self.temp), \
                mock.patch.object(C.shutil, "which", return_value=None):
            self.assertIn("2.1.311", C.find_claude())

    def test_a_session_gets_what_is_left_of_the_day(self):
        with mock.patch.object(C, "spend_on", return_value=110.0), mock.patch.object(C, "DAILY_BUDGET_USD", 120.0):
            self.assertEqual(B.session_budget(30.0), 10.0)
            self.assertTrue(C.ctx()["budget_cut"])
            self.assertTrue(B.cut_by_daily_budget("error_max_budget_usd"))
            B.release_reservation()
        with mock.patch.object(C, "spend_on", return_value=10.0), mock.patch.object(C, "DAILY_BUDGET_USD", 120.0):
            self.assertEqual(B.session_budget(30.0), 30.0)
            self.assertFalse(B.cut_by_daily_budget("error_max_budget_usd"))
            B.release_reservation()

    def test_blocks_in_a_row_queue_a_triage_then_pause(self):
        state = C.RUNTIME["state"]
        state["block_streak"] = [{"task": "M2-08", "reason": "pinned"}, {"task": "M2-10", "reason": "pinned"}]
        T.after_blocks(state)
        self.assertIn("M2-10: pinned", state["opus_writes"][0])
        self.assertFalse(state.get("paused"))
        T.after_blocks(state)
        self.assertEqual(len(state["opus_writes"]), 1)
        state["block_streak"] += [{"task": "M2-12", "reason": "x"}, {"task": "M2-13", "reason": "x"}]
        T.after_blocks(state)
        self.assertTrue(state["paused"])
        self.assertEqual(state["block_streak"], [])

    def test_a_commit_ends_the_streak(self):
        state = {"block_streak": [{"task": "a", "reason": "b"}], "triaged": True}
        T.task_committed(state)
        self.assertEqual((state["block_streak"], state["triaged"]), ([], False))

    def test_autostart_respects_an_owner_stop(self):
        C.write_driver_state({"ended_by": "owner"})
        with mock.patch.object(M, "supervise") as supervise:
            self.assertEqual(M.autostart(), 0)
            supervise.assert_not_called()
            C.write_driver_state({"ended_by": None})
            M.autostart()
            supervise.assert_called_once()

    def test_runners_pick_the_opus_task_by_its_who_line(self):
        pick = lambda spec: next(run for kind, run in RN.RUNNERS if kind(spec))  # noqa: E731
        self.assertIs(pick("### [ ] M3-F4 · x\n**Who.** Opus\n"), RN.do_senior_task)
        self.assertIs(pick("### [ ] M2-08 · x\n"), RN.do_worker_task)

    def test_runners_pick_the_fable_task_by_its_who_line(self):
        pick = lambda spec: next(run for kind, run in RN.RUNNERS if kind(spec))  # noqa: E731
        self.assertIs(pick("### [ ] M5-A · x\n**Who.** Fable\n"), RN.do_senior_task)
        self.assertEqual(T.senior_model("**Who.** Fable\n"), "fable")
        self.assertEqual(T.senior_model("**Who.** Opus\n"), "opus")
        self.assertEqual(T.senior_model("**Who.** Fableish\n"), "")

    def test_an_opus_task_asks_for_its_effort_in_an_effort_line(self):
        self.assertEqual(T.senior_effort("**Who.** Opus\n**Effort.** medium\n"), "medium")
        self.assertEqual(T.senior_effort("**Who.** Opus\n"), C.OPUS_EFFORT)
        self.assertEqual(T.senior_effort("**Who.** Opus\n**Effort.** loud\n"), C.OPUS_EFFORT)
        self.assertEqual(T.senior_effort("**Who.** Fable\n"), C.FABLE_EFFORT)
        self.assertEqual(T.senior_effort("**Who.** Fable\n**Effort.** xhigh\n"), "xhigh")

    def run_fable_audit(self, scope_files):
        """Runs an audit task with every outside step replaced. Returns the mocks for the gate, the landing and block."""
        (self.temp / "CLAUDE.md").write_text("rules", encoding="utf-8")
        C.RUNTIME["state"] = {}
        spec = "### [ ] M5-A · Fable audit\n**Who.** Fable\n\n**Files.** Create `loop/reviews/M5-audit.md`.\n"
        with mock.patch.object(K, "approved", return_value=True) as approved, \
                mock.patch.object(X, "run_session", return_value=("report written", "")) as session, \
                mock.patch.object(RN, "commit_made_during", return_value=False), \
                mock.patch.object(T, "safe_build", return_value="packet"), \
                mock.patch.object(RN, "scope_changes", return_value=scope_files), \
                mock.patch.object(RN, "check_passed", return_value=("", "")) as gate, \
                mock.patch.object(RN, "land", return_value="") as land, \
                mock.patch.object(T, "announce"), mock.patch.object(T, "block") as block:
            RN.do_senior_task(fake_slot(root=self.temp), "M5-A", "Fable audit", spec, C.RUNTIME["state"])
        self.assertIn("Fable, effort high", approved.call_args[0][1])
        self.assertEqual(session.call_args[0][2], C.FABLE_MODEL)
        self.assertIn("packet", session.call_args[0][0], "an audit's id gets the review packet")
        return gate, land, block

    def test_a_fable_audit_that_only_writes_its_report_lands(self):
        gate, land, block = self.run_fable_audit([])
        gate.assert_called_once()
        block.assert_not_called()
        self.assertIn(C.WORKER_MODELS["fable"], land.call_args[0][4])

    def test_a_fable_audit_that_changes_the_scope_is_blocked(self):
        gate, land, block = self.run_fable_audit(["game/src/core/world.gd"])
        gate.assert_not_called()
        land.assert_not_called()
        self.assertIn("an audit changes nothing in the scope", block.call_args[0][3])
        self.assertIn("game/src/core/world.gd", block.call_args[0][3])
        self.assertEqual(block.call_args[0][4], "fable_failed")


class LoopProblemTest(DriverTest):
    def test_a_session_that_never_started_pauses_instead_of_blocking(self):
        C.ctx()["last_session"] = {"turns": 0, "seconds": 1}
        self.assertTrue(B.never_started("M2-20", "Your organization has disabled Claude subscription access"))
        self.assertTrue(C.RUNTIME["state"]["paused"])
        self.assertGreater(C.RUNTIME["state"]["resume_at"], time.time())
        C.ctx()["last_session"] = {"turns": 12, "seconds": 1}
        self.assertFalse(B.never_started("M2-20", "x"))
        C.ctx()["last_session"] = {"turns": 0, "seconds": 549}
        self.assertFalse(B.never_started("M2-30", "x"))

    def test_a_failure_names_the_limit_that_stopped_it(self):
        result = {"is_error": True, "num_turns": 40, "terminal_reason": "max_turns", "subtype": "error_max_turns"}
        text = X.failure_text("haiku", 1, 549, result)
        self.assertIn("stopped by max_turns after 40 turns", text)
        self.assertNotIn("stopped by", X.failure_text("haiku", 1, 5, {"result": "boom"}))

    def test_task_sessions_refuse_cd_through_their_settings(self):
        hooks = json.loads(X.task_settings())["hooks"]["PreToolUse"][0]
        self.assertIn("Bash", hooks["matcher"].split("|"))
        self.assertIn("task_hook.py", hooks["hooks"][0]["command"])
        for command in ("cd game/src", "ls && cd game; python x", "(cd game && ls)", "pushd loop"):
            self.assertTrue(TH.changes_folder(command), command)
        for command in ('grep -rn "cd " game', "python game/tools/run_tests.py", "echo abcd"):
            self.assertFalse(TH.changes_folder(command), command)

    def test_a_cd_inside_quotes_is_not_a_change_of_folder(self):
        for command in ('grep -rn "a\\|cd b" loop', "echo 'x; cd y'", "cat <<'EOF' > f.txt\nls | cd x\nEOF\nls",
                        'echo "say \\"x\\"; cd y"'):
            self.assertFalse(TH.changes_folder(command), command)
        for command in ("ls | cd x", "cd x", "a && cd x", "(cd x)", "echo 'q' && cd x", "cat <<EOF\nhi\nEOF\ncd x"):
            self.assertTrue(TH.changes_folder(command), command)
        self.assertIn("cd <the project root> &&", TH.CD_REASON)

    def test_a_committed_task_leaves_the_blocked_list(self):
        progress = self.temp / "PROGRESS.md"
        progress.write_text("## Blocked\n\n- M2-30: ran out of turns\n- M2-31: cd\n(task id: what failed)\n\n## Next\n- M2-30: kept\n",
                            encoding="utf-8")
        with mock.patch.object(T, "PROGRESS", progress):
            T.clear_blocked("M2-30")
        text = progress.read_text(encoding="utf-8")
        self.assertNotIn("- M2-30: ran out", text)
        self.assertIn("- M2-31: cd", text)
        self.assertIn("- M2-30: kept", text)


class SlowSuiteTest(DriverTest):
    """A test run that runs out of time is checked against HEAD's own suite, and a suite near its limit is reported."""
    TIMED_OUT = "unit tests TIMED OUT after 300 s in shard 2 of 4: in test_rounds.gd, after test_x\nunit tests exit=124"
    PASSED = ("617 passed, 0 failed\nunit tests: 41.8 s in 4 shard(s); slowest shard 41.8 s of its 300 s limit; "
              "slowest files: test_harness.gd 32.9 s")

    def setUp(self):
        super().setUp()
        # why: the fixture's config has no timing_regex, and a suite near its limit is read only through one
        patch = mock.patch.object(C.CFG, "timing_regex", r"slowest shard ([\d.]+) s of its (\d+) s limit")
        patch.start()
        self.addCleanup(patch.stop)

    def head_suite(self, output):
        def prepare(slot, fresh=False):
            slot.base = "abc1234567"
            return slot.base
        with mock.patch.object(SL, "prepare", prepare), mock.patch.object(X, "run_tests", return_value=output), \
                mock.patch.object(SL, "revert") as revert:
            note = RN.suite_fault(fake_slot(), "M3-17")
        return note, revert

    def test_timing_line_and_timeouts_are_read(self):
        self.assertEqual(C.suite_load(self.PASSED), (41.8, 300))
        self.assertIsNone(C.suite_load("617 passed, 0 failed"))
        self.assertTrue(C.timed_out(self.TIMED_OUT))
        self.assertTrue(C.timed_out("test run timed out after 1800 s"))
        self.assertFalse(C.timed_out(self.PASSED))
        with mock.patch.object(C.CFG, "timing_regex", ""):
            self.assertIsNone(C.suite_load(self.PASSED), "no timing_regex, no load")

    def test_the_gate_records_how_near_the_suite_came_to_its_limit(self):
        def gate(slot, spec, opus, found):
            found.update(test_seconds=42.0, output=self.PASSED)
            return "", C.totals_line(self.PASSED)
        with mock.patch.object(RN, "gate", gate):
            self.assertEqual(RN.check_passed(fake_slot(root=self.temp), "spec"), ("", "617 passed, 0 failed"))
        events = [json.loads(line) for line in (self.temp / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        found = [event for event in events if event["event"] == "gate"]
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["suite_seconds"], found[0]["suite_limit"]), (41.8, 300))
        self.assertEqual((found[0]["totals"], found[0]["reason"], found[0]["receipt"]), ("617 passed, 0 failed", "", False))
        self.assertIn("617 passed", (self.temp / found[0]["log"]).read_text(encoding="utf-8"), "the output is kept")
        self.assertEqual(self.posts, [], "41.8 s of 300 s is far from the limit")

    def test_a_timed_out_gate_runs_its_tests_again(self):
        with mock.patch.object(X, "run_tests", side_effect=[self.TIMED_OUT, self.PASSED]) as run:
            self.assertEqual(RN.locked_test_run(self.temp), self.PASSED)
        self.assertEqual(run.call_count, 2)
        with mock.patch.object(X, "run_tests", return_value=self.TIMED_OUT) as run:
            self.assertTrue(C.timed_out(RN.locked_test_run(self.temp)))
        self.assertEqual(run.call_count, 2)
        with mock.patch.object(X, "run_tests", return_value=self.PASSED) as run:
            RN.locked_test_run(self.temp)
        self.assertEqual(run.call_count, 1)

    def test_two_gates_do_not_test_at_once(self):
        import threading
        inside, worst, guard = [0], [0], threading.Lock()

        def slow(root):
            with guard:
                inside[0] += 1
                worst[0] = max(worst[0], inside[0])
            time.sleep(0.2)
            with guard:
                inside[0] -= 1
            return self.PASSED
        with mock.patch.object(X, "run_tests", slow), mock.patch.object(RN, "GATE_LOCK_POLL", 0.05):
            threads = [threading.Thread(target=RN.locked_test_run, args=(self.temp,)) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(worst[0], 1)
        self.assertFalse((self.temp / "gate-tests.lock").exists())

    def test_head_too_slow_pauses_and_leaves_the_task_open(self):
        note, revert = self.head_suite(self.TIMED_OUT)
        self.assertIsNone(note)
        revert.assert_called_once()
        self.assertTrue(C.RUNTIME["state"]["paused"])
        self.assertEqual(self.posts[-1][0], "Driver paused: the test suite fails on HEAD")

    def test_head_passing_blames_the_change(self):
        note, revert = self.head_suite(self.PASSED)
        self.assertIn("HEAD's own suite passes", note)
        self.assertIn("41.8 s of 300 s", note)
        self.assertIn("617 passed, 0 failed", note)
        revert.assert_not_called()
        self.assertFalse(C.RUNTIME["state"].get("paused"))

    def test_a_suite_near_its_limit_is_told_once_a_day(self):
        RN.warn_suite_load((150.0, 300))
        self.assertEqual(self.posts, [])
        RN.warn_suite_load((200.0, 300))
        RN.warn_suite_load((250.0, 300))
        self.assertEqual([title for title, _, _ in self.posts], ["Test suite near its limit"])


class AttemptMemoryTest(DriverTest):
    """What a session said at the end reaches the task's next attempt, and a session past its deadline is told to
    write it."""

    def hook_args(self, deadline=0.0, **changes):
        """The task hook's arguments as the driver writes them for a session without a slot (task_settings), read back
        as the hook reads them, with --only as the one partial-run flag."""
        with mock.patch.object(C.CFG, "partial_test_flags", ["--only"]):
            X.task_settings(None, deadline)
        args = TH.parse(["--config", str(C.LOGS / "hook-main.json")])
        for key, value in changes.items():
            setattr(args, key, value)
        return args

    def test_last_words_reach_the_next_attempt_until_the_task_lands(self):
        state = C.RUNTIME["state"]
        C.ctx()["last_words"] = "The cause: Planner._send_workers keeps the worker at the queue."
        RN.record_attempt("M3-17", "opus", "requeued: it needed planner.gd")
        note = RN.attempts_note("M3-17")
        self.assertIn("_send_workers", note)
        self.assertIn("opus", note)
        self.assertIn("requeued: it needed planner.gd", note)
        self.assertEqual(RN.attempts_note("M3-18"), "")
        T.task_committed(state, "M3-17")
        self.assertEqual(RN.attempts_note("M3-17"), "")

    def test_only_the_last_attempts_are_kept(self):
        for index in range(C.MAX_ATTEMPTS_KEPT + 2):
            C.ctx()["last_words"] = f"attempt {index}"
            RN.record_attempt("M3-17", "sonnet", "failed")
        kept = C.RUNTIME["state"]["attempts"]["M3-17"]
        self.assertEqual(len(kept), C.MAX_ATTEMPTS_KEPT)
        self.assertEqual(kept[-1]["words"], f"attempt {C.MAX_ATTEMPTS_KEPT + 1}")

    def test_a_failed_attempt_is_recorded_before_the_escalation(self):
        def attempt(slot, task_id, title, spec, who, level, feedback):
            C.ctx()["last_words"] = f"{who} found the cause"
            return "tests fail" if who == "sonnet" else None
        with mock.patch.object(C, "ESCALATE_MODEL", "opus"), mock.patch.object(C, "PARALLEL_SLOTS", 1), \
                mock.patch.object(RN, "worker_attempt", attempt), \
                mock.patch.object(K, "approved", return_value=True), mock.patch.object(SL, "prepare"):
            RN.do_worker_task(fake_slot(), "M2-30", "x", "spec", {"worker_model": "sonnet"})
        kept = C.RUNTIME["state"]["attempts"]["M2-30"]
        self.assertEqual([(item["model"], item["outcome"]) for item in kept], [("sonnet", "failed: tests fail")])

    def test_a_worker_prompt_lists_its_done_when_tests(self):
        (self.temp / "CLAUDE.md").write_text("rules", encoding="utf-8")
        prompts_seen = []

        def session(prompt, *args):
            prompts_seen.append(prompt)
            C.RUNTIME["stopping"] = True
            return "done", ""
        named = ("### [ ] M9-1 · Parsers\n**Files.** Change `src/a.gd`.\n\n**Done when.** test `test_a`, test "
                 "`test_b` and file `loop/reviews/M9-audit.md`\n")
        bare = "### [ ] M9-2 · Parsers\n**Files.** Change `src/a.gd`.\n"
        with mock.patch.object(X, "run_session", session), mock.patch.object(B, "session_budget", return_value=5.0):
            for spec in (named, bare):
                C.RUNTIME["stopping"] = False
                self.assertIsNone(RN.worker_attempt(fake_slot(root=self.temp), "M9-1", "x", spec, "opus", "", None))
        self.assertEqual(prompts_seen[0].rstrip().splitlines()[-3:],
                         ["test test_a", "test test_b", "file loop/reviews/M9-audit.md"])
        self.assertIn("Before you stop, these must exist and pass:", prompts_seen[0])
        self.assertNotIn("Before you stop", prompts_seen[1])
        self.assertTrue(prompts_seen[1].endswith(C.task_context(bare) + "\n"))
        self.assertIn("rules", prompts_seen[0], "the slot's own rules file is in the prompt")

    def test_a_killed_session_gives_its_last_words(self):
        text = X.failure_text("opus", 1, 1800, {}, ["Every case now ends. Running the suite."])
        self.assertIn("no result", text)
        self.assertIn("Every case now ends", text)
        self.assertNotIn("last words", X.failure_text("opus", 1, 10, {"result": "an error"}, ["said"]))

    def test_the_hook_sends_a_full_test_run_back_for_a_timeout(self):
        args = self.hook_args()
        full = {"tool_name": "Bash", "tool_input": {"command": "python game/tools/run_tests.py"}}
        self.assertEqual(TH.decide(full, args), TH.TIMEOUT_REASON.format(timeout=600000))
        full["tool_input"]["timeout"] = 600000
        self.assertEqual(TH.decide(full, args), "")
        only = {"tool_name": "Bash", "tool_input": {"command": "python game/tools/run_tests.py --only test_parser"}}
        self.assertEqual(TH.decide(only, args), "")
        args.full_timeout_ms = 0
        del full["tool_input"]["timeout"]
        self.assertEqual(TH.decide(full, args), "", "full_run_timeout_ms 0 checks nothing")

    def test_a_read_of_the_test_script_is_not_a_test_run(self):
        call, partial = C.TEST_CALL.pattern, ["--only"]
        self.assertFalse(TH.needs_timeout("git diff 45a0bdb HEAD -- game/tools/run_tests.py", None, call, partial, 600000))
        self.assertFalse(TH.needs_timeout("grep -n shard game/tools/run_tests.py", None, call, partial, 600000))
        self.assertTrue(TH.needs_timeout("python game/tools/run_tests.py", None, call, partial, 600000))
        self.assertFalse(TH.needs_timeout("python game/tools/run_tests.py --only test_x", None, call, partial, 600000))
        self.assertTrue(TH.needs_timeout("echo hi && python game/tools/run_tests.py", None, call, partial, 600000))
        self.assertFalse(TH.runs_tests("grep -n shard game/tools/run_tests.py", call))
        self.assertTrue(TH.runs_tests("python game/tools/run_tests.py --only test_x", call))

    def test_a_cd_to_the_root_is_stripped(self):
        self.assertEqual(TH.strip_root_cd("cd C:/slot && git status", "C:/slot"), "git status")
        self.assertEqual(TH.strip_root_cd("cd /c/slot && ls", "C:/slot"), "ls")
        self.assertEqual(TH.strip_root_cd("cd . && ls", "C:/slot"), "ls")
        self.assertIsNone(TH.strip_root_cd("cd .. && ls", "C:/slot"))
        self.assertIsNone(TH.strip_root_cd("cd game && ls", "C:/slot"))
        self.assertIsNone(TH.strip_root_cd("ls", "C:/slot"))
        event = {"tool_name": "Bash", "tool_input": {"command": "cd game && ls"}}
        self.assertEqual(TH.decide(event, self.hook_args(root="C:/slot")), TH.CD_REASON)
        self.assertEqual(TH.rewrite_cd("cd game && ls"), "(cd game && ls)", "a read-only cd runs in a subshell")
        self.assertIsNone(TH.rewrite_cd("cd game && rm a.gd"), "a cd before a write is still refused")

    def test_the_hook_refuses_every_call_after_the_deadline(self):
        read = {"tool_name": "Read", "tool_input": {"file_path": "x.gd"}}
        self.assertEqual(TH.decide(read, self.hook_args(time.time() - 1)), TH.DEADLINE_REASON)
        self.assertEqual(TH.decide(read, self.hook_args(time.time() + 600)), "")
        self.assertEqual(TH.decide(read, self.hook_args()), "")
        X.task_settings(None, 1234.0)
        self.assertEqual(json.loads((C.LOGS / "hook-main.json").read_text(encoding="utf-8"))["deadline"], 1234.0)
        self.assertIn("Read", X.HOOKED_TOOLS)


class ReviewPacketTest(DriverTest):
    """The packet a review or audit session is given: what each task cost, why its sessions failed, and the driver's
    own report, which such a session cannot read for itself."""

    def rows(self, task, *sessions):
        """Writes one usage row and one session event per (kind, error, reason) and returns the packet's pieces."""
        usage = self.temp / "usage.jsonl"
        lines, events = [], []
        for kind, error, reason in sessions:
            lines.append(json.dumps({"time": "2026-10-28 10:00:00", "task": task, "kind": kind, "cost_usd": 1.0,
                                     "is_error": error}))
            events.append(json.dumps({"time": "2026-10-28 10:00:00", "t": 1.0, "event": "session", "task": task,
                                      "is_error": error, **reason}))
        usage.write_text("\n".join(lines) + "\nnot json\n", encoding="utf-8")
        (self.temp / "events.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")
        with mock.patch.object(C, "USAGE_LOG", usage):
            return T.task_costs({task})[task], T.task_failures({task})[task]

    def test_a_failed_session_names_its_reason(self):
        costs, why = self.rows("M5-01", ("task", False, {}), ("handoff", True, {"terminal_reason": "max_turns"}),
                               ("handoff", True, {"terminal_reason": "max_turns"}))
        cost, sessions, failed, kinds = costs
        self.assertAlmostEqual(cost, 3.0)
        self.assertEqual((sessions, failed), (3, 2))
        self.assertEqual(", ".join(f"{name} {count}" for name, count in kinds.most_common()), "handoff 2, task 1")
        self.assertEqual(why, ["max_turns", "max_turns"])

    def test_a_failed_session_with_no_reason_reads_as_no_result(self):
        _, why = self.rows("M5-02", ("task", True, {"subtype": "error_during_execution"}), ("task", True, {}))
        self.assertEqual(why, ["error_during_execution", "no result"])

    def test_an_audit_packet_carries_the_driver_report(self):
        """Only a review's or an audit's id gets a packet (runners.do_senior_task), and its packet ends with the
        driver's own report from the range's first day."""
        spec = "**Goal.** Review. The range starts at the commit after `M6-F2`.\n"

        def fake_git(*args, **kwargs):
            return mock.Mock(stdout="2026-10-28\n" if "--format=%cs" in args else "", returncode=0)
        with mock.patch.object(C, "git", fake_git):
            review = T.build("M6-R", spec)
        self.assertIn("| task | cost | sessions | kinds | failed sessions | failed because |", review)
        self.assertIn("## Driver report", review)
        self.assertIn("report.py --since 2026-10-28", review)
        self.assertTrue(T.is_review("M6-R") and T.is_review("M6-A") and T.is_review("M6-V2"))
        self.assertFalse(T.is_review("M6-U01"))

    def test_the_report_text_is_the_lines_the_report_prints(self):
        (self.temp / "events.jsonl").write_text(json.dumps(
            {"time": "2026-10-28 10:00:00", "t": 1.0, "event": "session", "task": "M5-01", "cost_usd": 2.0}) + "\n",
            encoding="utf-8")
        with mock.patch("builtins.print", REAL_PRINT):
            text = RP.report_text("2026-10-28")
            empty = RP.report_text("2099-01-01")
        self.assertIn("== Throughput", text)
        self.assertIn("No events recorded yet", empty)

    def test_the_report_counts_refusals_by_task(self):
        rows = [("M8-C4", ["cd refused"] * 3), ("M8-S5", ["cd refused"] * 2), ("M8-C4", ["other"])]
        lines = [json.dumps({"time": "2026-10-28 10:00:00", "t": float(i), "event": "session", "task": task,
                             "denials": denials}) for i, (task, denials) in enumerate(rows)]
        (self.temp / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        with mock.patch("builtins.print", REAL_PRINT):
            text = RP.report_text("2026-10-28")
        self.assertIn("5 x cd refused", text)
        self.assertIn("3 x M8-C4, 2 x M8-S5", text)


class MonitorTest(DriverTest):
    """The monitor runs every monitor_every landed tasks, over a clean checkout, and tells the phone only of what got
    worse."""

    def watch(self, base, since, clean="3 passed, 0 failed", exit_code=0):
        """One monitor run over a slot at base, with the clean checkout's output and the one monitor command's exit."""
        output = "fast\n" if exit_code == 0 else "slow\n"
        with mock.patch.object(C, "test_output", return_value=clean), \
                mock.patch.object(C.CFG, "monitor_command", ["bench", "{head}"]), \
                mock.patch.object(C, "command_result", return_value=(exit_code, output)) as run, \
                mock.patch.object(C, "git", return_value=mock.Mock(stdout="", returncode=0)):
            record = MO.watch(fake_slot(root=self.temp, base=base), since, since.split(", "))
        self.assertEqual(run.call_args[0][0], ["bench", base[:10]], "{head} is the commit under test")
        return record

    def test_the_monitor_is_due_every_few_landed_tasks(self):
        state = C.RUNTIME["state"]
        with mock.patch.object(C, "MONITOR_EVERY", 2):
            T.task_committed(state, "M3-01")
            self.assertFalse(MO.due(state))
            T.task_committed(state, "M3-02")
            self.assertTrue(MO.due(state))
            self.assertEqual(state["monitor_pending"], ["M3-01", "M3-02"])
        with mock.patch.object(C, "MONITOR_EVERY", 0):
            self.assertFalse(MO.due(state), "0 turns the monitor off")

    def test_a_run_takes_the_landed_tasks_and_frees_its_lock(self):
        state = C.RUNTIME["state"]
        state["monitor_pending"] = ["M3-01", "M3-02"]
        prepared = []

        def prepare(slot, fresh=False):
            prepared.append(fresh)
            slot.base = "abc1234567"
            return slot.base
        with mock.patch.object(SL, "prepare", prepare), mock.patch.object(MO, "watch") as watch:
            MO.start(state)
            MO.THREAD["thread"].join(30)
        self.assertFalse(MO.running())
        self.assertEqual(state["monitor_pending"], [])
        watch.assert_called_once()
        self.assertEqual(watch.call_args[0][1:], ("M3-01, M3-02", ["M3-01", "M3-02"]))
        self.assertEqual(prepared, [True], "the monitor's checkout starts without the cache folders")
        self.assertFalse(MO.LOCK.exists())

    def test_a_held_lock_skips_the_run(self):
        MO.LOCK.write_text(str(os.getpid()), encoding="utf-8")
        with mock.patch.object(C, "pid_alive", return_value=True):
            self.assertFalse(MO.take_lock())
        with mock.patch.object(C, "pid_alive", return_value=False):
            self.assertTrue(MO.take_lock(), "a lock whose process ended is taken over")
        self.assertEqual(C.read_pid(MO.LOCK), os.getpid())

    def test_the_monitor_records_each_run_and_alerts_on_what_got_worse(self):
        first = self.watch("aaaaaaaaaa11", "M3-01, M3-02")
        self.assertEqual((first["clean"], first["commands"]["monitor"]["exit"]), ("pass", 0))
        self.assertEqual(self.posts, [], "nothing got worse")
        self.watch("bbbbbbbbbb22", "M3-03", exit_code=1)
        self.assertEqual(self.posts[-1][0], "Monitor got worse")
        self.assertIn("monitor exits 1: slow", self.posts[-1][1])
        self.assertIn("M3-03", self.posts[-1][1])
        self.watch("cccccccccc33", "M3-04", clean="2 passed, 1 failed", exit_code=1)
        self.assertEqual(len(self.posts), 2)
        self.assertIn("the clean checkout's tests fail: 2 passed, 1 failed", self.posts[-1][1])
        self.assertNotIn("monitor exits", self.posts[-1][1], "a command that failed last time is not news")
        records = MO.read_history()
        self.assertEqual([record["head"] for record in records], ["aaaaaaaaaa", "bbbbbbbbbb", "cccccccccc"])
        self.assertEqual(records[1]["previous"], "aaaaaaaaaa")
        self.assertEqual(MO.record_of("bbbbbbbbbb22")["commands"]["monitor"]["exit"], 1)

    def test_status_says_how_far_the_last_record_is_behind(self):
        self.assertIn("No monitor record yet", MO.status_lines()[0])
        old = {"head": "abc", "time": "2020-01-01 08:00:00", "clean": "pass", "commands": {"monitor": {"exit": 2}}}
        MO.HISTORY.write_text(json.dumps(old) + "\n", encoding="utf-8")
        with mock.patch.object(C, "git", return_value=mock.Mock(returncode=0, stdout="12\n")), \
                mock.patch.object(SL, "main_tip", return_value="def"):
            line = MO.status_lines()[0]
        self.assertIn("`abc` on 2020-01-01 08:00:00, 12 commit(s) behind", line)
        self.assertIn("failing: monitor", line)
        self.assertIn("days old", line)


class IdleKindTest(unittest.TestCase):
    def test_every_wait_reason_maps_to_an_idle_kind(self):
        # why: one example of every reason scheduler.py and mainloop.py can emit; a new reason must get a kind here
        reasons = [
            "no open task is waiting",
            "M9-01 waits for every slot to be idle, then opus runs it alone",
            "M9-01 runs alone",
            "M9-01: outside the window",
            "M9-02: runs alone, so it waits for every slot to be idle",
            "M9-03: waits for M9-01 to land",
            "M9-04: shares world.gd with M9-01",
            "M9-05: a.gd is held by M9-01",
            "nothing in the window can start",
            "paused",
            "over the daily budget",
        ]
        for reason in reasons:
            self.assertNotIn("unknown", RP.idle_kind(reason), reason)


class RestartTest(DriverTest):
    """A fix to the driver's code is used only once the driver restarts, so a landed triage reopens what it looked at
    and the driver restarts itself on the new code."""

    TASKS = "# Task queue\n\n### [!] M9-01 · first\n\n### [!] M9-02 · second\n\n### [!] M9-03 · left alone\n"
    PROGRESS = ("# Progress\n\n## Blocked\n\n- M9-01: changed outside the scope\n- M9-02: changed outside the "
                "scope\n- M9-03: other\n\n## Log\n")

    def root(self):
        (self.temp / "loop").mkdir(exist_ok=True)
        (self.temp / C.TASKS_REL).write_text(self.TASKS, encoding="utf-8")
        (self.temp / C.PROGRESS_REL).write_text(self.PROGRESS, encoding="utf-8")
        return self.temp

    def test_the_driver_sees_a_change_to_its_own_code(self):
        C.RUNTIME["driver_tree"] = "tree-a"
        with mock.patch.object(C, "driver_tree", return_value="tree-a"):
            self.assertFalse(M.code_changed())
        with mock.patch.object(C, "driver_tree", return_value="tree-b"):
            self.assertTrue(M.code_changed())
        with mock.patch.object(C, "driver_tree", return_value=""):
            self.assertFalse(M.code_changed(), "a git error is no change")

    def test_a_restart_for_code_keeps_a_pause(self):
        state = {"paused": True}
        M.restart_for_code(state)
        self.assertTrue(C.read_driver_state()["restart_paused"])
        self.assertIn("Driver restarting", [post[0] for post in self.posts])

    def test_the_supervisor_starts_the_driver_again_at_once_for_new_code(self):
        patches = [mock.patch.object(C, "SUPERVISOR_PID", self.temp / "sup.pid"),
                   mock.patch.object(C, "DRIVER_PID", self.temp / "drv.pid")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        with mock.patch.object(M.subprocess, "call", side_effect=[M.RESTART_FOR_CODE, 0]) as call, \
                mock.patch.object(M.time, "sleep") as sleep:
            self.assertEqual(M.supervise(), 0)
        self.assertEqual(call.call_count, 2)
        sleep.assert_not_called()
        self.assertEqual(call.call_args.kwargs["env"]["WORKER_CONFIG"], str(C.CFG.config_path),
                         "the driver it starts reads the same config")
        self.assertFalse(SL.owner_file().exists(), "the supervisor gives the slots up when it ends")

    def test_a_triage_remembers_the_tasks_it_looks_at(self):
        state = C.RUNTIME["state"]
        state["block_streak"] = [{"task": "M9-01", "reason": "x"}, {"task": "M9-02", "reason": "x"}]
        T.after_blocks(state)
        self.assertEqual(state["triage_tasks"], ["M9-01", "M9-02"])

    def test_reopen_marks_open_the_triaged_tasks_still_blocked(self):
        root = self.root()
        reopened = []
        with mock.patch.object(T, "write_status"):
            T.reopen_marks(["M9-01", "M9-02", "M9-04"], reopened)(root)
        tasks = (root / C.TASKS_REL).read_text(encoding="utf-8")
        progress = (root / C.PROGRESS_REL).read_text(encoding="utf-8")
        self.assertEqual(reopened, ["M9-01", "M9-02"])
        self.assertIn("### [ ] M9-01 ·", tasks)
        self.assertIn("### [ ] M9-02 ·", tasks)
        self.assertIn("### [!] M9-03 ·", tasks, "a task the triage was not about stays blocked")
        self.assertNotIn("- M9-01:", progress)
        self.assertIn("- M9-03: other", progress)

    def test_a_landed_triage_reopens_its_tasks_and_says_the_driver_restarts(self):
        root = self.root()
        state = C.RUNTIME["state"]
        state.update(triage_tasks=["M9-01", "M9-02"], attempts={"M9-01": 2}, requeues={"M9-02": 1})
        instruction = O.prompts.TRIAGE_PROMPT.format(blocked="- M9-01: x\n- M9-02: x")
        answers = []

        def integrate(slot, message, bookkeeping=None, test=True):
            bookkeeping(root)
            return ""
        # why: the driver's code counts as the project's only when its repository is the project
        with mock.patch.object(C, "DRIVER_REPO", C.PROJECT.resolve()), mock.patch.object(C, "DRIVER_REL", "tools/driver"), \
                mock.patch.object(C, "changed_paths", return_value={"tools/driver/slots.py"}), \
                mock.patch.object(SL, "commit_work"), mock.patch.object(SL, "integrate", side_effect=integrate), \
                mock.patch.object(T, "write_status"), \
                mock.patch.object(O, "send_opus_answer", side_effect=lambda *a: answers.append(a)):
            O.land_opus_write(fake_slot(root=root), "opus-write-x", instruction, "fixed", "")
        self.assertTrue(instruction.startswith(O.TRIAGE_START))
        body = answers[0][1]
        self.assertIn("Re-opened, to be tried on the fixed code: M9-01, M9-02", body)
        self.assertIn("restarts on the new code", body)
        self.assertNotIn("triage_tasks", state)
        self.assertNotIn("M9-01", state["attempts"])
        self.assertNotIn("M9-02", state["requeues"])
        self.assertIn("### [ ] M9-02 ·", (root / C.TASKS_REL).read_text(encoding="utf-8"))

    def test_another_opus_write_reopens_nothing(self):
        state = C.RUNTIME["state"]
        state["triage_tasks"] = ["M9-01"]
        seen = []
        with mock.patch.object(C, "changed_paths", return_value={"loop/OWNER.md"}), \
                mock.patch.object(SL, "commit_work"), \
                mock.patch.object(SL, "integrate", side_effect=lambda s, m, b=None, test=True: seen.append(b) or ""), \
                mock.patch.object(O, "send_opus_answer") as answer:
            O.land_opus_write(fake_slot(root=self.temp), "opus-write-y", "Tidy OWNER.md", "done", "")
        self.assertEqual(seen, [None])
        self.assertEqual(state["triage_tasks"], ["M9-01"])
        self.assertNotIn("restarts on the new code", answer.call_args[0][1])


if __name__ == "__main__":
    unittest.main()
