import os
import shutil


def main():
    print()
    print("========================================")
    print("  Sync New Folders")
    print("========================================")
    print()
    print("Copies folders that exist in the source (Patreon downloader")
    print("output) but not yet in the destination (post-processor working")
    print("directory). Existing folders are never touched.")
    print()

    source = input("Source folder (Patreon downloader output): ").strip().strip('"\'')
    if not os.path.isdir(source):
        print(f"Directory not found: {source}")
        return

    destination = input("Destination folder (post-processor working dir): ").strip().strip('"\'')
    if not os.path.isdir(destination):
        print(f"Directory not found: {destination}")
        return

    source = os.path.abspath(source)
    destination = os.path.abspath(destination)

    if source == destination:
        print("Source and destination are the same directory — nothing to do.")
        return

    # Diff by folder name only — existing folders are never overwritten.
    source_folders = {
        f for f in os.listdir(source)
        if os.path.isdir(os.path.join(source, f))
    }
    dest_folders = {
        f for f in os.listdir(destination)
        if os.path.isdir(os.path.join(destination, f))
    }

    new_folders = sorted(source_folders - dest_folders)

    if not new_folders:
        print("\nNo new folders found — destination is already up to date.")
        return

    print(f"\nFound {len(new_folders)} new folder(s) to copy:")
    preview_limit = 20
    for folder in new_folders[:preview_limit]:
        print(f"  {folder}")
    if len(new_folders) > preview_limit:
        print(f"  ... and {len(new_folders) - preview_limit} more")

    print()
    confirm = input(f"Copy {len(new_folders)} folder(s) to destination? (y/n): ").strip().lower()
    if confirm != 'y':
        print("Cancelled.")
        return

    print()
    copied = 0
    errors = 0
    for i, folder in enumerate(new_folders, 1):
        src_path = os.path.join(source, folder)
        dst_path = os.path.join(destination, folder)
        print(f"  [{i}/{len(new_folders)}] {folder}")
        try:
            shutil.copytree(src_path, dst_path)
            copied += 1
        except OSError as e:
            print(f"    ERROR: {e}")
            errors += 1

    print()
    print(f"Done — copied: {copied}, errors: {errors}")


if __name__ == "__main__":
    main()
