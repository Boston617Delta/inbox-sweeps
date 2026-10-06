# inbox-sweeps

Scheduled newsletter sweeps for [Claude Code](https://claude.com/claude-code). You tell it which emails to watch and how often. On each run, Claude reads the new emails, turns the ones worth keeping into Markdown notes sorted by topic, updates an index note, and emails you a summary of what it filed.

The notes are written for Obsidian (`[[wikilinks]]`, YAML frontmatter), but any folder of Markdown works.

## What a run does

1. Searches Gmail for your query, limited to the window since the last run.
2. Skips promos, receipts, and anything already in the library.
3. Writes one note per useful email into the best-fit topic folder: a one-line takeaway, then a faithful 100 to 250 word distillation that keeps the numbers and exact wording.
4. Adds a line for each new note to the library's index.
5. Emails you a summary (optional).

A run that fails (API error, timeout, rate limit) retries once after 60 seconds. If it still fails, you get a macOS notification instead of a silent miss.

## Requirements

- macOS (scheduling uses launchd)
- Python 3.11 or newer (no packages to install)
- Claude Code (`claude` on your PATH)
- A Gmail MCP server that Claude Code can reach from `claude_workdir`. The defaults assume the Google Workspace `gws` server. If yours has different tool names, change the `gmail_*_tool` settings.

## Setup

```sh
git clone <this repo> && cd inbox-sweeps
./sweep.py add            # answers a few questions, writes sweeps.toml
./sweep.py prompt <name>  # preview exactly what Claude will be told
./sweep.py run <name>     # run it once now, check the notes and summary
./sweep.py install <name> # put it on the schedule
```

You can also copy `sweeps.example.toml` to `sweeps.toml` and edit it by hand. `sweeps.toml` is gitignored, so your addresses, folders, and sources never get committed.

## Defining a sweep

```toml
[sweep.lennys-newsletter]
display_name = "Lenny's Newsletter"
query = "from:lenny@substack.com"           # any Gmail search
folder = "Research Library/Lenny"           # relative to notes_root, or absolute
topics = ["Growth", "Product", "Pricing"]   # becomes the topic subfolders
schedule = "sun 9am"                        # see Schedules below
```

Optional keys:

| Key | Purpose |
|---|---|
| `keep` | What to preserve word for word (scripts, benchmarks, settings paths) |
| `skip` | What counts as noise for this sender |
| `source`, `tags` | Frontmatter values |
| `title_strip` | Subject prefix to remove from note titles, e.g. `"Mozi Minute:"` |
| `index_file` | Index note name, defaults to `<display_name> MOC.md` |
| `extra_sections` | Extra Markdown appended to the note template |
| `summary_extra` | An extra instruction for the summary email, e.g. flag items for a follow-up |
| `lookback_days` | Override the search window (default: longest gap between run days plus one day) |
| `enabled` | `false` keeps it in config but out of `install` |

The folder, topic subfolders, and index are created on the first run if they don't exist.

## Schedules

A schedule is the days, a space, then one or more times. Times use the Mac's local clock and can be written as `2pm`, `2:30pm`, `14:00`, or `noon`.

| You want | Write |
|---|---|
| Every day at 2pm | `daily 2pm` |
| Mondays at 8:30am | `mon 8:30am` |
| Mondays and Thursdays at 5:15pm | `mon,thu 5:15pm` |
| Weekdays, morning and end of day | `weekdays 7am,5pm` |
| Saturday and Sunday at noon | `weekends noon` |

A sweep usually finishes within a few minutes. If you want the notes ready before a meeting, schedule it 15 to 30 minutes ahead.

## Commands

| Command | |
|---|---|
| `./sweep.py list` | Configured sweeps, schedules, and whether each is scheduled |
| `./sweep.py add` | Interactive setup for a new sweep |
| `./sweep.py prompt <name>` | Print the generated prompt without running anything |
| `./sweep.py run <name>` | Run now; output goes to `logs/<name>.log` |
| `./sweep.py install [name ...]` | Schedule with launchd (all enabled sweeps if no name is given) |
| `./sweep.py uninstall [name ...]` | Remove from the schedule |

After changing a sweep's schedule, run `install` again to pick up the change. Prompt changes (query, topics, keep, skip) apply on the next run without reinstalling.

## Notes

- Runs use `claude -p --dangerously-skip-permissions` with an explicit `--allowedTools` list: the three Gmail tools plus Read, Write, Edit, and Bash. Review the prompt with `./sweep.py prompt` before you schedule anything.
- The Mac has to be awake at the scheduled time. launchd runs a missed job when the machine wakes, but not one that was missed while it was shut down.
- Existing notes are never modified. To rebuild one, delete it and the next run refiles it if the email is still inside the window.
