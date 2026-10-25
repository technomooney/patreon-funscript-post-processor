# Shared library database contract

Used by the downloader, the AV1 compressor, and any future tool. `scripts/library_db.py` and
`scripts/app_paths.py` are **byte-identical in every repo** (a test in the downloader compares them);
change one, copy to all.

## Two tiers
| Tier | Where | Holds |
|---|---|---|
| Library DB | `<library>/.library-data/library.db` (a library = the folder a tool is run on, e.g. one creator folder; never shared by a parent) | history of that library's content; travels with the library |
| App DB | `app_paths.app_home()/app.db` (`~/.local/share/library-tools`, `%LOCALAPPDATA%\library-tools`, or `$LIBRARY_TOOLS_HOME`) | per-machine/user: creator profiles, passwords, Discord config, last-action pointer, library registry; browser profiles in `profiles/` |

Libraries are not git repos, so a library's history must live inside it. Secrets stay in the OS keyring.

## Rules for every tool
1. Shared schema is versioned by `PRAGMA user_version` (`SCHEMA_VERSION`). **Additive only**: new tables/columns, never rename or drop. A tool that finds a newer version must refuse to write (`SchemaTooNew`).
2. A tool adds its own tables as `<tool>_*` through `register_migration(root, tool, version, statements)` (own track in `schema_migrations`). Never alter another tool's or shared tables that way; a shared change means bumping `SCHEMA_VERSION` in all repos.
3. Free-form data: `tool_data(tool, key, json)`.
4. Every row records `updated_by` / `updated_at` where the table has them.
5. Short transactions (`db(root, write=True)`); WAL + 30s busy timeout let two tools run together.
6. Scans must skip `SKIP_DIRNAMES` (`.trash`, `.library-data`).
7. Migrations from legacy files **copy, never move or delete**.

## Shared tables (schema v1)
| Table | Purpose | Written by |
|---|---|---|
| `meta` | `library_id` (UUID), created_at, migration markers | any |
| `schema_migrations` | per-tool migration tracks | any |
| `tool_data` | free-form per-tool JSON | owning tool |
| `runs` | completed script runs per folder (was `.folder_log.json` / `library_log.jsonl`) | any via folder_log |
| `files` | file identity surviving rename/recompress (orig vs current name/size, sha256, pixeldrain id, codec, state) | downloader, compressor |
| `pixeldrain_links` | link id -> status/sha256/name/size/local file (was Pize `.manifest.json`) | downloader |
| `discord_messages` | scanned Discord messages, processed flag, decoded links | downloader |
| `downloader_external_links` | (downloader's own table, migration track `downloader` v1) links found outside the post, e.g. inside eroscripts threads: folder + url -> status/source | downloader |
| `markers` | `.manual` and similar hands-off markers | drift scan |
| `drift` | differences between disk and recorded state | drift scan |

## Adding a new tool
Import `library_db`, call `use_root(root)` at start, `register_migration(...)` for your tables, read shared tables freely, write shared rows only through the documented columns, and add a row to the table above.
