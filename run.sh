#!/usr/bin/env bash

# ---------------------------------------------------------------------------
# run.sh — launch the Patreon downloader post-processor
# ---------------------------------------------------------------------------

# If not running inside a terminal (e.g. double-clicked in a file manager),
# relaunch this script inside the first terminal emulator we can find.
if [ ! -t 0 ]; then
    SELF="$(realpath "${BASH_SOURCE[0]}")"
    for term in gnome-terminal konsole xfce4-terminal lxterminal mate-terminal xterm; do
        if command -v "$term" &>/dev/null; then
            case "$term" in
                gnome-terminal) exec gnome-terminal -- bash "$SELF" ;;
                konsole)        exec konsole -e bash "$SELF" ;;
                *)              exec "$term" -e bash "$SELF" ;;
            esac
        fi
    done
    echo "No terminal emulator found. Please run this script from a terminal." >&2
    exit 1
fi

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d ".venv" ]; then
    echo "Virtual environment not found. Run ./setup.sh first."
    read -rp "Press Enter to close..."
    exit 1
fi

if [ ! -f ".env" ]; then
    echo ".env file not found. Run ./setup.sh first."
    read -rp "Press Enter to close..."
    exit 1
fi

# --- Program selection ------------------------------------------------------

DEPS_MARKER=".venv/.deps_updated_at"
DEPS_MAX_AGE_DAYS=7

print_help() {
    echo ""
    echo "  1) Sync new folders       — copy folders that are new in the Patreon"
    echo "     downloader output into the post-processor working directory, then"
    echo "     optionally check existing folders for files missing by content"
    echo "     (e.g. new funscripts added after the folder was first copied)"
    echo "     (run this first)"
    echo ""
    echo "  2) Fix file prefixes      — strip the attachment ID prefix from"
    echo "     downloaded filenames"
    echo ""
    echo "  3) Download content       — find links in description.json files"
    echo "     and download the associated videos and files"
    echo ""
    echo "  4) Extract variant archives — for creators (e.g. Pize) who now ship scripts"
    echo "     only inside a password-protected .rar/.zip/.7z per intensity variant:"
    echo "     extracts each archive and renames its funscripts with the variant folded"
    echo "     in, so they don't collide. Password resolved from local history first,"
    echo "     falling back to a live Discord fetch — first time for a creator, it"
    echo "     asks inline for the Discord channel to use, no separate setup needed."
    echo ""
    echo "  5) Download from funscript metadata — for creators who put the source"
    echo "     video's URL in the funscript's own metadata.video_url field instead of"
    echo "     (or in addition to) the post body: download it for any funscript that"
    echo "     has one but no matching video on disk yet. Same download engine as"
    echo "     option 3, tagged separately (own report files, own undo/folder_log entry)"
    echo "     so it never overwrites that run's output."
    echo ""
    echo "  6) Fix garbled names      — four-pass cleanup pipeline:"
    echo "     • detect video files with wrong/missing extension (magic bytes)"
    echo "     • detect funscripts with wrong/missing .funscript extension"
    echo "     • decode percent-encoded or mojibake filenames"
    echo "     • fuzzy-match funscript names to their video and rename to match"
    echo "     All changes written to CSV reports in _reports/"
    echo ""
    echo "  7) Check funscript match  — find videos missing a funscript and"
    echo "     report fuzzy-match suggestions, cross-checked against video/funscript"
    echo "     duration; can auto-rename a lone unmatched video to its funscript's"
    echo "     name when duration confirms it unambiguously (asks first)"
    echo ""
    echo "  8) Dedupe only            — clean leftover temp files, remove exact"
    echo "     duplicate files (a funscript compares by its points, keeping"
    echo "     whichever copy has richer metadata — not just whichever is oldest),"
    echo "     then runs pack consolidation (see 'p'), then tries to rename any"
    echo "     video still carrying a '[altN]' dedup-collision tag back to its"
    echo "     real name (matched to a funscript by name/duration) — uncertain"
    echo "     ones are left alone and written to a review report instead"
    echo "     all moved to .trash, undoable, no full download involved"
    echo ""
    echo "  9) Generate HTML          — build a description.html visual overview"
    echo "     in each post folder"
    echo ""
    echo "  10) Audit report          — read .folder_log.json from every post folder"
    echo "     and generate _reports/audit_report.html showing what each script"
    echo "     has done, with per-folder detail and an overall summary"
    echo ""
    echo "  n) Creator naming scripts — submenu of one-creator naming-quirk fixes"
    echo "     (e.g. MDemaxis's SMOOTH-prefix rename). Drop your own .py file into"
    echo "     scripts/creator_scripts/ to add one without editing this menu —"
    echo "     see scripts/creator_scripts/README.md for the contract."
    echo ""
    echo "  p) Consolidate packs      — for creators (e.g. Pize) who repost"
    echo "     already-released videos+scripts inside a later collection post:"
    echo "     finds a video sitting with no local funscript next to it whose"
    echo "     script only survives in one of these packs (confirmed by actual"
    echo "     audio/video comparison, not filename), keeps whichever copy fits"
    echo "     MAX_RESOLUTION better in the pack folder, and removes the"
    echo "     redundant copy — not every creator does this, most won't need it"
    echo ""
    echo "  sa) Set up unattended run — configure which steps run for a creator,"
    echo "     in what order, and every prompt's answer, so 'ra' can replay it"
    echo "     later with zero prompts. Re-run to edit an existing config."
    echo ""
    echo "  sc) Update credentials    — re-enter any service login/API key (pixeldrain,"
    echo "     iwara.tv, mega.nz, spankbang.com) without re-answering every other setup"
    echo "     question — run this if a saved credential expires or gets revoked"
    echo ""
    echo "  ru) Update dependencies   — upgrade pip packages in the venv (incl. yt-dlp,"
    echo "     undetected-chromedriver, selenium) — run this if downloads start failing"
    echo "     after a site or browser update"
    echo ""
    echo "  rz) Undo last action      — reverse the most recent renames/copies/dedupe"
    echo "     from options 1-8, a creator script (n), or consolidate packs (p)"
    echo "     (one level deep — running any of them again replaces what 'last"
    echo "     action' means)"
    echo ""
    echo "  ra) Run unattended        — replay a creator's saved unattended config"
    echo "     (set up via 'sa') start to finish with no prompts; writes its own"
    echo "     log file under that creator's _reports/ folder"
    echo ""
}

