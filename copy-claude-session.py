#!/usr/bin/env python3
"""Copy a Claude Code session under a new session id.

Works on macOS, Linux and Windows with Python 3.8+. The source session is
only read. The copy gets its own id, so it can be resumed on its own
(`claude --resume <new-id>`), placed in another config directory (another
account), and optionally registered in the Claude desktop app's Code tab.

Run without a session id in a terminal to pick one from a list with the
arrow keys (type to filter, Enter to select, Esc to quit).

Examples:
    copy-claude-session.py
    copy-claude-session.py <session-id>
    copy-claude-session.py <session-id> --no-desktop
    copy-claude-session.py <session-id> --dest-config-dir ~/.claude-work
    copy-claude-session.py <session-id> --target <account>/<org>
"""
import argparse
import contextlib
import json
import os
import re
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    import msvcrt
except ImportError:
    msvcrt = None

try:
    import select
    import termios
    import tty
except ImportError:
    termios = None

DROPPED_FIELDS = ("forkedFromSessionId", "spawnedFrom", "publishedArtifacts", "pendingSystemReminder")
CHUNK_LINES = 2000
AMBIGUITY_WINDOW_SECONDS = 3600
TITLE_SCAN_LINES = 300
TITLE_MAX_LINE_BYTES = 2_000_000
TITLE_MAX_CHARS = 80
DETAIL_GAP = 2
MIN_VISIBLE_ROWS = 5
FRAME_CHROME_ROWS = 4

KEY_UP, KEY_DOWN, KEY_PAGE_UP, KEY_PAGE_DOWN = "<up>", "<down>", "<pgup>", "<pgdn>"
KEY_HOME, KEY_END, KEY_ENTER, KEY_ESCAPE, KEY_BACKSPACE = "<home>", "<end>", "<enter>", "<esc>", "<backspace>"
POSIX_KEYS = {
    "\x1b[A": KEY_UP, "\x1bOA": KEY_UP,
    "\x1b[B": KEY_DOWN, "\x1bOB": KEY_DOWN,
    "\x1b[5~": KEY_PAGE_UP, "\x1b[6~": KEY_PAGE_DOWN,
    "\x1b[H": KEY_HOME, "\x1b[1~": KEY_HOME, "\x1bOH": KEY_HOME,
    "\x1b[F": KEY_END, "\x1b[4~": KEY_END, "\x1bOF": KEY_END,
    "\x1b": KEY_ESCAPE, "\r": KEY_ENTER, "\n": KEY_ENTER,
    "\x7f": KEY_BACKSPACE, "\x08": KEY_BACKSPACE,
}
WINDOWS_KEYS = {
    "H": KEY_UP, "P": KEY_DOWN, "I": KEY_PAGE_UP, "Q": KEY_PAGE_DOWN, "G": KEY_HOME, "O": KEY_END,
    "\x1b": KEY_ESCAPE, "\r": KEY_ENTER, "\n": KEY_ENTER, "\x08": KEY_BACKSPACE,
}


def fail(message):
    sys.exit(f"error: {message}")


