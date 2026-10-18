# Decisions log (append-only; newest last)

Each entry: what problem, what was decided, what was rejected, how to verify. Written so a later session knows *why*.

## 2026-10-17 - Shared per-library SQLite store
- **Problem:** the user transcodes with the AV1 compressor (`../Av1LibraryCompression`) and also pulls pixeldrain repos with a manual `rclone copy`; rclone compares name/size so transcoded files (.mkv->.mp4, smaller) look missing and get re-fetched. Separately, history was thousands of `.folder_log.json` files, and the compressor's `folder_log.use_root()` *moved* those away, silently erasing the downloader's history. The two tools could not read each other's records.
- **Decision:** one SQLite DB per library root (`.library-data/library.db`) shared by both tools via an identical `library_db.py`; machine-local data goes to user storage (`app_paths`). File identity (`files` table) survives renames/recompression, so downloads key on pixeldrain link id, not name/size. See `docs/LIBRARY_DB.md`.
- **Rejected:** JSON/JSONL manifests (no cross-queries, no cross-process safety); keeping data in the repo dir (tied to one checkout); committing library data to git (libraries are not repos).
- **Verify:** `python -m unittest discover -s tests -v`.
- **Plan:** `~/.claude/plans/ive-got-an-issue-zazzy-star.md` (parts 1-7: wrappers, standalone migration script, pixeldrain sync, global base64 decoding, Discord full history, drift detection, verified rebuild).

## 2026-10-17 - App-level data moves from the repo dir to user storage
- **Problem:** creator profiles (`.creators.json`), password history (`.creator_data.db`), the undo pointer, and the logged-in Discord/JOIGang browser profiles all sat in the repo directory (gitignored). That ties them to one checkout, and a second tool (the AV1 compressor, future tools) could never share them.
- **Decision:** they live under `app_paths.app_home()` (`~/.local/share/library-tools`, `%LOCALAPPDATA%\library-tools`, or `$LIBRARY_TOOLS_HOME`). Profiles and passwords are tables in `app.db` (`app_db.py`; creator_db adds its tables to the same file); the pointer is a file there; browser profiles are `profiles/<name>/`. Old files are COPIED in on first use (`legacy_app_data.py`: INSERT OR IGNORE for passwords, copy-if-missing for files/profiles, Chromium `Singleton*` locks skipped) and are never moved or deleted -- the user can remove them once satisfied. `CREATOR_DB_PATH` / `ACTION_LOG_POINTER_PATH` overrides still work for tests.
- **Rejected:** moving (loses the rollback path if the new location misbehaves); a separate DB file for passwords (one app DB is simpler to back up and inspect); putting app data inside libraries (it is per-machine, not per-library).
- **Prod note:** the live copy at `/media/Software/StandaloneSoftware/patreondownloaderpostprocessing` has its own repo-root data; after syncing, the first run there copies it into the user's app home.
- **Verify:** `python3 -I -m unittest discover -s tests -v` (`tests/test_app_data.py`).
