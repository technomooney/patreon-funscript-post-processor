"""Detect local GPU VRAM and set up a free, local coding model via Ollama, as
an alternative to the paid Anthropic API in ai_generate_creator_script.py.

Free here means "no per-call API cost" — it still needs real hardware (a GPU
with enough VRAM) and is meaningfully weaker at following instructions than
claude-opus-5, especially at the smaller end of the ladder below. Treat a
locally-generated draft with at least as much scrutiny as an API-generated
one, not less.

Model picks current as of 2026-08 (Qwen2.5-Coder / Qwen3-Coder via Ollama's
library) — re-check https://ollama.com/library if this file is more than a
few months old, since the "best local coding model" list moves fast.

Standalone usage (detect hardware, install Ollama if needed, pull the
recommended model, run a one-line test prompt):
    python scripts/local_llm.py
"""
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

_OLLAMA_URL = 'http://localhost:11434'
_VRAM_HEADROOM_GB = 1.0  # leave this much VRAM for the OS/display/other apps when sizing a model
_MIN_USABLE_VRAM_GB = 4

# (min usable VRAM in GB, ollama tag, approx download size, why this one)
# Ordered ascending — recommend_model() picks the highest tier the detected VRAM clears.
_MODEL_LADDER = [
    (4,  'qwen2.5-coder:3b-instruct',  '~2GB',  'small and fast; fine for short, well-specified scripts'),
    (6,  'qwen2.5-coder:7b-instruct',  '~5GB',  'the practical sweet spot for most machines'),
    (9,  'qwen2.5-coder:14b-instruct', '~9GB',  'noticeably better instruction-following than 7b'),
    (16, 'qwen3-coder:30b',            '~19GB', 'mixture-of-experts (only ~3.3B active params — runs '
                                                 'faster than the download size implies); the strongest '
                                                 'local option for this task as of mid-2026'),
]


def _run(cmd: list[str], timeout: int = 15) -> str | None:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def detect_gpu() -> dict | None:
    """Best-effort GPU + total VRAM detection. Returns {'vendor', 'name', 'vram_gb'} or None
    if nothing could be detected (no supported vendor tool found, or no discrete GPU)."""
    out = _run(['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader,nounits'])
    if out:
        # Multiple GPUs each print their own line — use the first (largest VRAM isn't guaranteed
        # to be first, but picking the first is simple and matches what Ollama defaults to using).
        line = out.splitlines()[0]
        name, mem_mb = [p.strip() for p in line.rsplit(',', 1)]
        try:
            return {'vendor': 'nvidia', 'name': name, 'vram_gb': round(int(mem_mb) / 1024, 1)}
        except ValueError:
            pass

    # AMD via ROCm — untested (no AMD GPU available to verify against); best-effort only,
    # wrapped so a parsing miss just falls through to manual entry instead of crashing.
    out = _run(['rocm-smi', '--showmeminfo', 'vram'])
    if out:
        try:
            import re
            m = re.search(r'VRAM Total Memory \(B\):\s*(\d+)', out)
            if m:
                name = _run(['rocm-smi', '--showproductname']) or 'AMD GPU'
                return {'vendor': 'amd', 'name': name.splitlines()[-1].strip(),
                        'vram_gb': round(int(m.group(1)) / (1024 ** 3), 1)}
        except Exception:
            pass

    return None


def recommend_model(usable_vram_gb: float) -> tuple[str, str, str] | None:
    """Highest-tier model the given usable VRAM (already headroom-adjusted) clears, or None
    if even the smallest tier doesn't fit."""
    best = None
    for min_gb, tag, size, note in _MODEL_LADDER:
        if usable_vram_gb >= min_gb:
            best = (tag, size, note)
    return best


def ollama_installed() -> bool:
    return shutil.which('ollama') is not None


