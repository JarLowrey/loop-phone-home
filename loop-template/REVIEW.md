# Milestone review

Every milestone review task (`M<n>-R`) follows this file. Rules a test can check by reading the source belong in a test
that runs on every task, so a review spends its time on what needs judgement.

## Before the questions

1. Find the milestone's first commit (the review task names it) and read `git diff <commit>^ HEAD --stat`, then every
   changed file in full, with the project rules and the milestone's part of the design.
2. Run the tests (the `test_command` in `worker.json`), and any longer check the project has (a bot, a benchmark, a
   smoke run). Keep their outputs.
3. Read the next milestone's tasks in the task file, and this milestone's blocked tasks and the Blocked lines in the
   progress file.

## The questions

Answer each one with evidence: a file and line, a test name, a number from a run, or a commit. "Nothing found" is an
answer when the evidence says so.

1. **Does it work as the design says?** Take the milestone's goal and the numbers this milestone added. Which ones does
   a test or a run show working, and which only exist in code? What do the longer checks say, against the last review's
   numbers?
2. **What will the next milestone fight with?** For each of its tasks, the code it will touch. Which of it would it
   have to restructure rather than extend, and why?
3. **Where is one fact written twice?** The same rule, number or list in two places: two functions, code and data, a
   test and the code it tests, a constant and the design. Which copy is the one to keep?
4. **What is hardest to change now?** The file or function a new person would be most afraid to edit: its size, how
   many places call it, how much state it reaches. What would make it smaller?
5. **Which tests would miss a real bug?** For the milestone's main rules, pick a line and ask whether any test fails if
   it changes (a sign flipped, an off-by-one, a step moved). Tests that only check that nothing crashed, or that restate
   the code, count as missing.
6. **What did the workers get wrong more than once?** From the blocked tasks, the reverts and the commits that fixed an
   earlier commit: the pattern, and the line in the project rules or a task's wording that would have prevented it.
7. **What would confuse a user?** A value the user sees with no source in the design, a screen or output that shows
   state nothing sets, an action with no feedback, an open question this milestone left unanswered.
8. **What is slower than it should be?** The test run's time, the longer checks' time, and any loop that does work for
   every pair of things when it could do less.

## What happens to each finding

- **Fix it now** when the fix stays inside the scope and the tests pass after it. Put simplification and less
  duplication first, then bugs, then design gaps.
- **Otherwise write a task** in the task file, at the start of the next milestone, in the task file's format, with a
  `**Done when.**` line. Its id is the next milestone's prefix and `F`, then a number (`M3-F1`).
- **A worker mistake (question 6)** goes under "Suggested rules" in the review, as the exact line to add to the project
  rules. The owner adds it; the review does not edit the rules file.
- Never delete, skip or loosen a test to make the suite pass. A test may move or merge with another only when it keeps
  every assert.

## The review file

`<logs or reviews folder>/<milestone>-review.md`, in this order:
1. A grade from A to D for each question, with one line saying why.
2. The test run and longer check numbers (time, totals, results).
3. What was fixed, file by file, and why.
4. The tasks written, by id, with the question that found each.
5. Suggested rules for the project rules file.
