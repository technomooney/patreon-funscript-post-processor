#!/usr/bin/env python3
"""
Interactive, one-time-per-creator setup for an unattended pipeline run.

Configure once: source/destination paths, which of this project's pipeline
steps to run, in what order, and the answer to every prompt each of those
steps would normally ask interactively. Saved per creator (derived from the
destination folder's name, same convention extract_variant_archives.py and
discord_passwords.py already use) via creator_profiles.py, alongside that
creator's Discord channel config if it has one.

Re-running this against an already-configured creator loads its saved
config as the defaults for every question (press Enter to keep), the same
"Enter to keep" convention setup_config.py's credentials wizard uses --
so this doubles as the way to edit an existing config, not just create one.

The saved config is later replayed with zero prompts by run_unattended.py
(menu option 'ra'). This script only ever collects answers; it never runs
anything itself.

Usage
-----
  python setup_unattended.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import creator_profiles

# (step_id, menu_label, short description shown in the step-picker)
_STEP_CATALOG = [
    ('sync_new_folders', 'Sync new folders',
     'copy new folders in, then symmetry-check existing ones for files missing by content'),
    ('extract_archives', 'Extract variant archives',
     'extract password-protected script archives (e.g. Pize-style intensity variants)'),
    ('download', 'Download content',
     'find links in description.json files and download videos/files'),
    ('download_from_funscript_metadata', 'Download from funscript metadata',
     "download a video from a funscript's own metadata.video_url when there's no local video yet"),
    ('prefix_fix', 'Fix file prefixes',
     'strip the attachment ID prefix from downloaded filenames'),
    ('fix_garbled_names', 'Fix garbled names',
     'decode percent-encoded/mojibake filenames, fuzzy-match funscripts to their video'),
    ('check_funscripts', 'Check funscript match',
     'find videos missing a funscript, optionally auto-rename a lone unmatched video'),
    ('dedupe', 'Dedupe (+ consolidate packs)',
     'remove exact duplicates and consolidate cross-folder video/pack redundancy'),
    ('generate_html', 'Generate HTML',
     'build a description.html visual overview in each post folder'),
    ('generate_audit_report', 'Audit report',
     'generate _reports/audit_report.html from every folder\'s .folder_log.json'),
]
_STEP_BY_ID = {step_id: (label, desc) for step_id, label, desc in _STEP_CATALOG}


def _ask(label: str, current: str) -> str:
    """Prompt with the current value in brackets; Enter keeps it. Same
    convention as setup_config.py's own _ask()."""
    hint = f'[{current}]' if current else '[not set]'
    value = input(f'  {label} {hint}: ').strip()
    return value if value else current


def _ask_bool(label: str, current: bool) -> bool:
    hint = 'y' if current else 'n'
    raw = input(f'  {label} (y/n) [{hint}]: ').strip().lower()
    if raw in ('y', 'yes'):
        return True
    if raw in ('n', 'no'):
        return False
    return current


def _ask_scope(current: str) -> str:
    hint = 'a' if current == 'all' else 'F'
    raw = input(f"  Symmetry check scope -- funscripts only or all files? [F/a] [{hint}]: ").strip().lower()
    if raw == 'a':
        return 'all'
    if raw == 'f':
        return 'funscripts'
    return current


def _ask_step_fields(step_id: str, saved: dict) -> dict:
    """Ask the specific questions for *step_id*, using *saved* (that step's
    previously-saved fields, {} if new) as defaults. Returns the new fields
    dict (never includes 'id' -- the caller adds that)."""
    if step_id == 'sync_new_folders':
        return {
            'auto_confirm_copy': _ask_bool('Auto-copy new folders without asking each time?',
                                            saved.get('auto_confirm_copy', True)),
            'run_symmetry': _ask_bool('Run the symmetry check on existing folders?',
                                       saved.get('run_symmetry', True)),
            'symmetry_scope': _ask_scope(saved.get('symmetry_scope', 'funscripts')),
            'auto_confirm_symmetry_copy': _ask_bool('Auto-copy symmetry-check results without asking?',
                                                     saved.get('auto_confirm_symmetry_copy', True)),
        }
    if step_id == 'extract_archives':
        return {
            'ignore_manual': _ask_bool('Also process archives in .manual-marked folders?',
                                        saved.get('ignore_manual', False)),
        }
    if step_id == 'download':
        return {
            'require_funscript': _ask_bool('Require a funscript to already exist before downloading?',
                                            saved.get('require_funscript', True)),
            'resume': _ask_bool('Auto-resume an interrupted previous session?',
                                 saved.get('resume', True)),
        }
    if step_id == 'download_from_funscript_metadata':
        return {
            'resume': _ask_bool('Auto-resume an interrupted previous session?',
                                 saved.get('resume', True)),
        }
    if step_id == 'prefix_fix':
        current_ext = ';'.join(e.lstrip('.') for e in saved.get('extensions', ['funscript']))
        raw = _ask('File extensions to process, semicolon-separated (blank = all)', current_ext)
        extensions = [f'.{e.strip()}' for e in raw.split(';') if e.strip()] if raw else []
        return {
            'extensions': extensions,
            'reprocess_all': _ask_bool('Reprocess folders already marked done by a previous run?',
                                        saved.get('reprocess_all', False)),
        }
    if step_id == 'check_funscripts':
        return {
            'auto_rename': _ask_bool(
                'Auto-rename a lone unmatched video when duration confirms it unambiguously?',
                saved.get('auto_rename', False)),
        }
    # fix_garbled_names, dedupe, generate_html, generate_audit_report take no
    # per-step choices -- they either have no prompts to begin with, or (like
    # fix_garbled_names' dry_run) always run for-real in an unattended context.
    return {}


