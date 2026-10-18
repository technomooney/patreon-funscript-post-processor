"""Compare a library on disk with what the shared database expects, and list every difference.

    ./check_drift.sh /path/to/library-root                 (read-only report)
    ... --csv drift.csv                                    (also write the findings to a CSV file)
    ... --accept 12                                        (adopt the disk's state for finding #12)
    ... --accept-all changed                               (adopt every open finding of a kind)

Reports `.manual` / `.consolidated` marker locations, files that were replaced or edited by hand (size differs from
what was recorded), files that disappeared, and probable renames. It never modifies a library file; --accept only
updates the database's record of the file. Ctrl+C prints "Cancelled.".
"""
import argparse
import csv
import os
import sys

import library_db
import library_drift


def _print_report(root: str, report: dict) -> None:
    print(f'Library: {root}')
    markers = report['markers']
    print(f'  active markers: {len(markers)} ({sum(1 for m in markers if m["kind"] == ".manual")} .manual, '
          f'{sum(1 for m in markers if m["kind"] == ".consolidated")} .consolidated)')
    for m in markers[:25]:
        print(f'      {m["kind"]:<14} {m["path"] or "(library root)"}')
    if len(markers) > 25:
        print(f'      ... and {len(markers) - 25} more (use --csv for the full list)')
    findings = library_drift.open_findings(root)
    print(f'  open differences: {len(findings)}   (files in tracked folders not in the database: '
          f'{report["untracked_in_tracked_folders"]}, not counted as differences)')
    for f in findings[:50]:
        extra = ''
        d = f['detail']
        if f['kind'] == 'changed':
            extra = f'  {d.get("recorded_size")} -> {d.get("actual_size")} bytes'
        elif f['kind'] == 'renamed_suspected':
            extra = f'  maybe now: {", ".join(d.get("candidates", [])[:3])}'
        elif f['kind'] == 'missing' and d.get('in_trash'):
            extra = '  (it is in .trash)'
        print(f'    #{f["id"]:<5} {f["kind"]:<18} {f["path"]}{extra}')
    if len(findings) > 50:
        print(f'    ... and {len(findings) - 50} more (use --csv for all)')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Report differences between a library and its database (read-only)')
    parser.add_argument('root', help='library root folder')
    parser.add_argument('--csv', help='also write all open differences and markers to this CSV file')
    parser.add_argument('--accept', type=int, action='append', default=[], metavar='ID',
                        help='adopt the disk state for this finding id (repeatable)')
    parser.add_argument('--accept-all', metavar='KIND', help='adopt every open finding of this kind '
                                                             '(changed, missing, renamed_suspected, marker_added, marker_removed)')
    args = parser.parse_args(argv)
    root = os.path.abspath(os.path.expanduser(args.root))
    if not os.path.isdir(root):
        print(f'Not a folder: {root}')
        return 2
    if not os.path.isdir(os.path.join(root, library_db.DATA_DIRNAME)):
        print(f'No library database in {root}. Run migrate.sh on it first (it is a dry run unless you add --apply).')
        return 2
    report = library_drift.scan(root)
    ids = list(args.accept)
    if args.accept_all:
        ids += [f['id'] for f in library_drift.open_findings(root, args.accept_all)]
    for i in ids:
        print(f'  accept #{i}: {library_drift.accept(root, i)}')
    _print_report(root, report)
    if args.csv:
        with open(args.csv, 'w', newline='', encoding='utf-8') as fh:
            w = csv.writer(fh)
            w.writerow(['id', 'kind', 'path', 'detail'])
            for f in library_drift.open_findings(root):
                w.writerow([f['id'], f['kind'], f['path'], f['detail_json']])
            for m in report['markers']:
                w.writerow(['', 'marker:' + m['kind'], m['path'], ''])
        print(f'  written to {args.csv}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nCancelled.')
        sys.exit(130)
