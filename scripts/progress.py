"""Small progress helpers shared by the long-running tools: a live one-line status that overwrites itself on a
terminal (and stays silent when output is piped to a file), stage headers, and size formatting. Standard library only."""
import shutil
import sys
import time

_TTY = sys.stdout.isatty()
_active = False
_last = 0.0


def live(message: str, min_interval: float = 0.15) -> None:
    """Show *message* on the status line, replacing the previous one. Throttled so a tight loop stays cheap."""
    global _active, _last
    if not _TTY:
        return
    now = time.monotonic()
    if now - _last < min_interval:
        return
    _last = now
    cols = shutil.get_terminal_size(fallback=(120, 24)).columns
    sys.stdout.write('\r' + message[:cols - 1].ljust(cols - 1) + '\r')
    sys.stdout.flush()
    _active = True


def clear() -> None:
    """Erase the status line so the next normal print starts clean."""
    global _active
    if _TTY and _active:
        cols = shutil.get_terminal_size(fallback=(120, 24)).columns
        sys.stdout.write('\r' + ' ' * (cols - 1) + '\r')
        sys.stdout.flush()
    _active = False


def say(message: str = '') -> None:
    """A normal line, printed after clearing any live status."""
    clear()
    print(message)


def stage(number: int, total: int, title: str) -> None:
    say(f'\n[{number}/{total}] {title}')


def size(n) -> str:
    if n is None:
        return '? size'
    n = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
