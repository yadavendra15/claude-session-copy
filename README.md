# claude-session-share

Copy or transfer a [Claude Code](https://claude.com/claude-code) session to another account.

- **Copy** writes the session under a new id. The original is only read and stays where it is.
- **Transfer** moves the session and keeps its id. It disappears from the source account and appears in the target one.

A single Python file, standard library only. Works on macOS, Linux and Windows with Python 3.8+.

## Why

Claude Code keeps a session's transcript on disk, but the Claude desktop app lists sessions per account. Switching accounts leaves your history behind. This tool puts a session where the other account can see it, and the same copy can be resumed from the CLI with `claude --resume <id>`.

## Install

```bash
git clone https://github.com/yadavendra15/claude-session-share.git
cd claude-session-share
```

No dependencies to install.

## Usage

Pick a session from a list, then choose copy or transfer:

```bash
python3 claude-session-share.py
```

Or pass a session id, either the CLI id (a uuid) or the desktop id (`local_<uuid>`):

```bash
python3 claude-session-share.py <session-id> --mode copy
python3 claude-session-share.py <session-id> --mode transfer
```

Preview without changing anything:

```bash
python3 claude-session-share.py --mode transfer --dry-run
```

On Windows use `py` or `python` instead of `python3`.

### The picker

When no session id is given, a list opens with the title, id, last-modified time and size of each session, newest first.

| Key | Action |
| --- | --- |
| Up / Down | Move |
| PgUp / PgDn, Home / End | Jump |
| Type | Filter the list |
| Enter | Select |
| Esc | Quit |

By default only the current folder's project is listed. Use `--all-projects` for every project. Without `--mode`, a second prompt asks whether to copy or transfer; Copy is the first row, and non-interactive runs default to copy.

### Options

| Option | Meaning |
| --- | --- |
| `--mode {copy,transfer}` | Skip the copy or transfer prompt |
| `--target <account>/<org>` | Desktop account folder to register the session under. Auto-detected when only one other account exists, otherwise asked |
| `--dest-config-dir DIR` | Write into another Claude config folder, for accounts kept in separate `CLAUDE_CONFIG_DIR`s |
| `--no-desktop` | Handle the CLI files only, skip the desktop sidebar entry |
| `--all-projects` | List sessions from every project |
| `--yes` | Skip the confirmation before a transfer |
| `--title-suffix TEXT` | Appended to the title of a copy (default ` (copy)`) |
| `--config-dir DIR` | Source config folder (default `$CLAUDE_CONFIG_DIR` or `~/.claude`) |
| `--desktop-dir DIR` | Desktop app `claude-code-sessions` folder, if it is not in the default place |
| `--dry-run` | Print the plan and change nothing |

After a copy or transfer, restart the Claude desktop app to see the change in the sidebar.

## How it works

- A session is a transcript at `<config dir>/projects/<project>/<session id>.jsonl`, plus an optional folder of the same name with subagent transcripts and tool results.
- The desktop app keeps one small `local_<id>.json` per session under `claude-code-sessions/<account>/<org>/`.
- **Copy** streams the transcript to a new id, rewriting only the `sessionId` field, copies the session folder, and writes a new desktop entry in the target account. Writes go to a temporary file first and are renamed at the end, so an interrupted copy leaves nothing half-written.
- **Transfer** moves the desktop entry to the target account. Since the transcript folder is shared by all accounts, the large file is not touched. With `--dest-config-dir` it moves the transcript and session folder too.
- Transfer refuses to run if the session was written to in the last 10 seconds, if it is already in the target account, or if the destination file already exists.

Desktop folder locations:

| OS | Path |
| --- | --- |
| macOS | `~/Library/Application Support/Claude/claude-code-sessions` |
| Windows | `%APPDATA%\Claude\claude-code-sessions` (or the Microsoft Store package equivalent) |
| Linux | `$XDG_CONFIG_HOME/Claude/claude-code-sessions` (default `~/.config`) |

## Caveats

- The desktop app's session file layout is internal and not documented. A future app update could change it and break the sidebar entry. The CLI side only relies on the transcript format.
- Close a session before transferring it. Moving a session that is still running can corrupt it.
- Transcripts can hold secrets, tokens and private code. Copying one to another account makes it readable there.
- The Windows and Linux desktop paths are best guesses and less tested than macOS. Pass `--desktop-dir` if the default is wrong.

## License

MIT. See [LICENSE](LICENSE).
