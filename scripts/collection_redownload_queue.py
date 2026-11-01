"""Persistent queue of collection videos that fell short of MAX_RESOLUTION
after extract_variant_archives._handle_bulk_collection() consolidated a bulk
collection archive's videos against any matching single-post copies already
in the library.

A collection extraction can only use whatever's *already* on disk to pick
the better-resolution copy — if neither the collection's own copy nor any
matching single-post copy meets the user's target, there's nothing more to
try locally, but the collection funscript's own metadata.video_url (when
present) is a real lead: something else could still be fetched over the
network to replace it. That's a deliberate, possibly-slow, possibly-bandwidth-
heavy action, so it's never done automatically at extraction time — it's
queued here instead, and downloadContent.find_and_download() offers to work
through the queue (defaulting to no) the next time it runs, per the user's
explicit request (2026-09-12): "add to an autodownload list then on next
download the script will ask to download flagged collection videos and will
default to no."

One report per library (flagged_collection_redownloads in the library database,
so it also shows in the HTML audit report) — a list of {folder, stem, video_url, current_height,
target_height, creator, flagged_at} entries, keyed for dedup purposes by
(folder, stem) so re-extracting (or re-scanning) the same collection archive
never piles up duplicate entries for a video already queued.
"""
import os
import time

import report_store

_REPORT = 'flagged_collection_redownloads'


def load(base_path: str) -> list[dict]:
    """All currently-queued entries for *base_path*. [] if nothing is queued (or no library database yet)."""
    return report_store.read_report(base_path, _REPORT)


def _save(base_path: str, entries: list[dict]) -> None:
    report_store.replace_report(base_path, _REPORT, entries)


def add(base_path: str, folder: str, stem: str, video_url: str,
        current_height: int, target_height: int, creator: str | None = None) -> None:
    """Queue one under-target collection video for a later redownload offer.
    A no-op if (folder, stem) is already queued — re-running extraction
    against the same collection shouldn't grow the queue without bound."""
    entries = load(base_path)
    for e in entries:
        if e.get('folder') == folder and e.get('stem') == stem:
            return  # already queued
    entries.append({
        'folder': folder,
        'stem': stem,
        'video_url': video_url,
        'current_height': current_height,
        'target_height': target_height,
        'creator': creator,
        'flagged_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    })
    _save(base_path, entries)
    print(f'  [collection-queue] flagged for possible redownload: {os.path.basename(folder)}/{stem} '
          f'({current_height}p, target {target_height}p)')


def remove(base_path: str, pairs: set[tuple[str, str]]) -> None:
    """Drop every entry whose (folder, stem) is in *pairs* — called once an
    entry has actually been offered and attempted (success or failure), so a
    declined offer keeps asking next time but an attempted one doesn't pile
    up regardless of outcome."""
    if not pairs:
        return
    entries = [e for e in load(base_path) if (e.get('folder'), e.get('stem')) not in pairs]
    _save(base_path, entries)
