"""MDemaxis-only: rename SMOOTH-prefixed and _maxinterval-suffixed files to
variant naming.

  SMOOTH example.funscript        ->  example (SMOOTH).funscript
  example_maxinterval.funscript   ->  example (max interval).funscript

Works on any file extension — default is funscript. The first working
example of the creator_scripts contract; see README.md in this folder.
"""
import os

import action_log

MENU_LABEL = 'MDemaxis rename fix'
MENU_DESCRIPTION = (
    'MDemaxis patreon only: rename SMOOTH-prefixed and _maxinterval-suffixed\n'
    'funscripts to variant naming (e.g. SMOOTH x.funscript -> x (SMOOTH).funscript)'
)


def _resolve_new_name(filename: str) -> tuple[str, str] | None:
    """Return (new_filename, rule_label) if a rename rule matches, else None.
    Rules are checked in order; only the first match is applied."""
    stem, ext = os.path.splitext(filename)

    if filename.startswith('SMOOTH '):
        base = stem[len('SMOOTH '):]
        return f'{base} (SMOOTH){ext}', 'SMOOTH prefix'

    if stem.endswith('_maxinterval'):
        base = stem[: -len('_maxinterval')]
        return f'{base} (max interval){ext}', 'max interval suffix'

    return None


def process(root_dir: str, extensions: list[str]) -> int:
    """Walk *root_dir* and rename matching files. *extensions* is a list of
    lowercase dot-prefixed extensions, e.g. ['.funscript']. Returns the
    number of files renamed."""
    renamed = 0
    for dirpath, _, filenames in os.walk(root_dir):
        if '.manual' in filenames:
            print(f'  SKIP (manual)  {dirpath}')
            continue
        for filename in filenames:
            ext = os.path.splitext(filename)[1].lower()
            if extensions and ext not in extensions:
                continue

            result = _resolve_new_name(filename)
            if result is None:
                continue

            new_name, rule = result
            old_path = os.path.join(dirpath, filename)
            new_path = os.path.join(dirpath, new_name)

            if os.path.exists(new_path):
                print(f'  SKIP (target exists) [{rule}]  {filename}')
                continue

            print(f'  RENAME [{rule}]')
            print(f'    {old_path}')
            print(f'    -> {new_path}')
            try:
                os.rename(old_path, new_path)
                action_log.record('rename', old_path=old_path, new_path=new_path)
                renamed += 1
            except OSError as e:
                print(f'  ERROR: {e}')

    return renamed


def run(base_path: str) -> None:
    raw = input('File extensions to process, separated by semicolons (default: funscript): ').strip()
    if raw:
        extensions = ['.' + e.lstrip('.').lower() for e in raw.split(';') if e.strip()]
    else:
        extensions = ['.funscript']

    print(f'\nProcessing: {base_path}')
    print(f'Extensions: {", ".join(extensions)}\n')

    action_log.start('mdemaxis_smooth_fix', base_path)
    count = process(base_path, extensions)
    action_log.finish()
    print(f'\nDone. {count} file(s) renamed.')
