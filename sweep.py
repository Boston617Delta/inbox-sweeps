#!/usr/bin/env python3
"""inbox-sweeps: config-driven newsletter sweeps for Claude Code.

Each sweep reads matching emails from Gmail, distills the useful ones into
Markdown notes (Obsidian-friendly), updates an index note, and emails you a
summary. Sweeps are defined in sweeps.toml and scheduled with macOS launchd.

Usage:
  sweep.py list                     show configured sweeps and schedules
  sweep.py add                      interactive wizard, appends a sweep to sweeps.toml
  sweep.py prompt <name>            print the prompt a sweep would send (dry run)
  sweep.py run <name>               run a sweep now
  sweep.py install [name ...]       schedule sweeps with launchd (all enabled if none given)
  sweep.py uninstall [name ...]     remove scheduled sweeps (all if none given)

Config path: $SWEEPS_CONFIG, else sweeps.toml next to this script.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import date, datetime
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("SWEEPS_CONFIG", HERE / "sweeps.toml"))
LOG_DIR = HERE / "logs"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"

DAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}

DEFAULT_SETTINGS = {
    "notes_root": "~/Documents/Obsidian Vault",
    "email_to": "",
    "claude_workdir": "~",
    "label_prefix": "com.inbox-sweeps",
    "notify": True,
    "gmail_list_tool": "mcp__gws__gmail_users_messages_list",
    "gmail_get_tool": "mcp__gws__gmail_users_messages_get",
    "gmail_send_tool": "mcp__gws__gmail_users_messages_send",
    "gmail_trash_tool": "mcp__gws__gmail_users_messages_trash",
    "style_rules": "",
    "extra_allowed_tools": [],
}

# `claude -p` can exit 0 even when the run died mid-flight, so the output is
# also checked for these signatures before a run counts as successful.
ERR_RE = re.compile(
    r"API Error|Connection closed|Request timed out|rate limit|"
    r"exceeds maximum allowed tokens|Execution error|Credit balance",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- config

def load_config():
    if not CONFIG_PATH.exists():
        sys.exit(f"No config at {CONFIG_PATH}. Copy sweeps.example.toml to sweeps.toml, "
                 f"or run: sweep.py add")
    with open(CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f)
    settings = {**DEFAULT_SETTINGS, **cfg.get("settings", {})}
    sweeps = cfg.get("sweep", {})
    for name, s in sweeps.items():
        missing = [k for k in ("query", "folder", "topics", "schedule") if k not in s]
        if missing:
            sys.exit(f"Sweep '{name}' is missing: {', '.join(missing)}")
        s.setdefault("display_name", name.replace("-", " ").title())
        s.setdefault("enabled", True)
        parse_schedule(s["schedule"], name)  # validate early
    return settings, sweeps


def expand(path, root=None):
    p = Path(os.path.expanduser(path))
    if not p.is_absolute() and root:
        p = Path(os.path.expanduser(root)) / p
    return p


def get_sweep(sweeps, name):
    if name not in sweeps:
        sys.exit(f"Unknown sweep '{name}'. Configured: {', '.join(sweeps) or 'none'}")
    return sweeps[name]


# -------------------------------------------------------------- schedule

SCHEDULE_HELP = "Use e.g. 'daily 2pm', 'mon 8:30am', 'mon,thu 14:00' or 'weekdays 7am,5pm'."
DAY_GROUPS = {"weekdays": [1, 2, 3, 4, 5], "weekends": [0, 6]}


def parse_time(text):
    """'14:00', '2pm', '2:30pm', '2:30 pm', 'noon' -> (hour, minute), or None if invalid."""
    t = text.strip().replace(" ", "")
    if t == "noon":
        return 12, 0
    if t == "midnight":
        return 0, 0
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", t)
    if not m or (m.group(2) is None and m.group(3) is None):
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def parse_schedule(text, name="?"):
    """'daily 2pm', 'mon,thu 09:00', 'weekdays 7am,5pm' -> (weekdays or None for daily, [(hour, minute), ...])."""
    parts = text.lower().strip().split(None, 1)
    if len(parts) != 2:
        sys.exit(f"Sweep '{name}': bad schedule '{text}'. {SCHEDULE_HELP}")
    days_part, times_part = parts
    times = []
    for t in times_part.split(","):
        parsed = parse_time(t)
        if not parsed:
            sys.exit(f"Sweep '{name}': can't read the time '{t.strip()}' in '{text}'. {SCHEDULE_HELP}")
        times.append(parsed)
    times = sorted(set(times))
    if days_part in ("daily", "everyday"):
        return None, times
    days = []
    for d in days_part.split(","):
        if d in DAY_GROUPS:
            days += DAY_GROUPS[d]
        elif d[:3] in DAYS:
            days.append(DAYS[d[:3]])
        else:
            sys.exit(f"Sweep '{name}': unknown day '{d}' in schedule '{text}'. {SCHEDULE_HELP}")
    days = sorted(set(days))
    return (None if len(days) == 7 else days), times


def default_lookback(schedule):
    """Days of mail to search: the longest gap between runs, plus one day of overlap."""
    days, _ = parse_schedule(schedule)
    if days is None:
        return 2
    if len(days) == 1:
        return 8
    gaps = [(days[(i + 1) % len(days)] - d) % 7 or 7 for i, d in enumerate(days)]
    return max(gaps) + 1


# ---------------------------------------------------------------- prompt

def build_prompt(settings, s):
    folder = expand(s["folder"], settings["notes_root"])
    display = s["display_name"]
    index_file = s.get("index_file", f"{display} MOC.md")
    lookback = s.get("lookback_days", default_lookback(s["schedule"]))
    query = f'{s["query"]} newer_than:{lookback}d'
    topics = ", ".join(s["topics"])
    tags = ", ".join(s.get("tags", [re.sub(r"[^a-z0-9]+", "-", display.lower()).strip("-")]))
    source = s.get("source", f"{display} newsletter")
    keep_items = s.get("keep", "the specific steps, numbers, named frameworks, and word-for-word scripts or templates")
    skip_items = s.get("skip", "pure promos: course or product launches, event and webinar invites with no lesson in the body, order or account notices")
    run_date = date.today().isoformat()

    title_rule = "Derive a clean note title from the subject (title case"
    if s.get("title_strip"):
        title_rule += f', drop the "{s["title_strip"]}" prefix'
    title_rule += ', never use ":" or "#" or "/" in filenames).'

    fetch_rule = ""
    if s.get("fetch_articles"):
        fetch_rule = (
            "- If the email was not skipped, get the full story before writing, because the email only carries teasers: find the link to the "
            "lead article in the email body (tracking links are fine, WebFetch follows redirects) and fetch it "
            "with WebFetch. Write \"The tip\" from the full article, and add a \"url: <article URL>\" line to the "
            "frontmatter. If the fetch fails or is paywalled, fall back to the email text and say so in one line "
            "at the end of the note. Fetch only the lead article, never the \"Also in this email\" items.\n"
        )

    extra_sections = s.get("extra_sections", "").strip()
    extra_sections = f"\n\n{extra_sections}" if extra_sections else ""

    prompt = f"""You maintain the {display} library of Markdown notes at:
{folder}
It has topic subfolders ({topics}) and an index at "{index_file}".

