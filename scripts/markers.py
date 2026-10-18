"""Folder marker files, and who may write them.

  .manual        written ONLY BY THE USER: "I organised/downloaded this folder by hand because automation did not work
                 right; leave it alone". No script may create it.
  .consolidated  written by the tools (consolidate_packs, extract_variant_archives, the Pize plugin) on folders they
                 emptied or merged themselves. It carries the same "automated passes skip this folder" meaning, but it
                 does not claim a human did anything.

Automated passes that must leave a folder alone ask `skips(filenames)`, which is true for either marker.
(Before 2026-10-18 the tools also wrote `.manual` next to `.consolidated`; those were removed by hand.)
"""
MANUAL = '.manual'
CONSOLIDATED = '.consolidated'


def skips(filenames) -> bool:
    """True if a folder whose file names are *filenames* is marked hands-off (by the user or by a tool)."""
    return MANUAL in filenames or CONSOLIDATED in filenames
