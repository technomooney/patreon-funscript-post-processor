#!/usr/bin/env python3
"""
Replay a creator's saved unattended config (built by setup_unattended.py)
with zero prompts -- every step's answers were already collected, so this
just runs them in the saved order and walks away.

A failed step is logged and skipped; the run continues to the next step
(per the user's explicit choice -- one bad step shouldn't strand a long
unattended run). Every run also writes its own log file under
<destination>/_reports/, in addition to normal terminal output, since
nobody's watching it live.

Usage
-----
  python run_unattended.py [creator folder]

  creator folder   the destination folder used when this creator was set
                    up (menu option 'sa') -- creator_key is derived from
                    its basename, same convention setup_unattended.py uses.
                    Prompted for if omitted.
"""
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import creator_profiles


class _Tee:
    """Mirrors every write to two streams -- used to send this run's output
    to both the real terminal and its own log file at once.

    Also forwards isatty()/reconfigure() to the real terminal stream
    specifically (assumed to be the first one passed in) -- downloadContent.py
    calls both on sys.stdout at import time (_STATUS_TTY, UTF-8 reconfigure
    on Windows), and a plain object without them would crash that import the
    moment any step needing it runs under this wrapper.
    """

    def __init__(self, *streams):
        self._streams = streams
        self._primary = streams[0]

    def write(self, data):
        for s in self._streams:
            s.write(data)

    def flush(self):
        for s in self._streams:
            s.flush()

    def isatty(self):
        return self._primary.isatty()

    def reconfigure(self, *args, **kwargs):
        for s in self._streams:
            if hasattr(s, 'reconfigure'):
                s.reconfigure(*args, **kwargs)


def _run_sync_new_folders(cfg: dict, fields: dict) -> None:
    import sync_new_folders
    scope = fields.get('symmetry_scope', 'funscripts')
    sync_new_folders.run(
        cfg['source'], cfg['destination'],
        auto_confirm=fields.get('auto_confirm_copy', True),
        run_symmetry=fields.get('run_symmetry', True),
        funscripts_only=(scope != 'all'),
        auto_confirm_copy=fields.get('auto_confirm_symmetry_copy', True),
    )


def _run_extract_archives(cfg: dict, fields: dict) -> None:
    import extract_variant_archives
    extract_variant_archives.scan_and_extract(
        cfg['destination'], ignore_manual=fields.get('ignore_manual', False))


def _run_download(cfg: dict, fields: dict) -> None:
    import downloadContent
    # auto_confirm is always True here -- there's no scenario where an
    # unattended run should stop and wait at "Proceed with downloads?";
    # setup_unattended.py deliberately never asks about it for that reason.
    downloadContent.find_and_download(
        cfg['destination'],
        require_funscript=fields.get('require_funscript', True),
        resume=fields.get('resume', True),
        auto_confirm=True,
    )


def _run_download_from_funscript_metadata(cfg: dict, fields: dict) -> None:
    import downloadContent
    downloadContent.find_and_download_from_funscript_metadata(
        cfg['destination'], resume=fields.get('resume', True), auto_confirm=True)


def _run_prefix_fix(cfg: dict, fields: dict) -> None:
    import prefixFix
    prefixFix.run(cfg['destination'], fields.get('extensions', []), fields.get('reprocess_all', False))


def _run_fix_garbled_names(cfg: dict, fields: dict) -> None:
    import fix_garbled_names
    fix_garbled_names.run(cfg['destination'], dry_run=False)


def _run_check_funscripts(cfg: dict, fields: dict) -> None:
    import check_funscripts
    check_funscripts.run(cfg['destination'], do_rename=fields.get('auto_rename', False))


def _run_dedupe(cfg: dict, fields: dict) -> None:
    import downloadContent
    downloadContent._dedup_existing(cfg['destination'])


def _run_generate_html(cfg: dict, fields: dict) -> None:
    import generate_html
    generate_html.generate(cfg['destination'], dry_run=False)