Step 0: If the folder, any topic subfolder, or the index file does not exist yet, create it. Give a new index file a "# {display}" heading and one "## <topic>" section per topic.

Step 1: Using {settings["gmail_list_tool"]} (userId "me"), search q: "{query}". For each result, fetch it with {settings["gmail_get_tool"]} (format FULL) and decode the body.

Step 2: FIRST use Bash to run: find "{folder}" -name "*.md" and treat that list as the existing notes. Do not use Glob for this step; an empty list here means the tool failed, not that the library is empty. Then for each email that has real substance (skip {skip_items}):
- {title_rule}
- Compare the title against the existing note list CASE-INSENSITIVELY and ignoring punctuation differences. If any existing note matches, skip this email.
{fetch_rule}- Then pick the best-fitting topic folder from the list above and Write the note there using exactly this template:

---
source: {source}
email-subject: "<original subject>"
date: <YYYY-MM-DD of the email>
topic: <topic folder name>
tags: [{tags}]
---

# <Clean Title>

**Takeaway:** <one sentence>

## The tip
<Faithful distillation, 100-250 words. Keep {keep_items} verbatim. Bullets or numbered steps where the email uses them. If the email is a digest with several distinct items, cover the main one in depth and list the others as one-line bullets under "## Also in this email". No promo content.>{extra_sections}

- Then Read "{index_file}", and use Edit to add one line for the new note at the END of the matching topic section, format: - [[<Clean Title>]]: <very short hook>
"""

    if s.get("trash_processed"):
        prompt += f"""
Step 2b: Once every note and index line is written, move every email this run handled to Trash with {settings["gmail_trash_tool"]} (userId "me", id <message id>): the ones you filed, the ones skipped as already filed, and the ones skipped as promos. Skip any message that already has the TRASH label. Never trash an email whose note failed to write, and never trash anything outside this run's search results.
"""

    if settings["email_to"]:
        trash_note = " List the emails moved to Trash as a short footnote." if s.get("trash_processed") else ""
        summary_extra = s.get("summary_extra", "").strip()
        summary_extra = f" {summary_extra}" if summary_extra else ""
        prompt += f"""
