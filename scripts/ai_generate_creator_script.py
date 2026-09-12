"""Advanced/optional: generate a new creator_scripts/ plugin with an LLM, from
a plain-English description of a creator's filename quirk.

This is deliberately the *harder* path, not the recommended one — writing your
own plugin by hand (see scripts/creator_scripts/README.md) is free, has no
external dependency, and you already understand every line of it because you
wrote it. This exists for when you don't want to write it yourself and are
willing to accept the tradeoffs. Two providers, same tradeoffs on the trust
side either way:

  - Anthropic API (claude-opus-5) — best quality, costs real money per
    generation (typically a few cents for a script this size; the actual
    cost is printed after every call, from response.usage).
  - A local model via Ollama (scripts/local_llm.py) — free (no per-call
    cost), runs entirely on your own GPU, auto-picks the strongest coding
    model your detected VRAM can actually run. Quality depends on your
    hardware and is generally weaker than the API at following the strict
    contract below, especially on smaller local models — review the draft
    even more carefully than you would an API-generated one.
  - Either way: the generated code gets the same filesystem access any
    other script here has (rename/move/soft-delete via action_log).
    Nothing here has a review or approval step *inside* the model — the
    review step is you.

To keep that review real: a generated script is written to
scripts/creator_scripts/_drafts/ and NEVER auto-run or auto-installed. It has
no effect on the menu until you read it and move it into
scripts/creator_scripts/ yourself. Treat it exactly like code from a stranger
on the internet — because functionally, that's what it is.

The Anthropic path requires the `anthropic` package (not a core dependency of
this project — installed on first use only, at your confirmation) and an API
key (prompted for once, stored in the OS keyring). The local path requires
Ollama (scripts/local_llm.py offers to install it) and a GPU with enough
VRAM — see that file for the model ladder.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_KEYRING_SERVICE = 'patreon-funscript-video-downloader'  # same service as downloadContent.py / setup_config.py
_DRAFTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'creator_scripts', '_drafts')
_MODEL = 'claude-opus-5'
# Published per-MTok rates for claude-opus-5 (see Anthropic's pricing page) — used only to
# print a cost estimate after each call. Hardcoded, so it can drift from reality; if a
# generation's printed cost looks wrong, check platform.claude.com/docs/en/pricing.
_INPUT_PRICE_PER_MTOK = 5.00
_OUTPUT_PRICE_PER_MTOK = 25.00

_WARNING = """
  ══════════════════════════════════════════════════════════════════
  ADVANCED / OPTIONAL — read before continuing
  ══════════════════════════════════════════════════════════════════
  This uses an LLM (your choice: paid Anthropic API, or a free local
  model on your own GPU) to write a new creator_scripts/ plugin for
  you. One thing is true either way — TRUST: the generated code gets
  the same filesystem access any other script here has (rename/move/
  soft-delete via action_log). It is written to a _drafts/ folder and
  is NEVER run or wired into the menu automatically. You must read it
  and move it into scripts/creator_scripts/ yourself before it does
  anything — a local model especially, since it's weaker at following
  the contract exactly than the API is.

  The safer, free-and-no-dependencies default is writing the plugin
  by hand — see scripts/creator_scripts/README.md for the contract;
  it's short. Use this only when you'd rather not write it yourself.
  ══════════════════════════════════════════════════════════════════
"""


def _get_api_key() -> str | None:
    try:
        import keyring
    except ImportError:
        keyring = None

    if keyring is not None:
        try:
            key = keyring.get_password(_KEYRING_SERVICE, 'ANTHROPIC_API_KEY')
            if key:
                return key
        except Exception:
            pass

    key = os.getenv('ANTHROPIC_API_KEY', '').strip()
    if key:
        return key

    print('\n  No Anthropic API key found (checked OS keyring and ANTHROPIC_API_KEY env var).')
    entered = input('  Paste an Anthropic API key to use now (leave blank to cancel): ').strip()
    if not entered:
        return None
    if keyring is not None:
        save = input('  Save this key to the OS keyring for next time? (y/n): ').strip().lower()
        if save == 'y':
            try:
                keyring.set_password(_KEYRING_SERVICE, 'ANTHROPIC_API_KEY', entered)
                print('  Saved.')
            except Exception as e:
                print(f'  Could not save to keyring ({e}) — you\'ll be asked again next time.')
    return entered


def _ensure_anthropic_sdk():
    try:
        import anthropic
        return anthropic
    except ImportError:
        pass
    print("\n  The 'anthropic' package isn't installed (it's optional — only this advanced")
    print("  feature needs it, so it's not part of the project's normal dependencies).")
    answer = input('  Install it now with pip? (y/n): ').strip().lower()
    if answer != 'y':
        return None
    venv_python = sys.executable
    try:
        subprocess.run([venv_python, '-m', 'pip', 'install', '--quiet', 'anthropic'], check=True)
    except subprocess.CalledProcessError as e:
        print(f'  pip install failed: {e}')
        return None
    import anthropic
    return anthropic


_SYSTEM_PROMPT_TEMPLATE = """\
You write exactly one Python file for a personal media-organizing tool. The file is a \
plugin implementing this exact contract (see the real example below):

    MENU_LABEL = "Short name shown in the menu"
    MENU_DESCRIPTION = "One or two lines of detail shown under the label"

    def run(base_path: str) -> None:
        ...

`run()` is called with a directory the user picked. Inside it you may walk the \
directory tree, inspect filenames, and rename/move files as needed to fix the \
naming quirk described by the user below.

