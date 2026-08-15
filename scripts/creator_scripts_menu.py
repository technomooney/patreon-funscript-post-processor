"""Submenu for creator-specific scripts — one-creator naming quirks (e.g.
MDemaxis's SMOOTH-prefix convention) live here, separate from the main
menu's general-purpose numbered options. Anyone can drop their own script
into scripts/creator_scripts/ and it shows up automatically; no core file
needs editing. See scripts/creator_scripts/README.md for the contract.

Usage: python scripts/creator_scripts_menu.py
"""
import importlib.util
import os
import sys

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.join(_SCRIPTS_DIR, 'creator_scripts')

# Plugin modules import sibling helpers (action_log, etc.) the same way every
# other script in this project does — they need scripts/ itself on the path,
# not just creator_scripts/.
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


def discover() -> list[tuple[str, object]]:
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
        path = os.path.join(_PLUGIN_DIR, fname)
        modname = f'creator_scripts.{fname[:-3]}'
        try:
            spec = importlib.util.spec_from_file_location(modname, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:
            print(f'  [creator_scripts] skipping {fname}: failed to load ({e})')
            continue

        label = getattr(module, 'MENU_LABEL', None)
        run_fn = getattr(module, 'run', None)
        if not label or not callable(run_fn):
            print(f'  [creator_scripts] skipping {fname}: missing MENU_LABEL or run(base_path)')
            continue
        found.append((label, module))

    return found


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
        return

    for i, (label, module) in enumerate(plugins, start=1):
        print(f'  {i}) {label}')
        desc = getattr(module, 'MENU_DESCRIPTION', '')
        for line in desc.strip().splitlines():
            print(f'     {line}')
        print()
    print('  g) Generate a new one with AI (advanced — costs money, writes a draft')
    print('     only, never runs automatically; see the warning before it starts)')
    print()
    print('  q) Back to main menu')
    print()

    while True:
        choice = input(f'Choose a script to run (1-{len(plugins)}, g=AI-generate, q=back): ').strip()
        if choice.lower() in ('q', ''):
            return
        if choice.lower() == 'g':
            import ai_generate_creator_script
            ai_generate_creator_script.run()
            return
        if choice.isdigit() and 1 <= int(choice) <= len(plugins):
            break
        print(f'Invalid choice. Please enter 1-{len(plugins)}, g, or q.')

    label, module = plugins[int(choice) - 1]
    entered = input('Enter full directory path to process: ').strip()
    base_path = os.path.abspath(entered)
    if not os.path.isdir(base_path):
        print(f'Directory not found: {base_path}')
        return

    print(f'\nRunning: {label}\n')
    module.run(base_path)


if __name__ == '__main__':
    main()
