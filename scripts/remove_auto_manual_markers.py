"""Remove the `.manual` markers the tools themselves used to write.

Until 2026-10-18 consolidate_packs, extract_variant_archives and the Pize plugin wrote `.manual` AND `.consolidated`
on folders they emptied. `.manual` is meant to be the user's alone, so this deletes the `.manual` file from every folder
that also has `.consolidated` (the folder stays hands-off for automated passes through `.consolidated`, see markers.py).
A `.manual` WITHOUT `.consolidated` is never touched.

    python scripts/remove_auto_manual_markers.py ROOT           (dry run: lists them)
    python scripts/remove_auto_manual_markers.py ROOT --apply
The list of removed paths is written to <ROOT>/.library-data/reports/ so it can be undone by hand.
Run it only after every copy of the tools has the matching update (otherwise old code would not skip these folders).
"""
import argparse
import os
import sys
import time

import action_log
import library_db


def find(root: str) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in action_log.SKIP_DIRNAMES]
        if '.manual' in filenames and '.consolidated' in filenames:
            out.append(os.path.relpath(dirpath, root))
    return sorted(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('root')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args(argv)
    root = os.path.abspath(os.path.expanduser(args.root))
    folders = find(root)
    print(f'{len(folders)} folder(s) have a tool-written .manual next to .consolidated.')
    if not folders:
        return 0
    if not args.apply:
        for f in folders[:10]:
            print(f'  {f}')
        print('Dry run. Re-run with --apply to remove those .manual files.')
        return 0
    log = os.path.join(library_db.data_dir(root, 'reports'), f'removed_auto_manual_{time.strftime("%Y%m%d_%H%M%S")}.txt')
    with open(log, 'w', encoding='utf-8') as f:
        f.write('\n'.join(os.path.join(x, '.manual') for x in folders) + '\n')
    for x in folders:
        os.remove(os.path.join(root, x, '.manual'))
    print(f'Removed {len(folders)} file(s). List: {log}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nCancelled.')
        sys.exit(130)