def offer_install_ollama() -> bool:
    """Returns True once `ollama` is on PATH (already was, or the user installed it now)."""
    if ollama_installed():
        return True

    print("\n  Ollama (the local model runtime) isn't installed.")
    if sys.platform == 'win32':
        print('  Download and run the installer from https://ollama.com/download/windows')
        input("  Press Enter once it's installed (you may need to open a new terminal for PATH to update)...")
    elif sys.platform == 'darwin':
        print('  Download and run the installer from https://ollama.com/download/mac')
        input("  Press Enter once it's installed...")
    else:
        answer = input('  Install it now via the official install script (curl | sh)? (y/n): ').strip().lower()
        if answer == 'y':
            subprocess.run(['sh', '-c', 'curl -fsSL https://ollama.com/install.sh | sh'])
        else:
            print('  Install manually from https://ollama.com/download, then re-run this.')
            return False

    if not ollama_installed():
        print('  Still not finding `ollama` on PATH — installation may not have finished. Cancelling.')
        return False
    return True


def ollama_running() -> bool:
    try:
        urllib.request.urlopen(f'{_OLLAMA_URL}/api/version', timeout=3)
        return True
    except (urllib.error.URLError, OSError):
        return False


def start_ollama_server() -> bool:
    """Ensure a local Ollama server is reachable, starting one if needed. On Windows/macOS the
    installer typically registers it as an always-running background service already, so this
    is mainly load-bearing on Linux."""
    if ollama_running():
        return True
    try:
        subprocess.Popen(
            ['ollama', 'serve'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=(sys.platform != 'win32'),
        )
    except OSError:
        return False
    for _ in range(20):
        if ollama_running():
            return True
        time.sleep(0.5)
    return False


def model_pulled(tag: str) -> bool:
    listed = _run(['ollama', 'list']) or ''
    return tag.split(':')[0] in listed


def pull_model(tag: str) -> bool:
    print(f'  Pulling {tag} (first run only — this can take a while)...')
    result = subprocess.run(['ollama', 'pull', tag])
    return result.returncode == 0


def get_or_setup_model() -> str | None:
    """Detect hardware, recommend a model, install Ollama and pull the model if needed.
    Returns the ready-to-use Ollama tag, or None if hardware is insufficient or the user
    declined a required step along the way."""
    gpu = detect_gpu()
    if gpu:
        print(f"  Detected GPU: {gpu['name']} ({gpu['vram_gb']} GB VRAM)")
        vram = gpu['vram_gb']
    else:
        print('  Could not auto-detect a GPU (no nvidia-smi/rocm-smi found, or none present).')
        raw = input('  Enter your GPU VRAM in GB if you have one (blank = skip / CPU only): ').strip()
        try:
            vram = float(raw) if raw else 0.0
        except ValueError:
            vram = 0.0

    usable = max(0.0, vram - _VRAM_HEADROOM_GB)
    pick = recommend_model(usable)
    if pick is None:
        print(f'  {vram:.1f} GB VRAM (~{usable:.1f} GB usable after headroom) isn\'t enough for a local')
        print(f'  coding model worth using here ({_MIN_USABLE_VRAM_GB}+ GB recommended) — the Anthropic')
        print('  API option, or writing the plugin by hand, will do much better on hardware this size.')
        return None

    tag, size, note = pick
    print(f'  Recommended model: {tag} ({size} download) — {note}')

    if not offer_install_ollama():
        return None
    if not start_ollama_server():
        print('  Could not reach a running Ollama server (tried starting one — check `ollama serve` by hand).')
        return None
    if not model_pulled(tag):
        if not pull_model(tag):
            print('  Model pull failed.')
            return None
    return tag


def generate(tag: str, system_prompt: str, user_prompt: str, timeout: int = 300) -> str | None:
    """One-shot chat completion against a local Ollama model. Returns the reply text, or None
    on any failure (server unreachable, timeout, malformed response)."""
    payload = json.dumps({
        'model': tag,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        'stream': False,
    }).encode('utf-8')
    req = urllib.request.Request(
        f'{_OLLAMA_URL}/api/chat', data=payload, headers={'Content-Type': 'application/json'}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return data.get('message', {}).get('content')
    except Exception as e:
        print(f'  Local generation failed: {e}')
        return None


if __name__ == '__main__':
    print('Local LLM setup check\n')
    chosen = get_or_setup_model()
    if not chosen:
        sys.exit(1)
    print(f'\nReady: {chosen}')
    print('Sending a one-line test prompt...')
    reply = generate(chosen, 'You are a helpful assistant.', 'Reply with exactly: OK')
    print(f'Response: {reply!r}')
