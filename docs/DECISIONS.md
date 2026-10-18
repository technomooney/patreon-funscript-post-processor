# Decisions log (append-only; newest last)

Each entry: what problem, what was decided, what was rejected, how to verify. Written so a later session knows *why*.

## 2026-10-17 - Shared per-library SQLite store
- **Problem:** the user transcodes with the AV1 compressor (`../Av1LibraryCompression`) and also pulls pixeldrain repos with a manual `rclone copy`; rclone compares name/size so transcoded files (.mkv->.mp4, smaller) look missing and get re-fetched. Separately, history was thousands of `.folder_log.json` files, and the compressor's `folder_log.use_root()` *moved* those away, silently erasing the downloader's history. The two tools could not read each other's records.
- **Decision:** one SQLite DB per library root (`.library-data/library.db`) shared by both tools via an identical `library_db.py`; machine-local data goes to user storage (`app_paths`). File identity (`files` table) survives renames/recompression, so downloads key on pixeldrain link id, not name/size. See `docs/LIBRARY_DB.md`.
- **Rejected:** JSON/JSONL manifests (no cross-queries, no cross-process safety); keeping data in the repo dir (tied to one checkout); committing library data to git (libraries are not repos).
- **Verify:** `python -m unittest discover -s tests -v`.
- **Plan:** `~/.claude/plans/ive-got-an-issue-zazzy-star.md` (parts 1-7: wrappers, standalone migration script, pixeldrain sync, global base64 decoding, Discord full history, drift detection, verified rebuild).