def _pick_steps(existing_steps: list[dict]) -> list[str]:
    print()
    print('Available steps:')
    for i, (step_id, label, desc) in enumerate(_STEP_CATALOG, 1):
        print(f'  {i:2d}) {label:32s} — {desc}')
    print()
    if existing_steps:
        current_order = ','.join(
            str(next(i for i, (sid, _, _) in enumerate(_STEP_CATALOG, 1) if sid == s['id']))
            for s in existing_steps if s['id'] in _STEP_BY_ID
        )
        print(f'  Current order: {current_order}')
    print('Enter the step numbers you want, in the order to run them (e.g. 1,3,2,6,7,8,9,10).')

    while True:
        raw = input('  Steps: ').strip()
        if not raw:
            if existing_steps:
                return [s['id'] for s in existing_steps]
            print('  Enter at least one step number.')
            continue
        try:
            indices = [int(x.strip()) for x in raw.split(',') if x.strip()]
        except ValueError:
            print('  Please enter step numbers separated by commas.')
            continue
        if not indices:
            print('  Enter at least one step number.')
            continue
        if len(set(indices)) != len(indices):
            print('  Each step can only appear once.')
            continue
        if any(i < 1 or i > len(_STEP_CATALOG) for i in indices):
            print(f'  Numbers must be between 1 and {len(_STEP_CATALOG)}.')
            continue
        return [_STEP_CATALOG[i - 1][0] for i in indices]


def main() -> None:
    print()
    print('========================================')
    print('  Set Up Unattended Run')
    print('========================================')
    print()
    print("Configure which steps run for a creator, in what order, and the")
    print("answer to every prompt each step would normally ask -- so 'ra'")
    print("(Run unattended) can replay it later with no prompts at all.")
    print("Re-run this against an already-configured creator to edit it")
    print("(press Enter at any prompt to keep the current value).")
    print()

    # Destination first, not source -- it's what determines creator_key, which
    # is needed before an existing config's other fields (including source)
    # can be looked up and offered as defaults.
    destination = input('Destination folder (post-processor working dir): ').strip().strip('"\'')
    if not os.path.isdir(destination):
        print(f'Directory not found: {destination}')
        return
    destination = os.path.abspath(destination)
    creator_key = os.path.basename(destination).strip().lower()

    existing = creator_profiles.get_unattended_config(creator_key) or {}
    existing_steps = existing.get('steps', [])
    if existing:
        print(f'\nExisting config found for "{creator_key}" — loaded as defaults.')

    source = _ask('Source folder (Patreon downloader output)', existing.get('source', ''))
    if not os.path.isdir(source):
        print(f'Directory not found: {source}')
        return
    source = os.path.abspath(source)

    step_ids = _pick_steps(existing_steps)
    existing_by_id = {s['id']: s for s in existing_steps}

    print()
    print('Answer each selected step\'s questions (Enter keeps the current value):')
    steps = []
    for step_id in step_ids:
        label, _desc = _STEP_BY_ID[step_id]
        print(f'\n--- {label} ---')
        fields = _ask_step_fields(step_id, existing_by_id.get(step_id, {}))
        steps.append({'id': step_id, **fields})

    config = {'source': source, 'destination': destination, 'steps': steps}
    creator_profiles.set_unattended_config(creator_key, config)

    print()
    print(f'Saved unattended config for "{creator_key}" ({len(steps)} step(s)).')
    print("Run it any time with menu option 'ra' (Run unattended).")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n\nCancelled.')
