# Creator-specific scripts

Drop a `.py` file in this folder to add your own creator-specific fix-up
script to the "Creator-specific scripts" submenu (`s` in the main menu) —
no need to touch `run.sh`, `run.bat`, or any core file. This is where
one-creator naming quirks belong (e.g. a creator who prefixes files with
`SMOOTH `), as opposed to `scripts/`'s top-level numbered options, which
are general-purpose across every creator.

## Contract

Your file needs exactly two things at module level:

```python
MENU_LABEL = "Short name shown in the menu"
MENU_DESCRIPTION = "One or two lines of detail shown under the label (optional)"

def run(base_path: str) -> None:
    ...
```

Optional extras:

```python
SCRIPT_ID = "my_script"          # default: the file name; used as the undo
                                 # journal name and folder_log script name

def run(base_path: str, options: dict | None = None) -> None:
    ...                          # options is None when run from the menu
                                 # (prompt as usual); a dict when run
                                 # unattended (never prompt then)

def setup_unattended(current: dict) -> dict:
    ...                          # ask your own questions for 'sa' (Enter
                                 # keeps current), return the options dict

REPLACES_DOWNLOAD = True         # only for scripts that can stand in for
                                 # the normal download step (see below)
RECOMMENDED_CONFIG = {...}       # creator_config.json values to *offer*
                                 # when a user picks this as a download script
```

A plugin with `setup_unattended` (or none -- it's then run with `{}`)
shows up as a step in the unattended setup (`sa`) automatically.

`run()` is called with the directory the user entered when they picked
your script from the submenu. What you do inside it is entirely up to
you — rename files, fix funscripts, whatever the quirk needs. Prompt for
anything else you need (extensions, options, ...) from inside `run()`
itself, same as any of this project's other scripts.

## Recommended: use action_log for anything reversible

If your script renames, copies, or soft-deletes files, wire it into its
**own** undo journal (`journal=SCRIPT_ID`). It's then undone from this
submenu's `u) Undo a creator script's last run`, separately from the main
menu's "Undo last action" (option `z`) for the core tools:

```python
import action_log

action_log.start(SCRIPT_ID, base_path, journal=SCRIPT_ID)
try:
    ...
    action_log.record('rename', old_path=old, new_path=new)  # or 'copy' / 'copytree' / 'soft_delete'
    ...
finally:
    action_log.finish()
```

Anything core your script calls that journals its own changes (e.g.
`downloadContent.find_and_download`) nests into your journal while it's
active, so undoing your script undoes that too.

## Download-replacement scripts and creator_config.json

A creator folder can hold a hand-editable `creator_config.json` (see
`scripts/creator_config.py`). Every flag in it is explicit opt-in: it's
only set by hand, via `sa`, or via this submenu's `c) Configure creator
flags`. No script writes it on its own, and having a plugin for a creator
never implies any flag. The flags:

- `download_script`: a plugin with `REPLACES_DOWNLOAD = True` that runs
  *instead of* the normal download (and archive extraction) for that
  creator. The normal process then only runs when forced.
- `sync_exclude`: kinds/extensions sync_new_folders must not copy.
- `protected_dirs`: subfolders every core tree-walking script skips.

See `mdemaxis_smooth_fix.py` in this folder for a real, working example.

## Skip `.trash` when walking base_path

If your script does its own `os.walk(base_path)`, prune out
`action_log.TRASH_DIRNAME` the same way `mdemaxis_smooth_fix.py` does (and
the creator's `protected_dirs`, via `creator_config.prune`, unless your
script is the one that owns them) —
soft-deleted files live there and shouldn't be renamed, re-hashed, or
otherwise touched by anything except `action_log`'s own trash/undo
machinery:

```python
for dirpath, dirnames, filenames in os.walk(base_path):
    if action_log.TRASH_DIRNAME in dirnames:
        dirnames.remove(action_log.TRASH_DIRNAME)
    ...
```

## Loading

Every `.py` file in this folder (not starting with `_`) is imported and
checked for `MENU_LABEL` and `run()` automatically by
`scripts/creator_scripts_menu.py`. A file missing either is skipped with a
warning instead of crashing the menu — a broken plugin can't take down
anything else. Errors raised while a script actually *runs* are your own
script's responsibility to handle or let surface; the submenu doesn't
catch those.
