# Patreon Funscript Post-Processor

A toolkit for managing downloaded Patreon content — renaming files, downloading linked media, matching funscripts to videos, and keeping your library clean.

## Prerequisites — initial Patreon download

This tool **post-processes** content already downloaded from Patreon. The initial download must be done with [PatreonDownloader](https://github.com/AlexCSDev/PatreonDownloader).

Run it with these flags so the post-processor has everything it needs:

```
PatreonDownloader --json --embeds --descriptions --use-sub-directories --url <creator_url>
```

| Flag | Why it's needed |
|------|----------------|
| `--json` | Saves post metadata — the post-processor reads this to find linked media |
| `--embeds` | Saves embedded content references |
| `--descriptions` | Saves `description.json` per post — this is what the download script scans |
| `--use-sub-directories` | Creates one folder per post — required for the folder-based workflow |
| `--url` | The Patreon creator URL to download from |

## Features

- **Download linked media** from description.json files via Selenium (supports Patreon-hosted content, iwara.tv, mega.nz, pixeldrain, spankbang, hanime, gofile, rule34, e621, eporner, yt-dlp fallback, and more)
- **Fix garbled filenames** — percent-encoded URLs, mojibake (Latin-1/cp1252 mis-decoded UTF-8), and CJK truncation
- **Fix wrong/missing extensions** — detect video files and funscripts by content (magic bytes / JSON structure) and rename them correctly
- **Fuzzy funscript-to-video matching** — rename funscripts to match their video, handling axis-variant prefixes (HARD, SUCK, SIDE, etc.) and label suffixes (SMOOTH, max interval, etc.)
- **Deduplicate files** — hash-based exact duplicate removal with parallel I/O
- **Check funscript coverage** — report videos missing a matching funscript with fuzzy-match suggestions
- **Generate HTML overviews** — build a `description.html` visual summary in each post folder
- **Sync new folders** — copy newly downloaded Patreon folders into your working directory
- **Fix attachment ID prefixes** — strip numeric prefix added by the Patreon downloader

## Requirements

- Python 3.11+
- [Brave Browser](https://brave.com) (recommended — built-in ad blocker reduces popup interference during downloads; Chromium works as a fallback)
- ffmpeg / ffprobe (installed automatically by setup)

## Setup

**Linux / macOS**
```bash
chmod +x setup.sh
./setup.sh
```

**Windows**
```
setup.bat
```

Setup will:
1. Create a Python virtual environment and install dependencies
2. Download portable ffmpeg/ffprobe binaries
3. Prompt for credentials (iwara.tv, mega.nz, pixeldrain, spankbang) — stored in OS keyring
4. Configure settings (headless browser, max resolution, auto-dedup)
5. Run a disk I/O benchmark to find the optimal thread count for deduplication

Credentials are stored in the OS keyring (Windows Credential Manager, macOS Keychain, Linux Secret Service). Settings are written to `.env`.

## Usage

**Linux / macOS**
```bash
./run.sh
```

**Windows**
```
run.bat
```

### Menu options

| # | Option | Description |
|---|--------|-------------|
| 1 | Fix file prefixes | Strip the numeric attachment ID prefix from downloaded filenames |
| 2 | Download content | Find links in `description.json` files and download the associated media |
| 3 | Check funscript match | Report videos missing a funscript with fuzzy-match suggestions |
| 4 | Generate HTML | Build a `description.html` visual overview in each post folder |
| 5 | Sync new folders | Copy new Patreon downloader output into the working directory |
| 6 | Fix garbled names | Four-pass cleanup: fix video extensions → fix funscript extensions → decode garbled names → match funscript names to videos |
| 7 | Dedupe only | Clean leftover temp files and remove exact duplicate files |

### Fix garbled names (option 6) — detail

Runs four passes in order so each step sees already-corrected extensions:

1. **Media content fix** — reads magic bytes to detect video files with wrong or missing extensions (MP4, MKV, AVI, FLV) and renames them
2. **Funscript extension fix** — reads file content to detect `.json`, `.funsc`, or extension-less files that are actually funscripts and renames them to `.funscript`
3. **Garbled filename fix** — decodes percent-encoded filenames and reverses Latin-1/cp1252 mojibake
4. **Funscript-to-video match** — fuzzy-matches each funscript to the best video in the same folder; renames at ≥85% confidence, writes uncertain matches to a report for manual review

All changes are written to CSV reports in `_reports/`.

### Skipping folders

Place a file named `.manual` in any folder to exclude it from all automated processing.

## Configuration

All settings live in `.env` (created by setup). Key options:

| Variable | Default | Description |
|----------|---------|-------------|
| `BROWSER_HEADLESS` | `true` | Run Brave in headless mode. Set to `false` if sites block automation |
| `MAX_RESOLUTION` | `1080` | Maximum download resolution |
| `DEDUP_EXISTING` | `true` | Auto-dedup at the start of each download run. Option 7 always runs regardless of this |
| `SKIP_KNOWN_FAILURES` | `false` | Skip links listed in `failed_downloads.csv` |
| `DEDUP_VERBOSE` | `false` | Print one line per file during dedup. Warning: very noisy on large libraries |
| `DEDUP_THREADS` | (benchmarked) | Parallel threads for hashing during dedup |

## Supported download sources

| Source | Notes |
|--------|-------|
| iwara.tv | Requires account credentials for 18+ content |
| mega.nz | Requires MEGAcmd and optional account for private links |
| pixeldrain.com | Optional API key for private files |
| spankbang.com | Requires account credentials |
| hanime1.me / hanime.tv | Via browser automation |
| gofile.io | Public files |
| rule34.xxx / rule34video.com | |
| e621.net | |
| eporner.com | |
| fap-nation.org | |
| faptap.net | Follows source links to the original host |
| disk.yandex.com | |
| yt-dlp fallback | Any site supported by yt-dlp |

## Output files

| File | Description |
|------|-------------|
| `failed_downloads.csv` | Links that could not be downloaded |
| `_reports/garbled_names.csv` | Garbled filename fix results |
| `_reports/funscript_renames.csv` | Funscript extension fix results |
| `_reports/funscript_video_matches.csv` | Funscript-to-video match results (renamed + uncertain) |
| `_reports/media_renames.csv` | Media extension fix results |
| `full_folder_playlist.m3u8` | Playlist of all videos in the scanned directory |
| `new_media_playlist.m3u8` | Playlist of videos downloaded in the last run |

---

## Credits

Originally created by Marty M. Evolved and heavily influenced through collaborative development with [Claude](https://claude.ai) (Anthropic).