def _run_generate_audit_report(cfg: dict, fields: dict) -> None:
    import generate_audit_report
    generate_audit_report.generate(cfg['destination'])


_STEP_LABELS = {
    'sync_new_folders': 'Sync new folders',
    'extract_archives': 'Extract variant archives',
    'download': 'Download content',
    'download_from_funscript_metadata': 'Download from funscript metadata',
    'prefix_fix': 'Fix file prefixes',
    'fix_garbled_names': 'Fix garbled names',
    'check_funscripts': 'Check funscript match',
    'dedupe': 'Dedupe (+ consolidate packs)',
    'generate_html': 'Generate HTML',
    'generate_audit_report': 'Audit report',
}

_STEP_HANDLERS = {
    'sync_new_folders': _run_sync_new_folders,
    'extract_archives': _run_extract_archives,
    'download': _run_download,
    'download_from_funscript_metadata': _run_download_from_funscript_metadata,
    'prefix_fix': _run_prefix_fix,
    'fix_garbled_names': _run_fix_garbled_names,
    'check_funscripts': _run_check_funscripts,
    'dedupe': _run_dedupe,
    'generate_html': _run_generate_html,
    'generate_audit_report': _run_generate_audit_report,
}


def run_unattended(creator_folder: str) -> None:
    creator_folder = os.path.abspath(creator_folder)
    creator_key = os.path.basename(creator_folder).strip().lower()

    cfg = creator_profiles.get_unattended_config(creator_key)
    if not cfg:
        print(f'No unattended config found for "{creator_key}" — '
              f"run menu option 'sa' (Set up unattended run) for this folder first.")
        return

    reports_dir = os.path.join(cfg['destination'], '_reports')
    os.makedirs(reports_dir, exist_ok=True)
    log_path = os.path.join(reports_dir, f'unattended_{creator_key}_{time.strftime("%Y%m%d_%H%M%S")}.log')

    real_stdout = sys.stdout
    log_file = open(log_path, 'w', encoding='utf-8')
    sys.stdout = _Tee(real_stdout, log_file)
    try:
        print()
        print("========================================")
        print(f"  Run Unattended — {creator_key}")
        print("========================================")
        print(f"Source:      {cfg['source']}")
        print(f"Destination: {cfg['destination']}")
        print(f"Log file:    {log_path}")
        print()

        steps = cfg.get('steps', [])
        ok = 0
        failed: list[str] = []
        for i, step in enumerate(steps, 1):
            step_id = step['id']
            label = _STEP_LABELS.get(step_id, step_id)
            print(f"\n[{i}/{len(steps)}] === {label} ===")
            handler = _STEP_HANDLERS.get(step_id)
            if handler is None:
                print(f"  SKIP — unknown step id: {step_id}")
                failed.append(f'{label} (unknown step id)')
                continue
            try:
                handler(cfg, step)
                ok += 1
            except Exception as e:
                print(f"  ERROR — {label} failed: {e}")
                traceback.print_exc(file=sys.stdout)
                failed.append(label)
                print(f"  Continuing to the next step...")

        print()
        print("========================================")
        print(f"  Done — {ok}/{len(steps)} step(s) completed")
        if failed:
            print(f"  Failed: {', '.join(failed)}")
        print(f"  Log: {log_path}")
        print("========================================")
    finally:
        sys.stdout = real_stdout
        log_file.close()


def main() -> None:
    args = sys.argv[1:]
    if args:
        creator_folder = args[0]
    else:
        print()
        print("========================================")
        print("  Run Unattended")
        print("========================================")
        print()
        configured = [
            key for key, profile in creator_profiles.load().items()
            if profile.get('unattended')
        ]
        if configured:
            print("Configured creators:", ', '.join(sorted(configured)))
        else:
            print("No creators have an unattended config yet — set one up with 'sa' first.")
        print()
        creator_folder = input("Creator folder (the destination path used during setup): ").strip().strip('"\'')

    if not creator_folder:
        print("No folder given.")
        return

    run_unattended(creator_folder)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n\nCancelled.')