while true; do
    echo ""
    echo "========================================"
    echo "  Patreon Downloader Post-Processor"
    echo "========================================"
    echo ""
    if [ -f "$DEPS_MARKER" ]; then
        deps_age_days=$(( ($(date +%s) - $(cat "$DEPS_MARKER")) / 86400 ))
        if [ "$deps_age_days" -gt "$DEPS_MAX_AGE_DAYS" ]; then
            echo "  [deps] Dependencies are $deps_age_days day(s) old — choose 'ru' below to update."
            echo ""
        fi
    else
        echo "  [deps] Dependency update status unknown — choose 'ru' below to update."
        echo ""
    fi
    echo "  1) Sync new folders          — copy new folders in, then sync existing ones (run first)"
    echo "  2) Fix file prefixes         — strip the attachment ID prefix from filenames"
    echo "  3) Download content          — download videos/files linked in description.json"
    echo "  4) Extract variant archives  — extract password-protected per-variant script archives"
    echo "  5) Funscript-metadata download — fetch a video from a funscript's own video_url field"
    echo "  6) Fix garbled names         — 4-pass cleanup: extensions, mojibake, funscript-video match"
    echo "  7) Check funscript match     — find videos missing a funscript, suggest/auto-rename"
    echo "  8) Dedupe (+ consolidate)    — remove exact duplicates, fix cross-folder pack redundancy"
    echo "  9) Generate HTML             — build description.html in each post folder"
    echo "  10) Audit report             — build _reports/audit_report.html from folder logs"
    echo ""
    echo "  n)  Creator naming scripts   — one-creator naming-quirk fixes (drop-in plugins)"
    echo "  p)  Consolidate packs        — fix videos whose script only lives in a repost/pack folder"
    echo "  sa) Set up unattended run    — configure a creator's steps/order/answers"
    echo "  sc) Update credentials       — re-enter a service login/API key"
    echo "  ru) Update dependencies      — upgrade pip packages in the venv"
    echo "  rz) Undo last action         — reverse the most recent change"
    echo "  ra) Run unattended           — replay a creator's saved config, zero prompts"
    echo ""
    echo "  h) Help — full description of every option above"
    echo ""
    echo "  q) Exit"
    echo ""

    while true; do
        read -rp "Choose a program to run (1-10, n, p, sa, sc, ru, rz, ra, h=help, q=exit): " choice
        case "$choice" in
            q|Q)
                echo ""
                exit 0
                ;;
            h|H)
                print_help
                break
                ;;
            ru|RU)
                echo ""
                .venv/bin/pip install --quiet --upgrade pip
                .venv/bin/python scripts/update_deps.py
                date +%s > "$DEPS_MARKER"
                echo "Dependencies updated."
                break
                ;;
            sc|SC)
                echo ""
                .venv/bin/python scripts/setup_config.py --credentials
                break
                ;;
            sa|SA)
                echo ""
                .venv/bin/python scripts/setup_unattended.py
                break
                ;;
            n|N)
                echo ""
                .venv/bin/python scripts/creator_scripts_menu.py
                break
                ;;
            p|P)
                echo ""
                .venv/bin/python scripts/consolidate_packs.py
                break
                ;;
            rz|RZ)
                echo ""
                .venv/bin/python scripts/undo_last_action.py
                break
                ;;
            ra|RA)
                echo ""
                .venv/bin/python scripts/run_unattended.py
                break
                ;;
            1)
                echo ""
                .venv/bin/python scripts/sync_new_folders.py
                break
                ;;
            2)
                echo ""
                .venv/bin/python scripts/prefixFix.py
                break
                ;;
            3)
                echo ""
                .venv/bin/python scripts/downloadContent.py
                break
                ;;
            4)
                echo ""
                .venv/bin/python scripts/extract_variant_archives.py
                break
                ;;
            5)
                echo ""
                .venv/bin/python scripts/download_from_funscript_metadata.py
                break
                ;;
            6)
                echo ""
                .venv/bin/python scripts/fix_garbled_names.py
                break
                ;;
            7)
                echo ""
                .venv/bin/python scripts/check_funscripts.py
                break
                ;;
            8)
                echo ""
                .venv/bin/python scripts/dedupe_only.py
                break
                ;;
            9)
                echo ""
                .venv/bin/python scripts/generate_html.py
                break
                ;;
            10)
                echo ""
                .venv/bin/python scripts/generate_audit_report.py
                break
                ;;
            *)
                echo "Invalid choice. Please enter 1-10, n, p, sa, sc, ru, rz, ra, h, or q to exit."
                ;;
        esac
    done

    echo ""
    read -rp "Press Enter to return to menu..."
done