def default_config_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def desktop_dir_candidates():
    home = Path.home()
    if sys.platform == "darwin":
        bases = [home / "Library" / "Application Support"]
    elif sys.platform in ("win32", "cygwin"):
        roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        packaged = sorted(local.glob("Packages/Claude_*/LocalCache/Roaming"))
        bases = [roaming, *packaged]
    else:
        bases = [Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")]
    return [base / "Claude" / "claude-code-sessions" for base in bases]


def default_desktop_dir():
    candidates = desktop_dir_candidates()
    return next((path for path in candidates if path.is_dir()), candidates[0])


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def parse_json_line(line):
    try:
        return json.loads(line)
    except ValueError:
        return None


def is_interactive():
    return sys.stdin.isatty() and sys.stdout.isatty()


def project_slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def format_size(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


class DesktopStore:
    def __init__(self, root):
        self.root = root

    def metadata_files(self):
        return self.root.glob("*/*/local_*.json") if self.root.is_dir() else iter(())

    @staticmethod
    def scope_of(path):
        return path.parent.parent.name, path.parent.name

    def find_by_cli_id(self, cli_id):
        return next((p for p in self.metadata_files() if (read_json(p) or {}).get("cliSessionId") == cli_id), None)

    def find_by_desktop_id(self, desktop_id):
        return next((p for p in self.metadata_files() if p.stem == desktop_id), None)

    def metadata_by_cli_id(self):
        found = {}
        for path in self.metadata_files():
            meta = read_json(path) or {}
            if meta.get("cliSessionId"):
                found[meta["cliSessionId"]] = meta
        return found

    def parse_scope(self, value):
        parts = value.strip("/\\").replace("\\", "/").split("/")
        if len(parts) != 2 or not (self.root / parts[0] / parts[1]).is_dir():
            fail(f"--target must be an existing <account>/<org> folder under {self.root}")
        return tuple(parts)

    def detect_target_scope(self, source_scope):
        latest = {}
        for path in self.metadata_files():
            scope = self.scope_of(path)
            if scope != source_scope:
                latest[scope] = max(latest.get(scope, 0), path.stat().st_mtime)
        if not latest:
            fail("no other desktop account found; pass --target <account>/<org> or --no-desktop")
        ranked = sorted(latest.items(), key=lambda item: item[1], reverse=True)
        if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < AMBIGUITY_WINDOW_SECONDS:
            options = [(scope, "/".join(scope), format_time(mtime)) for scope, mtime in ranked]
            if not is_interactive():
                fail(f"cannot tell which account is active; pass --target <account>/<org> (options: {', '.join(o[1] for o in options)})")
            return pick("Copy into which account?", options) or sys.exit(1)
        return ranked[0][0]


@dataclass
class SessionInfo:
    id: str
    title: str
    project: str
    mtime: float
    size: int


def format_time(timestamp):
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def clean_title(text):
    return " ".join(str(text).split())[:TITLE_MAX_CHARS]


def message_text(message):
    content = (message or {}).get("content")
    if isinstance(content, list):
        content = " ".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content if isinstance(content, str) else ""


def transcript_title(path):
    first_prompt = ""
    with path.open("rb") as reader:
        for index, line in enumerate(reader):
            if index >= TITLE_SCAN_LINES:
                break
            if len(line) > TITLE_MAX_LINE_BYTES:
                continue
            if b'"custom-title"' in line:
                title = (parse_json_line(line) or {}).get("customTitle")
                if title:
                    return clean_title(title)
            elif not first_prompt and b'"type":"user"' in line:
                first_prompt = clean_title(message_text((parse_json_line(line) or {}).get("message")))
    return first_prompt or "(untitled)"


def list_sessions(projects_dir, desktop_meta, only_slug):
    sessions = []
    for path in projects_dir.glob("*/*.jsonl"):
        if only_slug and path.parent.name != only_slug:
            continue
        meta = desktop_meta.get(path.stem, {})
        project = Path(meta["cwd"]).name if meta.get("cwd") else path.parent.name[-24:]
        stat = path.stat()
        title = clean_title(meta["title"]) if meta.get("title") else transcript_title(path)
        sessions.append(SessionInfo(path.stem, title, project, stat.st_mtime, stat.st_size))
    return sorted(sessions, key=lambda session: session.mtime, reverse=True)


@contextlib.contextmanager
def raw_keyboard():
    if termios is None:
        if sys.platform == "win32":
            os.system("")
        yield
        return
    descriptor = sys.stdin.fileno()
    saved = termios.tcgetattr(descriptor)
    tty.setcbreak(descriptor)
    try:
        yield
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)


def read_key():
    if msvcrt is not None:
        char = msvcrt.getwch()
        if char in ("\x00", "\xe0"):
            return WINDOWS_KEYS.get(msvcrt.getwch(), "")
        if char == "\x03":
            raise KeyboardInterrupt
        return WINDOWS_KEYS.get(char, char if char.isprintable() else "")
    chunk = os.read(sys.stdin.fileno(), 32).decode("utf-8", errors="ignore")
    if chunk in POSIX_KEYS:
        return POSIX_KEYS[chunk]
    return chunk if chunk.isprintable() else ""


def fit(text, width):
    return text if len(text) <= width else text[: max(width - 1, 0)] + "…"


def render_row(option, width, selected):
    _, title, detail = option
    title_width = max(width - len(detail) - DETAIL_GAP - 2, 10)
    row = f"{fit(title, title_width):<{title_width}}{' ' * DETAIL_GAP}{detail}"
    return f"\x1b[7m> {row}\x1b[0m" if selected else f"  {row}"


def pick_with_keys(prompt, options):
    columns, rows = shutil.get_terminal_size()
    window = max(min(len(options), rows - FRAME_CHROME_ROWS), MIN_VISIBLE_ROWS)
    query, index, top, drawn = "", 0, 0, 0

    def matches():
        words = query.lower().split()
        return [o for o in options if all(w in f"{o[1]} {o[2]}".lower() for w in words)]

    def frame(shown):
        header = f"{prompt}  filter: {query}_"
        footer = f"{len(shown)}/{len(options)}  ↑↓ move  PgUp/PgDn page  type to filter  Enter select  Esc quit"
        body = [render_row(o, columns - 1, i == index) for i, o in enumerate(shown[top : top + window], top)]
        body += ["  (no matches)"] * (not shown) + [""] * (window - len(body) - (not shown))
        return [fit(header, columns - 1), *body, fit(footer, columns - 1)]

    sys.stdout.write("\x1b[?25l")
    try:
        with raw_keyboard():
            while True:
                shown = matches()
                index = min(index, max(len(shown) - 1, 0))
                top = min(max(top, index - window + 1), index) if shown else 0
                lines = frame(shown)
                sys.stdout.write((f"\x1b[{drawn}A" if drawn else "") + "".join(f"\r\x1b[2K{l}\n" for l in lines))
                sys.stdout.flush()
                drawn = len(lines)
                key = read_key()
                if key == KEY_ESCAPE:
                    return None
                if key == KEY_ENTER and shown:
                    return shown[index][0]
                if key == KEY_UP:
                    index = max(index - 1, 0)
                elif key == KEY_DOWN:
                    index = min(index + 1, max(len(shown) - 1, 0))
                elif key == KEY_PAGE_UP:
                    index = max(index - window, 0)
                elif key == KEY_PAGE_DOWN:
                    index = min(index + window, max(len(shown) - 1, 0))
                elif key == KEY_HOME:
                    index = 0
                elif key == KEY_END:
                    index = max(len(shown) - 1, 0)
                elif key == KEY_BACKSPACE:
                    query, index, top = query[:-1], 0, 0
                elif key and not key.startswith("<"):
                    query, index, top = query + key, 0, 0
    finally:
        sys.stdout.write(f"\x1b[{drawn}A\x1b[J\x1b[?25h" if drawn else "\x1b[?25h")
        sys.stdout.flush()


def pick_with_numbers(prompt, options):
    for number, (_, title, detail) in enumerate(options, 1):
        print(f"{number:>3}. {title}  {detail}")
    answer = input(f"{prompt} number (blank to quit): ").strip()
    if not answer:
        return None
    if not answer.isdigit() or not 1 <= int(answer) <= len(options):
        fail("invalid selection")
    return options[int(answer) - 1][0]


def pick(prompt, options):
    if not options:
        fail("nothing to choose from")
    keys_available = msvcrt is not None or termios is not None
    return pick_with_keys(prompt, options) if keys_available else pick_with_numbers(prompt, options)


def choose_session(sessions, show_project):
    if not sessions:
        fail("no sessions found; try --all-projects or --config-dir")
    options = [
        (s.id, s.title, f"{s.id}  {format_time(s.mtime)}  {format_size(s.size):>8}" + (f"  {s.project}" if show_project else ""))
        for s in sessions
    ]
    return pick("Select a session to copy.", options) or sys.exit(1)


def find_transcript(projects_dir, cli_id):
    matches = list(projects_dir.glob(f"*/{cli_id}.jsonl"))
    if not matches:
        fail(f"no transcript {cli_id}.jsonl under {projects_dir}")
    return matches[0]


def copy_transcript(source, destination, old_id, new_id):
    old = f'"sessionId":"{old_id}"'.encode()
    new = f'"sessionId":"{new_id}"'.encode()
    temp = destination.with_name(destination.name + ".tmp")
    lines = 0
    try:
        with source.open("rb") as reader, temp.open("wb") as writer:
            buffer = []
            for line in reader:
                buffer.append(line.replace(old, new))
                lines += 1
                if len(buffer) >= CHUNK_LINES:
                    writer.writelines(buffer)
                    buffer.clear()
            writer.writelines(buffer)
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)
    return lines


