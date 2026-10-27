"""Tests for the build loop's driver. Run from the project root: python -m unittest discover -s loop/driver/tests -v

Nothing here starts a session, calls ntfy or runs git commands that change files: the parts that would are replaced."""
import datetime
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("WORKER_CONFIG", str(pathlib.Path(__file__).resolve().parent / "fixtures" / "worker.json"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import budget as B  # noqa: E402
import commands as K  # noqa: E402
import loop_common as C  # noqa: E402
import mainloop as M  # noqa: E402
import opus as O  # noqa: E402
import phone as P  # noqa: E402
import runners as RN  # noqa: E402
import runtime as R  # noqa: E402
import scheduler as SC  # noqa: E402
import sessions as X  # noqa: E402
import settings as S  # noqa: E402
import slots as SL  # noqa: E402
import tasks as T  # noqa: E402
import workspace as W  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


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
            mock.patch.object(R, "DRIVER_STATE", self.temp / "driver.json"),
            mock.patch.object(R, "DRIVER_LOG", self.temp / "driver.log"),
            mock.patch.object(R, "REQUEST_FILE", self.temp / "driver-request.txt"),
            mock.patch.object(P, "SEEN", self.temp / "seen.txt"),
            mock.patch.object(P, "post", lambda title, body, priority="default", actions="":
                              self.posts.append((title, body, actions))),
            mock.patch("builtins.print"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        R.RUNTIME.clear()
        R.RUNTIME.update(state={})
        R.ctx().clear()

    def phone(self, *texts):
        """handle_commands with these phone messages and no PC requests."""
        messages = [(f"id{index}", text) for index, text in enumerate(texts)]
        with mock.patch.object(K, "poll_messages", return_value=messages):
            K.handle_commands(R.RUNTIME["state"])

    def pc(self, *texts):
        R.REQUEST_FILE.write_text("\n".join(texts) + "\n", encoding="utf-8")
        with mock.patch.object(K, "poll_messages", return_value=[]):
            K.handle_commands(R.RUNTIME["state"])


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
    def test_default_is_sonnet(self):
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


class ApprovalTest(DriverTest):
    def test_which_calls_need_approval(self):
        self.assertTrue(K.needs_approval("fable", "low"))
        self.assertTrue(K.needs_approval("opus", "medium"))
        self.assertTrue(K.needs_approval("opus", None))
        self.assertFalse(K.needs_approval("opus", "low"))
        self.assertFalse(K.needs_approval("sonnet", "max"))
        self.assertFalse(K.needs_approval("haiku", None))

    def test_approve_button_approves(self):
        state = R.RUNTIME["state"]

        def tap(_state):
            for code in state["approvals"]:
                state["approvals"][code]["answer"] = "approve"
        with mock.patch.object(K, "handle_commands", tap):
            self.assertTrue(K.approved(state, "M2-R review"))
        self.assertEqual(state["approvals"], {})
        self.assertIn("APPROVE ", self.posts[0][2])
        self.assertIn("DENY ", self.posts[0][2])

    def test_deny_and_no_answer_are_denials(self):
        state = R.RUNTIME["state"]

        def tap(_state):
            for code in state["approvals"]:
                state["approvals"][code]["answer"] = "deny"
        with mock.patch.object(K, "handle_commands", tap):
            self.assertFalse(K.approved(state, "a"))
        with mock.patch.object(S, "APPROVAL_HOURS", 0), mock.patch.object(K, "handle_commands", lambda s: None):
            self.assertFalse(K.approved(state, "b"))

    def test_approvals_off_asks_nothing(self):
        state = R.RUNTIME["state"]
        with mock.patch.object(S, "APPROVALS_ON", False):
            self.assertTrue(K.approved(state, "M3-F4 Opus"))
        self.assertEqual(self.posts, [])
        self.assertEqual(state.get("approvals", {}), {})

    def test_phone_answer_needs_the_right_code(self):
        state = R.RUNTIME["state"]
        state["approvals"] = {"abc123": {"what": "x", "answer": None}}
        self.phone("APPROVE ffffff")
        self.assertIsNone(state["approvals"]["abc123"]["answer"])
        self.phone("deny abc123")
        self.assertEqual(state["approvals"]["abc123"]["answer"], "deny")

    def test_pc_approve_answers_every_request(self):
        state = R.RUNTIME["state"]
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
        self.assertEqual(K.worker(R.RUNTIME["state"]), ("haiku", "low"))

    def test_pc_commands_are_read_from_the_request_file(self):
        self.pc("MODEL opus high")
        self.assertEqual(K.worker(R.RUNTIME["state"]), ("opus", "high"))
        self.assertFalse(R.REQUEST_FILE.exists())

    def test_pause_and_resume(self):
        self.phone("pause")
        self.assertTrue(R.RUNTIME["state"]["paused"])
        self.phone("RESUME")
        self.assertFalse(R.RUNTIME["state"]["paused"])


class EscalationTest(DriverTest):
    def run_task(self, results, approve=True, escalate="opus", model="sonnet"):
        calls = []

        def attempt(slot, task_id, title, spec, who, level, feedback):
            calls.append((who, feedback))
            return results[len(calls) - 1]
        with mock.patch.object(S, "ESCALATE_MODEL", escalate), mock.patch.object(RN, "worker_attempt", attempt), \
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
        self.assertEqual(calls, [("sonnet", None), ("opus", ("sonnet", "tests fail"))])
        block.assert_not_called()

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
            RN.task_crashed(fake_slot(), "M2-30", "x", ValueError("boom"), S.MAX_TASK_ERRORS, state)
        self.assertTrue(state["paused"])


class FilesTest(DriverTest):
    def test_state_is_written_whole(self):
        R.write_driver_state({"paused": True, "task": "M2-30"})
        self.assertEqual(json.loads(R.DRIVER_STATE.read_text(encoding="utf-8"))["task"], "M2-30")
        self.assertFalse(R.DRIVER_STATE.with_suffix(".tmp").exists())

    def test_log_drops_control_characters_and_starts_a_new_file_each_day(self):
        R.DRIVER_LOG.write_text("old\n", encoding="utf-8")
        yesterday = time.time() - 86400
        os.utime(R.DRIVER_LOG, (yesterday, yesterday))
        R.log("a\x00b\x1bc")
        day = datetime.date.fromtimestamp(yesterday).isoformat()
        self.assertEqual((self.temp / f"driver-{day}.log").read_text(encoding="utf-8"), "old\n")
        self.assertTrue(R.DRIVER_LOG.read_text(encoding="utf-8").endswith(" abc\n"))

    def test_set_heading(self):
        tasks = self.temp / "TASKS.md"
        tasks.write_text("### [ ] M2-30 · Layer toggles\nbody\n### [ ] M2-31 · Radar\n", encoding="utf-8")
        with mock.patch.object(C, "TASKS", tasks):
            T.set_heading("M2-31", "!")
            self.assertEqual(T.next_task(tasks.read_text(encoding="utf-8")), ("M2-30", "Layer toggles"))
            T.set_heading("M2-30", "x")
            self.assertIsNone(T.next_task(tasks.read_text(encoding="utf-8")))

    def test_spend_on_adds_up_one_day(self):
        usage = self.temp / "usage.jsonl"
        usage.write_text(json.dumps({"time": "2026-10-26 10:00:00", "cost_usd": 1.5}) + "\n"
                         + json.dumps({"time": "2026-10-26 11:00:00", "cost_usd": None}) + "\n"
                         + json.dumps({"time": "2026-10-25 11:00:00", "cost_usd": 9}) + "\nnot json\n",
                         encoding="utf-8")
        with mock.patch.object(C, "USAGE_LOG", usage):
            self.assertAlmostEqual(C.spend_on("2026-10-26"), 1.5)


class ParsingTest(DriverTest):
    def test_done_when_wraps_to_the_next_blank_line(self):
        spec = "**Done when.** test `test_a` and file `loop/reviews/M2-review.md`\nand output `bot run: PASS`\n\nmore"
        self.assertEqual(W.done_when(spec), [("test", "test_a"), ("file", "loop/reviews/M2-review.md"),
                                             ("output", "bot run: PASS")])

    def test_reset_time_is_within_a_day(self):
        reset = B.reset_time("You've hit your session limit · resets 1:20pm (America/Los_Angeles)")
        self.assertTrue(time.time() < reset <= time.time() + 86400)
        self.assertIsNone(B.reset_time("some other error"))

    def test_settings_take_old_names_and_refuse_a_bad_model(self):
        values = S.read_settings("```driver-settings\nhaiku_turns: 55\nescalate_model: none\n```")
        self.assertEqual(values["worker_turns"], "55")
        S.apply_settings(values)
        self.assertEqual(S.ESCALATE_MODEL, "")
        values["escalate_model"] = "gpt"
        with self.assertRaises(ValueError):
            S.apply_settings(values)
        S.apply_settings(dict(S.SETTING_DEFAULTS))


class WorkspaceTest(DriverTest):
    BEFORE = "func test_a():\n\tassert_eq(1, 1)\n\tassert_true(true)\n\n\nfunc test_b():\n\tassert_true(true)\n"

    def test_assert_counts_per_test_function(self):
        text = self.BEFORE + "\n\nfunc _helper():\n\tassert_true(true)\n"
        self.assertEqual(W.assert_counts(text), {"test_a": 2, "test_b": 1})

    def test_fewer_asserts_in_a_test_the_task_does_not_name_is_weakened(self):
        now = self.BEFORE.replace("\tassert_true(true)\n\n\nfunc test_b", "\tpass\n\n\nfunc test_b")
        found = W.weakened(self.BEFORE, now, "spec", "game/tests/test_x.gd")
        self.assertEqual(found, ["test_x.test_a (2 to 1 asserts)"])
        self.assertEqual(W.weakened(self.BEFORE, now, "rewrite test_a's assert", "game/tests/test_x.gd"), [])
        self.assertEqual(W.weakened(self.BEFORE, now, "in test_x, ...", "game/tests/test_x.gd"), [])

    def test_allowed_loop_files_come_from_the_files_and_done_when_lines(self):
        spec = ("**Files.** Change `loop/PLAYTEST.md` and `game/src/a.gd`.\n\n"
                "**Done when.** file `loop/reviews/M3-review.md`\n")
        self.assertEqual(W.allowed_loop_files(spec, False), {"loop/PLAYTEST.md", "loop/reviews/M3-review.md"})
        self.assertIn("loop/TASKS.md", W.allowed_loop_files(spec, True))

    def test_loop_changes_flag_the_driver_but_not_named_or_earlier_files(self):
        snap = {"head": "abc", "dirty": {"loop/LOOP.md"}}
        changed = {"loop/LOOP.md", "loop/driver.py", "loop/reviews/x.md", "game/src/a.gd"}
        with mock.patch.object(C, "changed_paths", return_value=changed):
            self.assertEqual(W.loop_changes(snap, {"loop/reviews/"}), ["loop/driver.py"])
            self.assertEqual(W.outside_scope(snap, ["game/tests"]), ["game/src/a.gd"])

    def test_a_commit_during_a_task_pauses_and_leaves_it_open(self):
        state = R.RUNTIME["state"]
        slot = fake_slot(base="old")
        with mock.patch.object(C, "head", return_value="new"):
            self.assertTrue(RN.commit_made_during(slot, "M2-08"))
        self.assertTrue(state["paused"])
        with mock.patch.object(C, "head", return_value="old"):
            self.assertFalse(RN.commit_made_during(slot, "M2-08"))

    def test_the_busy_slots_survive_in_driver_json(self):
        state = {}
        with mock.patch.object(SL, "LEASES", self.temp / "leases.json"):
            scheduler = SC.Scheduler(state)
            scheduler.slots["1"].task = "M3-07"
            scheduler.slots["1"].base = "abc"
            scheduler.save()
        saved = R.read_driver_state()
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
        with mock.patch.object(C, "spend_on", return_value=110.0), mock.patch.object(S, "DAILY_BUDGET_USD", 120.0):
            self.assertEqual(B.session_budget(30.0), 10.0)
            self.assertTrue(R.ctx()["budget_cut"])
            self.assertTrue(B.cut_by_daily_budget("error_max_budget_usd"))
            B.release_reservation()
        with mock.patch.object(C, "spend_on", return_value=10.0), mock.patch.object(S, "DAILY_BUDGET_USD", 120.0):
            self.assertEqual(B.session_budget(30.0), 30.0)
            self.assertFalse(B.cut_by_daily_budget("error_max_budget_usd"))
            B.release_reservation()

    def test_blocks_in_a_row_queue_a_triage_then_pause(self):
        state = R.RUNTIME["state"]
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
        R.write_driver_state({"ended_by": "owner"})
        with mock.patch.object(M, "supervise") as supervise:
            self.assertEqual(M.autostart(), 0)
            supervise.assert_not_called()
            R.write_driver_state({"ended_by": None})
            M.autostart()
            supervise.assert_called_once()

    def test_runners_pick_the_opus_task_by_its_who_line(self):
        pick = lambda spec: next(run for kind, run in RN.RUNNERS if kind(spec))  # noqa: E731
        self.assertIs(pick("### [ ] M3-F4 · x\n**Who.** Opus\n"), RN.do_opus_task)
        self.assertIs(pick("### [ ] M2-08 · x\n"), RN.do_worker_task)

    def test_an_opus_task_asks_for_its_effort_in_an_effort_line(self):
        self.assertEqual(RN.opus_effort("**Who.** Opus\n**Effort.** medium\n"), "medium")
        self.assertEqual(RN.opus_effort("**Who.** Opus\n"), S.OPUS_EFFORT)
        self.assertEqual(RN.opus_effort("**Who.** Opus\n**Effort.** loud\n"), S.OPUS_EFFORT)


class LoopProblemTest(DriverTest):
    def test_a_session_that_never_started_pauses_instead_of_blocking(self):
        R.ctx()["last_session"] = {"turns": 0, "seconds": 1}
        self.assertTrue(B.never_started("M2-20", "Your organization has disabled Claude subscription access"))
        self.assertTrue(R.RUNTIME["state"]["paused"])
        self.assertGreater(R.RUNTIME["state"]["resume_at"], time.time())
        R.ctx()["last_session"] = {"turns": 12, "seconds": 1}
        self.assertFalse(B.never_started("M2-20", "x"))
        R.ctx()["last_session"] = {"turns": 0, "seconds": 549}
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
        sys.path.insert(0, str(X.TASK_HOOK.parent))
        import task_hook
        for command in ("cd game/src", "ls && cd game; python x", "(cd game && ls)", "pushd loop"):
            self.assertTrue(task_hook.changes_folder(command), command)
        for command in ('grep -rn "cd " game', "python game/tools/run_tests.py", "echo abcd"):
            self.assertFalse(task_hook.changes_folder(command), command)

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


class MonitorTest(DriverTest):
    def test_the_monitor_runs_every_few_landings_and_alerts_on_a_failure(self):
        import monitor
        state = R.RUNTIME["state"]
        patch = mock.patch.object(monitor, "HISTORY", self.temp / "monitor-history.jsonl")
        patch.start()
        self.addCleanup(patch.stop)

        def prepare(slot, fresh=False):
            slot.base = "abc1234567"
            return slot.base
        with mock.patch.object(S, "MONITOR_EVERY", 2), mock.patch.object(monitor.SL, "prepare", prepare), \
                mock.patch.object(C, "test_output", return_value="3 passed, 0 failed"), \
                mock.patch.object(C.CFG, "monitor_command", ["bench"]), \
                mock.patch.object(monitor.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="fast\n",
                                                                                     stderr="")):
            T.task_committed(state, "M3-01")
            self.assertFalse(monitor.due(state))
            T.task_committed(state, "M3-02")
            self.assertTrue(monitor.due(state))
            monitor.run(state)
            self.assertEqual(state["monitor_pending"], [])
            self.assertEqual(self.posts, [])
            T.task_committed(state, "M3-03")
            with mock.patch.object(C, "test_output", return_value="2 passed, 1 failed"):
                monitor.run(state)
        self.assertEqual(self.posts[-1][0], "Clean checkout fails")
        self.assertIn("M3-03", self.posts[-1][1])
        records = [json.loads(line) for line in monitor.HISTORY.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["clean"] for record in records], ["pass", "2 passed, 1 failed"])
        self.assertEqual(records[0]["monitor_exit"], 0)


if __name__ == "__main__":
    unittest.main()