Step 3: Send a summary email with {settings["gmail_send_tool"]} (userId "me") to {settings["email_to"]}, subject "{display} sweep {run_date}: <N> new notes filed". Body: one line per new note (title, topic, takeaway), or "No new {display} emails this run" if nothing was filed. Include already-filed skips as a short footnote.{trash_note}{summary_extra} Build a plain-text RFC 2822 message and base64url-encode it for the "raw" field.
"""

    rules = ["Do not modify existing notes.", "Do not print note contents to stdout."]
    if settings["style_rules"].strip():
        rules.insert(0, settings["style_rules"].strip())
    prompt += "\nSTRICT RULES: " + " ".join(rules) + "\n"
    return prompt


def allowed_tools(settings, s):
    tools = [settings["gmail_list_tool"], settings["gmail_get_tool"]]
    if settings["email_to"]:
        tools.append(settings["gmail_send_tool"])
    tools += ["Read", "Write", "Edit", "Bash"] + list(settings["extra_allowed_tools"])
    if s.get("fetch_articles"):
        tools.append("WebFetch")
    if s.get("trash_processed"):
        tools.append(settings["gmail_trash_tool"])
    return ",".join(tools)


# ------------------------------------------------------------------- run

def notify(settings, title, message, failed=False):
    if not settings["notify"] or sys.platform != "darwin":
        return
    sound = ' sound name "Basso"' if failed else ""
    script = f'display notification "{message}" with title "{title}"{sound}'
    subprocess.run(["osascript", "-e", script], capture_output=True)


def cmd_run(name):
    settings, sweeps = load_config()
    s = get_sweep(sweeps, name)
    claude = shutil.which("claude")
    if not claude:
        sys.exit("`claude` not found on PATH.")
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"{name}.log"
    prompt = build_prompt(settings, s)
    workdir = expand(settings["claude_workdir"])
    display = s["display_name"]

    def log(line):
        with open(log_path, "a") as f:
            f.write(line + "\n")

    def attempt():
        with tempfile.TemporaryFile("w+") as out:
            proc = subprocess.run(
                [claude, "-p", prompt, "--allowedTools", allowed_tools(settings, s),
                 "--dangerously-skip-permissions"],
                cwd=workdir, stdout=out, stderr=subprocess.STDOUT, text=True,
            )
            out.seek(0)
            text = out.read()
        log(text)
        return proc.returncode != 0 or bool(ERR_RE.search(text)), proc.returncode

    log(f"=== {datetime.now():%Y-%m-%d %H:%M:%S} :: running {display} sweep ===")
    failed, rc = attempt()
    if failed:
        log(f"=== attempt 1 failed (exit {rc}), retrying in 60s ===")
        time.sleep(60)
        failed, rc = attempt()

    status = "FAILED" if failed else "OK"
    log(f"=== {datetime.now():%Y-%m-%d %H:%M:%S} :: exit {rc} :: {status} ===")
    if failed:
        notify(settings, f"{display} Sweep", f"{display} sweep FAILED. Check logs/{name}.log", failed=True)
    else:
        notify(settings, f"{display} Sweep", f"{display} sweep done")
    print(f"{display} sweep {status}. Log: {log_path}")
    sys.exit(1 if failed else 0)


# --------------------------------------------------------------- launchd

def label_for(settings, name):
    return f'{settings["label_prefix"]}.{name}'


def plist_for(settings, name, s):
    days, times = parse_schedule(s["schedule"], name)
    if days is None:
        interval = [{"Hour": h, "Minute": m} for h, m in times]
    else:
        interval = [{"Weekday": d, "Hour": h, "Minute": m} for d in days for h, m in times]
    claude = shutil.which("claude")
    path_dirs = [str(Path(claude).parent)] if claude else []
    path_dirs += [str(Path(sys.executable).parent), "/opt/homebrew/bin", "/usr/local/bin",
                  "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    LOG_DIR.mkdir(exist_ok=True)
    launchd_log = str(LOG_DIR / f"{name}.launchd.log")
    return {
        "Label": label_for(settings, name),
        "ProgramArguments": [sys.executable, str(HERE / "sweep.py"), "run", name],
        "EnvironmentVariables": {
            "PATH": ":".join(dict.fromkeys(path_dirs)),
            "SWEEPS_CONFIG": str(CONFIG_PATH),
        },
        "StartCalendarInterval": interval,
        "StandardOutPath": launchd_log,
        "StandardErrorPath": launchd_log,
    }


def to_plist(obj, indent=1):
    """Minimal plist XML writer (avoids plistlib, whose expat dependency is broken on some Homebrew Pythons)."""
    pad = "    " * indent
    if isinstance(obj, bool):
        return f"{pad}<{'true' if obj else 'false'}/>"
    if isinstance(obj, int):
        return f"{pad}<integer>{obj}</integer>"
    if isinstance(obj, str):
        return f"{pad}<string>{escape(obj, quote=False)}</string>"
    if isinstance(obj, list):
        inner = "\n".join(to_plist(v, indent + 1) for v in obj)
        return f"{pad}<array>\n{inner}\n{pad}</array>"
    inner = "\n".join(f"{pad}    <key>{escape(k, quote=False)}</key>\n{to_plist(v, indent + 1)}" for k, v in obj.items())
    return f"{pad}<dict>\n{inner}\n{pad}</dict>"


def plist_xml(obj):
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n' + to_plist(obj, 0) + "\n</plist>\n")


def launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def cmd_install(names):
    settings, sweeps = load_config()
    targets = names or [n for n, s in sweeps.items() if s["enabled"]]
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    for name in targets:
        s = get_sweep(sweeps, name)
        label = label_for(settings, name)
        path = LAUNCH_AGENTS / f"{label}.plist"
        launchctl("bootout", f"{domain}/{label}")  # no-op if not loaded
        path.write_text(plist_xml(plist_for(settings, name, s)))
        res = launchctl("bootstrap", domain, str(path))
        ok = "scheduled" if res.returncode == 0 else f"written, but launchctl said: {res.stderr.strip()}"
        print(f"{name}: {s['schedule']} -> {ok}")


def cmd_uninstall(names):
    settings, sweeps = load_config()
    prefix = settings["label_prefix"] + "."
    if names:
        labels = [label_for(settings, n) for n in names]
    else:
        labels = [p.stem for p in LAUNCH_AGENTS.glob(f"{prefix}*.plist")]
    domain = f"gui/{os.getuid()}"
    for label in labels:
        launchctl("bootout", f"{domain}/{label}")
        path = LAUNCH_AGENTS / f"{label}.plist"
        if path.exists():
            path.unlink()
        print(f"{label.removeprefix(prefix)}: unscheduled")


# --------------------------------------------------------------- list/add

def cmd_list():
    settings, sweeps = load_config()
    if not sweeps:
        print("No sweeps configured. Run: sweep.py add")
        return
    for name, s in sweeps.items():
        installed = (LAUNCH_AGENTS / f"{label_for(settings, name)}.plist").exists()
        state = "scheduled" if installed else ("enabled" if s["enabled"] else "disabled")
        print(f"{name:<20} {s['schedule']:<16} {state:<10} {s['query']}")


def ask(question, default=""):
    suffix = f" [{default}]" if default else ""
    answer = input(f"{question}{suffix}: ").strip()
    return answer or default


def toml_str(value):
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def cmd_add():
    print("New sweep. Press Enter to accept a default.\n")
    display = ask("Name of the newsletter or source (e.g. Lenny's Newsletter)")
    if not display:
        sys.exit("A name is required.")
    key = re.sub(r"[^a-z0-9]+", "-", display.lower()).strip("-")
    query = ask("Gmail search for these emails (e.g. from:hello@example.com)")
    folder = ask("Folder for the notes, relative to notes_root or absolute", display)
    topics = ask("Topic subfolders, comma separated", "Tactics, Strategy, Case Studies, News")
    schedule = ask("When should it run? e.g. 'daily 2pm', 'mon 8:30am', 'weekdays 7am,5pm'", "mon 8:30am")
    parse_schedule(schedule, key)
    keep = ask("What to keep verbatim (optional)")
    skip = ask("What to skip (optional)")

    topic_list = ", ".join(toml_str(t.strip()) for t in topics.split(",") if t.strip())
    lines = [
        "",
        f"[sweep.{key}]",
        f"display_name = {toml_str(display)}",
        f"query = {toml_str(query)}",
        f"folder = {toml_str(folder)}",
        f"topics = [{topic_list}]",
        f"schedule = {toml_str(schedule)}",
    ]
    if keep:
        lines.append(f"keep = {toml_str(keep)}")
    if skip:
        lines.append(f"skip = {toml_str(skip)}")

    if not CONFIG_PATH.exists():
        example = HERE / "sweeps.example.toml"
        header = example.read_text().split("\n[sweep.")[0] if example.exists() else "[settings]\n"
        CONFIG_PATH.write_text(header.rstrip() + "\n")
    with open(CONFIG_PATH, "a") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nAdded '{key}' to {CONFIG_PATH}.")
    print(f"Preview the prompt:  ./sweep.py prompt {key}")
    print(f"Test it now:         ./sweep.py run {key}")
    print(f"Schedule it:         ./sweep.py install {key}")


# ------------------------------------------------------------------ main

def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
    if cmd == "list":
        cmd_list()
    elif cmd == "add":
        cmd_add()
    elif cmd == "prompt" and len(args) == 1:
        settings, sweeps = load_config()
        print(build_prompt(settings, get_sweep(sweeps, args[0])))
    elif cmd == "run" and len(args) == 1:
        cmd_run(args[0])
    elif cmd == "install":
        cmd_install(args)
    elif cmd == "uninstall":
        cmd_uninstall(args)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