def transcript_cwd(transcript):
    with transcript.open("rb") as reader:
        for line in reader:
            if b'"cwd"' in line:
                cwd = (parse_json_line(line) or {}).get("cwd")
                if cwd:
                    return cwd
    return str(Path.home())


def build_metadata(source_meta, cli_id, new_id, transcript, title_suffix):
    now = int(time.time() * 1000)
    if source_meta is not None:
        meta = read_json(source_meta) or {}
    else:
        cwd = transcript_cwd(transcript)
        meta = {"cwd": cwd, "originCwd": cwd, "createdAt": now, "permissionMode": "default", "title": cli_id[:8]}
    for field in DROPPED_FIELDS:
        meta.pop(field, None)
    meta.update(
        sessionId=f"local_{new_id}",
        cliSessionId=new_id,
        title=f"{meta.get('title', cli_id[:8])}{title_suffix}",
        titleSource="user",
        isArchived=False,
        remoteControlUserEnabled=False,
        lastFocusedAt=now,
        lastActivityAt=now,
    )
    return meta


def parse_args():
    parser = argparse.ArgumentParser(
        description="Copy a Claude Code session under a new session id. The source session is only read."
    )
    parser.add_argument("session_id", nargs="?", help="CLI session id (uuid) or desktop session id (local_<uuid>); omit to pick from a list")
    parser.add_argument("--all-projects", action="store_true", help="list sessions from every project, not just the current directory's")
    parser.add_argument("--config-dir", type=Path, default=default_config_dir(), help="source config dir (default: $CLAUDE_CONFIG_DIR or ~/.claude)")
    parser.add_argument("--dest-config-dir", type=Path, help="write the copy into this config dir instead (e.g. another account's)")
    parser.add_argument("--desktop-dir", type=Path, default=default_desktop_dir(), help="desktop app claude-code-sessions folder")
    parser.add_argument("--target", help="desktop <account>/<org> to register the copy under (auto-detected when omitted)")
    parser.add_argument("--no-desktop", action="store_true", help="copy the CLI session only, skip the desktop sidebar entry")
    parser.add_argument("--title-suffix", default=" (copy)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan without writing anything")
    return parser.parse_args()


