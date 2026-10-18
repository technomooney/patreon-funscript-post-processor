"""Menu of creator-agnostic tools (the 't' option): generic utilities that work on any creator's folder, as
opposed to the creator-specific scripts ('n'). A tool is a creator_scripts plugin that declares GENERIC_TOOL = True,
so it keeps the same contract, undo journal and unattended-step support; it just is not listed under 'n'.

Usage: python scripts/tools_menu.py
"""
import os

import creator_scripts_menu as csm


def main() -> None:
    tools = [(l, m) for l, m in csm.discover() if csm.is_generic(m)]
    print()
    print('========================================')
    print('  Tools (work on any creator)')
    print('========================================')
    print()
    if not tools:
        print('  No tools found.')
        return
    for i, (label, module) in enumerate(tools, start=1):
        print(f'  {i}) {label}')
        for line in (getattr(module, 'MENU_DESCRIPTION', '') or '').strip().splitlines():
            print(f'     {line}')
        print()
    print('  u) Undo a tool\'s last run')
    print('  q) Back to main menu')
    print()
    while True:
        choice = input(f'Choose (1-{len(tools)}, u=undo, q=back): ').strip().lower()
        if choice in ('q', ''):
            return
        if choice == 'u':
            csm._undo_menu(tools)
            return
        if choice.isdigit() and 1 <= int(choice) <= len(tools):
            break
        print('Invalid choice.')
    label, module = tools[int(choice) - 1]
    base_path = csm._ask_dir('Enter full directory path to process: ')
    if base_path is None:
        return
    print(f'\nRunning: {label}\n')
    csm.run_plugin(module, base_path)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n\nCancelled.')
