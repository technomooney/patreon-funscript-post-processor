# TODO

## Handle mixed video+funscript archives (Pize, changed again ~2026-08-19)

`scripts/extract_variant_archives.py` currently assumes a variant archive
contains *only* funscripts (see its module docstring) — it extracts
everything found and renames every extracted file as if it were one of the
variant's funscripts.

Pize has changed distribution again: instead of posting the video file
loose in the pixeldrain folder alongside a separate password-protected
funscript archive (the 3-4 separate files this repo was built to expect),
the video file(s) are now bundled *inside* the same zip as the funscripts.

Needs:
- Detect archive contents before/while extracting (or after, before the
  rename pass) and split by type: funscripts get the existing variant-tag
  rename treatment; video files need their own handling — almost certainly
  no variant tag folded in (the video is shared across variants, only the
  scripts differ), and probably need routing through whatever normally
  handles a downloaded video (naming convention, dedupe against an
  already-downloaded copy, `check_funscripts.py` matching, etc.) rather
  than the funscript-only rename path.
- Watch for the collision risk this could introduce: if multiple variant
  archives each bundle their own copy of the same video, naively extracting
  all of them would produce redundant/duplicate video files (or overwrite
  attempts) — decide whether to keep just one copy and discard the rest,
  or dedupe post-hoc (e.g. via hash, similar to the existing
  quality-replace-on-AV-match approach used elsewhere in this repo).
- Not yet confirmed against a real live example — only the "old" shape
  (funscripts-only zip) has been tested in production. Get a real sample
  of the new mixed-archive shape from the Pize folder before implementing.

See project memory `project_pize_pixeldrain_archives.md` for the prior
pixeldrain migration this builds on.