def select_session_id(args, store, source_projects):
    if args.session_id:
        return args.session_id
    if not is_interactive():
        fail("no session id given and no interactive terminal to pick from")
    current_slug = project_slug(Path.cwd())
    in_current_project = not args.all_projects and (source_projects / current_slug).is_dir()
    sessions = list_sessions(source_projects, store.metadata_by_cli_id(), current_slug if in_current_project else None)
    return choose_session(sessions, show_project=not in_current_project)


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    args = parse_args()
    store = DesktopStore(args.desktop_dir.expanduser())
    source_projects = args.config_dir.expanduser() / "projects"
    dest_projects = (args.dest_config_dir or args.config_dir).expanduser() / "projects"

    cli_id = select_session_id(args, store, source_projects)
    if cli_id.startswith("local_"):
        source_meta = store.find_by_desktop_id(cli_id)
        if source_meta is None:
            fail(f"no desktop session {cli_id} found under {store.root}")
        cli_id = (read_json(source_meta) or {}).get("cliSessionId") or fail(f"{source_meta} has no cliSessionId")
    else:
        source_meta = store.find_by_cli_id(cli_id)

    transcript = find_transcript(source_projects, cli_id)
    new_id = str(uuid.uuid4())
    destination_dir = dest_projects / transcript.parent.name
    destination = destination_dir / f"{new_id}.jsonl"
    sidecar = transcript.with_suffix("")

    register = not args.no_desktop and store.root.is_dir()
    target_scope = None
    if register:
        source_scope = store.scope_of(source_meta) if source_meta else None
        target_scope = store.parse_scope(args.target) if args.target else store.detect_target_scope(source_scope)
    elif args.target:
        fail("--target needs the desktop folder; check --desktop-dir or drop --no-desktop")

    print(f"source      {transcript} ({format_size(transcript.stat().st_size)})")
    print(f"source meta {source_meta or 'none'}")
    print(f"copy        {destination}")
    print(f"desktop     {'/'.join(target_scope) if target_scope else 'not registered'}")
    print(f"new id      {new_id}")
    if args.dry_run:
        return

    destination_dir.mkdir(parents=True, exist_ok=True)
    lines = copy_transcript(transcript, destination, cli_id, new_id)
    if sidecar.is_dir():
        shutil.copytree(sidecar, destination_dir / new_id)
    if target_scope:
        meta_path = store.root / target_scope[0] / target_scope[1] / f"local_{new_id}.json"
        meta_path.write_text(json.dumps(build_metadata(source_meta, cli_id, new_id, transcript, args.title_suffix)), encoding="utf-8")
    print(f"copied {lines} lines")
    if target_scope:
        print("restart the Claude desktop app to see it in the sidebar")
    print(f"resume from the CLI: claude --resume {new_id}")


if __name__ == "__main__":
    main()
