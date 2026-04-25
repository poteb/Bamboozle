---
name: add-docket
description: Use when the user asks to create, add, queue, file, or docket a new task — phrases like "make a task for X", "add a task to do Y", "add to the docket", "docket this", or "new task". Applies in any project that follows the Docket convention (a `Tasks/` folder with one markdown file per task).
---

# add-docket

Create a new Docket task file in `Tasks/` with a UTC timestamp prefix, after verifying the description is clear enough to act on later.

## When to use

- User asks to create / add / queue / file a new task.
- User says "make a task to X", "add a task for Y", "turn this into a task".
- User wants to turn a verbal request into a persistent task file.

Do NOT use for: editing existing tasks, marking tasks done, listing tasks. Those don't need this skill.

## Workflow

1. **Ensure `Tasks/` exists.** From the repo root, check for `Tasks/`. If missing, create `Tasks/` and `Tasks/done/` (drop a `.gitkeep` in `done/` so git tracks it).

2. **Generate a UTC timestamp** with Bash:

   ```bash
   date -u +%Y%m%d-%H%M%S
   ```

   Example output: `20260419-145321`. This is the filename prefix — no scan of existing files needed, timestamps are collision-free by construction.

3. **Clarity check.** Before writing the file, evaluate the description against these criteria:
   - **Goal**: Is it clear what "done" looks like?
   - **Scope**: Is the boundary of the work defined (what's in, what's out)?
   - **Inputs**: Does the task reference files, data, or decisions the user hasn't provided?
   - **Design choices**: Are there forks-in-the-road you'd have to guess at?

   If any of these is ambiguous, **ask the user before writing the file**. Use AskUserQuestion when there are 2+ discrete choices; use a direct chat question for a single open-ended gap. Do not invent answers — a vague task file is worse than no task file, because future-you will act on it.

4. **Derive a slug.** Short kebab-case, 2–5 words, lowercase, hyphen-separated. Strip articles ("the", "a", "an") and filler ("please", "can you"). Example: "Add thumbnails to the sidebar" → `add-thumbnails-sidebar`.

5. **Write the file** at `Tasks/<timestamp>-<slug>.md`:

````markdown
# <Title Case of slug>

## Status
todo  <!-- todo | in progress | blocked | done -->

## Task
<Cleaned-up description: 1–3 sentences or a short bullet list. Keep the user's intent; trim filler.>

## Notes

## Questions
````

   If the exact filename already exists (same second + same slug — extremely rare), bump the timestamp by 1 second and retry.

6. **Report** the created file path back to the user in one line, e.g. `Created Tasks/20260419-145321-add-thumbnails-sidebar.md.`

## Clarity check — examples

**Clear enough, write it:**
> "Add a dark-mode toggle to the header that persists to localStorage."

Goal, scope, and behavior are all specified.

**Vague, ask first:**
> "Make the scripts faster."

Which scripts? By how much? What's acceptable to change? Ask.

> "Add thumbnails."

Where? Sourced from what? One size or responsive? Ask.

> "Clean up the UI."

Unbounded. Push the user for a specific, acceptance-testable slice.

## Filename rules

- Timestamp is always UTC, format `YYYYMMDD-HHMMSS` — ASCII-sortable and collision-free in practice.
- Always run `date -u +%Y%m%d-%H%M%S` live. Don't hard-code, don't reuse a previous timestamp, don't estimate.
- The slug is the *verbal handle* — short, memorable, lowercase kebab-case. Long slugs (>5 words) should be trimmed.
- Never retroactively rename a task file; the creation timestamp is immutable.

## Common mistakes

| Mistake | Fix |
|---|---|
| Writing the file before clarifying vague asks | Ask first. Better to stall 30 seconds than create a misleading task. |
| Hard-coding or estimating the timestamp | Always run `date -u +%Y%m%d-%H%M%S` live. |
| Using local time | Always UTC (`-u`). Mixed timezones break sort order across contributors. |
| Long slugs (>5 words) | Trim. The timestamp is the identity; the slug is just a hint. |
| Status `in progress` on a new task | Always start as `todo`. |
| Echoing the user's raw words into `## Task` verbatim | Clean up: trim filler, tighten to 1–3 sentences. Keep the intent. |
| Creating the task file when you really should have just answered the question | If the user is asking something, not queuing work, don't write a file. |
