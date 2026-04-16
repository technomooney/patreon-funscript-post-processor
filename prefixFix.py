import os
import re
from urllib.parse import unquote

# Matches the Patreon prefix: one or more non-underscore chars (type), underscore,
# one or more digits (ID), underscore.  Everything after is the real filename.
_PREFIX_RE = re.compile(r'^[^_]+_\d+_(.*)', re.DOTALL)

# ---------------------------------------------------------------------------
# Mojibake repair
# ---------------------------------------------------------------------------
# Filenames downloaded on a system that misread UTF-8 bytes as CP1252 end up
# with characters like U+0090 (DCS) embedded in the name — garbling it and
# breaking terminal output.  The reverse mapping below lets us re-encode each
# character back to the single byte it was before the mis-decoding, then
# re-decode those bytes as UTF-8 to recover the original text.
#
# Encoding chain that caused the damage:
#   original UTF-8 bytes  →  misread as CP1252  →  stored as UTF-8
# Repair:
#   current string  →  encode each char as its CP1252/Latin-1 byte  →  decode as UTF-8

# CP1252-only codepoints that aren't in Latin-1 (U+0080-U+009F range of CP1252).
_CP1252_EXTRAS: dict[int, int] = {
    0x20AC: 0x80,  # €
    0x201A: 0x82,  # ‚
    0x0192: 0x83,  # ƒ
    0x201E: 0x84,  # „
    0x2026: 0x85,  # …
    0x2020: 0x86,  # †
    0x2021: 0x87,  # ‡
    0x02C6: 0x88,  # ˆ
    0x2030: 0x89,  # ‰
    0x0160: 0x8A,  # Š
    0x2039: 0x8B,  # ‹
    0x0152: 0x8C,  # Œ
    0x017D: 0x8E,  # Ž
    0x2018: 0x91,  # '
    0x2019: 0x92,  # '
    0x201C: 0x93,  # "
    0x201D: 0x94,  # "
    0x2022: 0x95,  # •
    0x2013: 0x96,  # –
    0x2014: 0x97,  # —
    0x02DC: 0x98,  # ˜
    0x2122: 0x99,  # ™
    0x0161: 0x9A,  # š
    0x203A: 0x9B,  # ›
    0x0153: 0x9C,  # œ
    0x017E: 0x9E,  # ž
    0x0178: 0x9F,  # Ÿ
}


def _try_percent_decode(name: str) -> str | None:
    """Return percent-decoded *name*, or None if no change or decoding fails."""
    if '%' not in name:
        return None
    decoded = unquote(name, encoding='utf-8', errors='replace')
    if decoded == name:
        return None
    if '\ufffd' in decoded:
        return None
    return decoded


def _has_mojibake(name: str) -> bool:
    """Return True if *name* contains C1 control characters (U+0080-U+009F).

    These appear in filenames where UTF-8 bytes were misread as CP1252:
    e.g. the third byte of 【 (0x90) becomes U+0090 (DCS), which breaks
    terminal output and signals a garbled encoding.
    """
    return any(0x80 <= ord(c) <= 0x9F for c in name)


def _try_fix_mojibake(name: str) -> str | None:
    """Attempt to reverse CP1252-read-as-UTF-8 mojibake in *name*.

    Maps each character back to the single byte it represented before the
    mis-decoding (Latin-1 for U+0000-U+00FF; CP1252 extras for characters
    like €), reassembles the byte string, and decodes as UTF-8.
    Returns the repaired string, or None if the repair fails.
    """
    buf = bytearray()
    for c in name:
        o = ord(c)
        if o <= 0xFF:
            buf.append(o)           # direct Latin-1 / C0/C1 byte
        elif o in _CP1252_EXTRAS:
            buf.append(_CP1252_EXTRAS[o])
        else:
            return None             # character is not representable as one byte
    try:
        return buf.decode('utf-8')
    except UnicodeDecodeError:
        return None


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def main():
    filePath, extList = getUserInput()
    print(extList)
    fileList, fileRoots = getFileList(filePath, extList)
    processAndRename(fileList, fileRoots)


def getUserInput():
    filePath = input("Enter full file path for the downloaded files from patreon downloader: ")
    fileExtensionsToProcess = input("Enter file extensions of the files to process, separated by semicolon: ")
    if len(fileExtensionsToProcess) > 0:
        extList = fileExtensionsToProcess.split(";")
        for index, ext in enumerate(extList):
            extList[index] = "." + ext
    else:
        extList = []
    return filePath, extList


def getFileList(filePath: str, extList: list):
    fileList = []
    fileRoots = []
    for root, dirs, files in os.walk(filePath):
        dirs.sort()
        if '.manual' in files:
            print(f"  SKIP (manual): {root}")
            continue
        for file in sorted(files):
            if extList:
                if os.path.splitext(file)[1] in extList:
                    fileList.append(os.path.join(root, file))
                    fileRoots.append(root)
                else:
                    print(f"skipping (wrong ext): {file}")
            else:
                fileList.append(os.path.join(root, file))
                fileRoots.append(root)
    return fileList, fileRoots


def processAndRename(fileList: list, fileRoots: list):
    """For each file: percent-decode, repair mojibake if needed, then strip the Patreon prefix."""
    for index, file in enumerate(fileList):
        original_basename = os.path.basename(file)
        working_name = original_basename

        # Step 1: percent-decode %xx sequences if present.
        percent_decoded = _try_percent_decode(working_name)
        if percent_decoded is not None:
            print(f"  percent-decoded: {working_name!r}")
            print(f"               → {percent_decoded!r}")
            working_name = percent_decoded

        # Step 2: repair garbled encoding if C1 control chars are present.
        repaired = None
        if _has_mojibake(working_name):
            repaired = _try_fix_mojibake(working_name)
            if repaired:
                print(f"  repaired: {original_basename!r}")
                print(f"       → {repaired!r}")
                working_name = repaired
            else:
                print(f"  garbled (could not repair): {original_basename!r}")

        # Step 3: strip the Patreon prefix (type_id_) if present.
        m = _PREFIX_RE.match(working_name)
        if m:
            final_name = m.group(1)
            if not final_name:
                print(f"  prefix stripped but empty result — skipping: {working_name!r}")
                continue
        elif repaired or percent_decoded is not None:
            # The file was garbled/encoded and fixed, but has no prefix to strip —
            # still rename it to the repaired name.
            final_name = working_name
        else:
            print(f"  no prefix — skipping: {working_name!r}")
            continue

        dest = os.path.join(fileRoots[index], final_name)

        if dest == file:
            continue  # nothing changed

        if os.path.exists(dest):
            print(f"  destination already exists — skipping: {final_name!r}")
            continue

        try:
            os.rename(file, dest)
            print(f"  renamed: {original_basename!r}  →  {final_name!r}")
        except OSError as e:
            print(f"  rename failed: {e}")


if __name__ == "__main__":
    main()
