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

`run()` is called with the directory the user entered when they picked
your script from the submenu. What you do inside it is entirely up to
you — rename files, fix funscripts, whatever the quirk needs. Prompt for
anything else you need (extensions, options, ...) from inside `run()`
itself, same as any of this project's other scripts.

## Recommended: use action_log for anything reversible

If your script renames, copies, or soft-deletes files, wire it into the
shared undo journal so it participates in the main menu's "Undo last
action" (option `z`) the same way the built-in scripts do:

```python
import action_log

action_log.start('my_script_name', base_path)
...
action_log.record('rename', old_path=old, new_path=new)  # or 'copy' / 'copytree' / 'soft_delete'
...
action_log.finish()
```

See `mdemaxis_smooth_fix.py` in this folder for a real, working example.

## Loading

Every `.py` file in this folder (not starting with `_`) is imported and
checked for `MENU_LABEL` and `run()` automatically by
`scripts/creator_scripts_menu.py`. A file missing either is skipped with a
warning instead of crashing the menu — a broken plugin can't take down
anything else. Errors raised while a script actually *runs* are your own
script's responsibility to handle or let surface; the submenu doesn't
catch those.
