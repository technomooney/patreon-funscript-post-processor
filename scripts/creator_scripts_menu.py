"""Submenu for creator-specific scripts — one-creator naming quirks (e.g.
MDemaxis's SMOOTH-prefix convention) live here, separate from the main
menu's general-purpose numbered options. Anyone can drop their own script
into scripts/creator_scripts/ and it shows up automatically; no core file
needs editing. See scripts/creator_scripts/README.md for the contract.

Also home to:
  u) undo a creator script's last run -- each creator script keeps its own
     undo journal per folder (action_log's named journals), separate from
     the main menu's "Undo last action" for the core tools.
  c) configure a creator folder's creator_config.json flags (see
     creator_config.py) -- the only interactive way besides setup_unattended
     ('sa') those flags ever get set.

Usage: python scripts/creator_scripts_menu.py
"""
import importlib.util
import inspect
import os
import sys

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.join(_SCRIPTS_DIR, 'creator_scripts')

# Plugin modules import sibling helpers (action_log, etc.) the same way every
# other script in this project does — they need scripts/ itself on the path,
# not just creator_scripts/.
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

_loaded: dict[str, object] = {}


def _import_plugin(fname: str):
    path = os.path.join(_PLUGIN_DIR, fname)
    modname = f'creator_scripts.{fname[:-3]}'
    if modname in _loaded:
        return _loaded[modname]
    spec = importlib.util.spec_from_file_location(modname, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _loaded[modname] = module
    return module


def script_id(module) -> str:
    """A plugin's SCRIPT_ID, defaulting to its file stem -- used as its undo
    journal name and its folder_log script name."""
    sid = getattr(module, 'SCRIPT_ID', None)
    if sid:
        return sid
    return os.path.splitext(os.path.basename(module.__file__))[0]


def discover(quiet: bool = False) -> list[tuple[str, object]]:
    """Return [(MENU_LABEL, module), ...] for every valid plugin in
    creator_scripts/, in filename order. A file that fails to import, or
    imports but is missing MENU_LABEL/run(), is skipped with a warning —
    one broken plugin can't take the menu down for anything else."""
    found: list[tuple[str, object]] = []
    if not os.path.isdir(_PLUGIN_DIR):
        return found

    for fname in sorted(os.listdir(_PLUGIN_DIR)):
        if not fname.endswith('.py') or fname.startswith('_'):
            continue
        try:
            module = _import_plugin(fname)
        except Exception as e:
            if not quiet:
                print(f'  [creator_scripts] skipping {fname}: failed to load ({e})')
            continue

        label = getattr(module, 'MENU_LABEL', None)
        run_fn = getattr(module, 'run', None)
        if not label or not callable(run_fn):
            if not quiet:
                print(f'  [creator_scripts] skipping {fname}: missing MENU_LABEL or run(base_path)')
            continue
        found.append((label, module))

    return found


def load(sid: str):
    """The plugin module whose script_id() is *sid*, or None."""
    for _label, module in discover(quiet=True):
        if script_id(module) == sid:
            return module
    return None


def download_replacements() -> list[tuple[str, object]]:
    """Plugins declaring REPLACES_DOWNLOAD = True -- the only ones eligible
    for a creator_config.json download_script."""
    return [(label, m) for label, m in discover(quiet=True) if getattr(m, 'REPLACES_DOWNLOAD', False)]


def run_plugin(module, base_path: str, options: dict | None = None) -> None:
    """Call *module*.run -- interactively (options None) or unattended with
    *options*. Plugins written before the options parameter existed take
    only base_path; they're always run interactively-shaped."""
    if options is None:
        module.run(base_path)
        return
    params = inspect.signature(module.run).parameters
    if len(params) >= 2:
        module.run(base_path, options)
    else:
        module.run(base_path)


# ---------------------------------------------------------------------------
# c) Configure creator flags
# ---------------------------------------------------------------------------

def _ask_list(label: str, current: list) -> list:
    shown = ', '.join(current) if current else 'none'
    raw = input(f'  {label} [{shown}] (comma-separated, "-" to clear, Enter keeps): ').strip()
    if not raw:
        return list(current)
    if raw == '-':
        return []
    return [p.strip() for p in raw.split(',') if p.strip()]


def configure_flags(creator_folder: str) -> None:
    """Interactively edit *creator_folder*/creator_config.json. Every answer
    defaults to what's already there (Enter keeps); nothing is written
    unless something actually changed."""
    import creator_config

    raw = creator_config.load_raw(creator_folder)
    cfg = dict(raw)
    print(f'\nCreator flags for: {creator_folder}')
    print(f'  ({creator_config.FILENAME} — explicit opt-in; unset = normal behavior)\n')

    candidates = download_replacements()
    current = cfg.get('download_script') or None
    print('  Download-replacement script (runs INSTEAD of the normal download for this creator):')
    print('    0) none — normal download')
    for i, (label, m) in enumerate(candidates, 1):
        mark = '  <- current' if script_id(m) == current else ''
        print(f'    {i}) {label} [{script_id(m)}]{mark}')
    default = next((str(i) for i, (_l, m) in enumerate(candidates, 1) if script_id(m) == current), '0')
    choice = input(f'  Choose [{default}]: ').strip() or default
    chosen = None
    if choice.isdigit() and 1 <= int(choice) <= len(candidates):
        chosen = candidates[int(choice) - 1][1]
    if chosen is None:
        cfg.pop('download_script', None)
    else:
        cfg['download_script'] = script_id(chosen)
        recommended = getattr(chosen, 'RECOMMENDED_CONFIG', None) or {}
        if recommended:
            print(f'\n  {script_id(chosen)} recommends:')
            for k, v in recommended.items():
                print(f'    {k}: {v}')
            if input('  Apply these? (y/n, default n): ').strip().lower() == 'y':
                cfg.update(recommended)

    print()
    cfg['sync_exclude'] = _ask_list(
        'sync_exclude — kinds (video, funscript, archive) or .ext sync must NOT copy',
        cfg.get('sync_exclude') or [])
    cfg['protected_dirs'] = _ask_list(
        'protected_dirs — subfolders core tools must never touch',
        cfg.get('protected_dirs') or [])
    if cfg.get('download_script'):
        cur = bool(cfg.get('force_normal_download'))
        ans = input(f'  force_normal_download — unattended runs use the normal download anyway? '
                    f'(y/n) [{"y" if cur else "n"}]: ').strip().lower()
        if ans in ('y', 'n'):
            cfg['force_normal_download'] = ans == 'y'

    for key in ('sync_exclude', 'protected_dirs'):
        if not cfg.get(key):
            cfg.pop(key, None)
    if not cfg.get('force_normal_download'):
        cfg.pop('force_normal_download', None)

    if cfg == raw:
        print('\nNo changes.')
        return
    creator_config.save(creator_folder, cfg)
    print(f'\nSaved {os.path.join(creator_folder, creator_config.FILENAME)}')


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

def _ask_dir(prompt: str) -> str | None:
    entered = input(prompt).strip().strip('"\'')
    base_path = os.path.abspath(entered)
    if not entered or not os.path.isdir(base_path):
        print(f'Directory not found: {base_path}')
        return None
    return base_path


def _undo_menu(plugins: list[tuple[str, object]]) -> None:
    for i, (label, module) in enumerate(plugins, start=1):
        print(f'  {i}) {label} [{script_id(module)}]')
    choice = input(f'Undo which script\'s last run? (1-{len(plugins)}): ').strip()
    if not (choice.isdigit() and 1 <= int(choice) <= len(plugins)):
        print('Cancelled.')
        return
    module = plugins[int(choice) - 1][1]
    base_path = _ask_dir('Folder it ran against: ')
    if base_path is None:
        return
    import undo_last_action
    undo_last_action.main(base_path, journal=script_id(module))


def main() -> None:
    plugins = discover()

    print()
    print('========================================')
    print('  Creator-specific scripts')
    print('========================================')
    print()
    if not plugins:
        print('  No creator scripts found in scripts/creator_scripts/.')
        print('  See scripts/creator_scripts/README.md to add your own.')
        print()

    for i, (label, module) in enumerate(plugins, start=1):
        print(f'  {i}) {label}')
        desc = getattr(module, 'MENU_DESCRIPTION', '')
        for line in desc.strip().splitlines():
            print(f'     {line}')
        print()
    if plugins:
        print("  u) Undo a creator script's last run (each script has its own undo)")
    print('  c) Configure creator flags (creator_config.json: download-replacement')
    print('     script, sync exclusions, protected folders)')
    print('  g) Generate a new one with AI (advanced — costs money, writes a draft')
    print('     only, never runs automatically; see the warning before it starts)')
    print()
    print('  q) Back to main menu')
    print()

    while True:
        choice = input(f'Choose (1-{len(plugins)}, u=undo, c=configure, g=AI-generate, q=back): ').strip().lower()
        if choice in ('q', ''):
            return
        if choice == 'g':
            import ai_generate_creator_script
            ai_generate_creator_script.run()
            return
        if choice == 'u' and plugins:
            _undo_menu(plugins)
            return
        if choice == 'c':
            folder = _ask_dir('Creator folder to configure: ')
            if folder:
                configure_flags(folder)
            return
        if choice.isdigit() and 1 <= int(choice) <= len(plugins):
            break
        print('Invalid choice.')

    label, module = plugins[int(choice) - 1]
    base_path = _ask_dir('Enter full directory path to process: ')
    if base_path is None:
        return

    print(f'\nRunning: {label}\n')
    run_plugin(module, base_path)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n\nCancelled.')