Hard rules, no exceptions:
- Only ever touch files under `base_path`. Never delete a file outright — if a script \
  ever needs to remove one, only ever move it via `action_log.soft_delete(base_path, path)` \
  (which quarantines it into a recoverable .trash folder), never `os.remove`.
- Every rename must go through `os.rename` AND be recorded immediately after via \
  `action_log.record('rename', old_path=old_path, new_path=new_path)`, so it's covered \
  by this project's existing undo feature. Wrap the whole run in \
  `action_log.start('<script_name>', base_path)` ... `action_log.finish()`.
- Never overwrite an existing file — skip (with a printed reason) if the target name \
  already exists.
- If you walk `base_path` with `os.walk`, prune `action_log.TRASH_DIRNAME` out of the \
  yielded dirnames list before descending further, so the walk never enters `.trash` — \
  soft-deleted files are quarantined there and must not be touched by anything except \
  `action_log`'s own trash/undo machinery.
- Print one line per action taken (or skipped, with why), plus a final summary count.
- No network access, no subprocess calls, no imports beyond Python's standard library \
  plus `action_log` (already on the path — `import action_log`) and `os`/`re`/`pathlib` \
  as needed.
- Output ONLY the Python file's contents. No markdown fences, no commentary before or \
  after — the response body IS the file.

Reference example of a real, working plugin in this exact style (for you to match the \
tone/structure of, not to copy verbatim — the new script solves a different problem):

```python
{reference_example}
```
"""


def _extract_code(text: str) -> str:
    """Strip a leading/trailing markdown fence if the model added one despite instructions."""
    text = text.strip()
    fence = re.match(r'^```(?:python)?\s*\n(.*)\n```\s*$', text, re.DOTALL)
    return fence.group(1) if fence else text


def _slugify(label: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '_', label.lower()).strip('_')
    return slug or 'generated'


def run() -> None:
    print(_WARNING)
    if input('  Continue? (y/n): ').strip().lower() != 'y':
        print('  Cancelled.')
        return

    print('\n  1) Anthropic API (claude-opus-5) — best quality, costs a few cents per generation')
    print('  2) Local model via Ollama — free, runs on your own GPU, auto-picks a model to fit your VRAM')
    use_local = input('  Choose (1/2): ').strip() == '2'

    if use_local:
        import local_llm
    else:
        anthropic = _ensure_anthropic_sdk()
        if anthropic is None:
            print('  Cancelled — anthropic package not available.')
            return
        api_key = _get_api_key()
        if not api_key:
            print('  Cancelled — no API key provided.')
            return

    creator = input('\n  Creator name (for the script label, e.g. "Pize"): ').strip()
    if not creator:
        print('  Cancelled — no creator name given.')
        return

    print('  Describe the naming quirk this creator uses, and give a few real')
    print('  before -> after filename examples (paste, then an empty line to finish):')
    lines = []
    while True:
        line = input('  ' if lines else '  > ')
        if not line.strip() and lines:
            break
        lines.append(line)
    description = '\n'.join(lines).strip()
    if not description:
        print('  Cancelled — no description given.')
        return

    reference_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'creator_scripts', 'mdemaxis_smooth_fix.py'
    )
    try:
        with open(reference_path, 'r', encoding='utf-8') as f:
            reference_example = f.read()
    except OSError:
        reference_example = '# (reference example unavailable — proceeding without it)'

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(reference_example=reference_example)
    user_prompt = f'Creator: {creator}\n\nNaming quirk and examples:\n{description}'

    if use_local:
        tag = local_llm.get_or_setup_model()
        if tag is None:
            print('  Cancelled — no usable local model set up.')
            return
        print(f'\n  Generating with {tag} (local)...')
        text = local_llm.generate(tag, system_prompt, user_prompt)
        if not text or not text.strip():
            print('  Local model returned no text — nothing written.')
            return
        print(f'  Done — free (ran locally on {tag}).')
    else:
        client = anthropic.Anthropic(api_key=api_key)
        print('\n  Calling Claude (claude-opus-5)...')
        try:
            response = client.messages.create(
                model=_MODEL,
                max_tokens=8000,
                system=system_prompt,
                messages=[{'role': 'user', 'content': user_prompt}],
            )
        except Exception as e:
            print(f'  API call failed: {e}')
            return

        text = next((b.text for b in response.content if b.type == 'text'), '')
        if not text.strip():
            print('  Claude returned no text — nothing written.')
            return

        in_tok = response.usage.input_tokens
        out_tok = response.usage.output_tokens
        cost = (in_tok / 1_000_000) * _INPUT_PRICE_PER_MTOK + (out_tok / 1_000_000) * _OUTPUT_PRICE_PER_MTOK
        print(f'  Done — {in_tok} input / {out_tok} output tokens, ~${cost:.4f}')

    code = _extract_code(text)

    os.makedirs(_DRAFTS_DIR, exist_ok=True)
    slug = _slugify(creator)
    dest = os.path.join(_DRAFTS_DIR, f'{slug}_generated.py')
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(_DRAFTS_DIR, f'{slug}_generated_{n}.py')
        n += 1
    with open(dest, 'w', encoding='utf-8') as f:
        f.write(code if code.endswith('\n') else code + '\n')

    print(f'\n  Draft written to: {dest}')
    print('  This is NOT active — it will not appear in any menu and nothing here has')
    print('  run it. Read it carefully (it was not reviewed by anyone but the model),')
    print('  then move it into scripts/creator_scripts/ yourself once you\'re satisfied.')


if __name__ == '__main__':
    try:
        run()
    except KeyboardInterrupt:
        print('\n\nCancelled.')
