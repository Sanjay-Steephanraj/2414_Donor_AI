"""
Read-only verification script.

Compares local `.txt` files in `scraped_pages/` against the items in the
Gloo publisher content library and reports which files are missing from,
extra in, or not ready in the remote library.

Usage:
    python verify_uploads.py
"""

import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv


def _get_env_or_fail(name: str) -> str:
    value = os.getenv(name)
    if not value:
        print(f"ERROR: {name} is missing or empty in .env")
        sys.exit(1)
    return value


def get_api_key() -> str:
    api_key = os.getenv("GLOO_API_KEY") or os.getenv("API_KEY")
    if not api_key:
        print("ERROR: GLOO_API_KEY (or API_KEY) is missing or empty in .env")
        sys.exit(1)
    return api_key


def fetch_all_items(api_key: str, publisher_id: str) -> list[dict]:
    base_url = f"https://platform.ai.gloo.com/engine/v2/publisher/{publisher_id}/items"
    items: list[dict] = []
    page = 1
    total_pages = 1

    while page <= total_pages:
        resp = requests.get(
            base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            params={"page": page, "page_size": 1000},
            timeout=60,
        )

        if resp.status_code != 200:
            print(f"ERROR: list-items request failed on page {page}, status {resp.status_code}")
            print(resp.text)
            sys.exit(1)

        data = resp.json()
        page_items = data.get("items", [])
        pagination = data.get("pagination", {})

        if page == 1:
            print(f"\nRemote library reports pagination.total = {pagination.get('total', '?')}\n")
            total_pages = pagination.get("total_pages", 1)

        items.extend(page_items)
        page += 1

    return items


def is_ready_status(status: str | None) -> bool:
    if not status:
        return False
    return status.strip().lower() in {
        "processed",
        "completed",
        "ready",
        "indexed",
        "success",
        "active",
    }


def main() -> None:
    app_dir = Path(__file__).resolve().parent.parent
    load_dotenv(app_dir / ".env")

    publisher_id = _get_env_or_fail("PUBLISHER_ID")

    scraped_dir = app_dir / "scraped_pages"

    if not scraped_dir.is_dir():
        print(f"ERROR: scraped_pages directory not found at {scraped_dir}")
        sys.exit(1)

    local_files = {path.name for path in scraped_dir.glob("*.txt")}

    api_key = get_api_key()
    items = fetch_all_items(api_key, publisher_id)

    remote_filenames: set[str] = set()
    null_filename_count = 0
    non_ready_items: list[tuple[str | None, str | None]] = []

    for item in items:
        filename = item.get("filename")
        status = item.get("status")

        if filename:
            remote_filenames.add(filename)
        else:
            null_filename_count += 1

        if not is_ready_status(status):
            non_ready_items.append((filename, status))

    missing = sorted(local_files - remote_filenames)
    extra = sorted(remote_filenames - local_files)

    print("=" * 60)
    print("UPLOAD VERIFICATION REPORT")
    print("=" * 60)
    print(f"Local files:          {len(local_files)}")
    print(f"Remote items total:   {len(items)}")
    print(f"Remote with filename: {len(remote_filenames)}")
    print(f"Remote with no filename: {null_filename_count}")
    print("-" * 60)

    print(f"\nMISSING FROM GLOO ({len(missing)}):")
    if missing:
        for name in missing:
            print(f"  - {name}")
    else:
        print("  (none)")

    print(f"\nEXTRA IN GLOO ({len(extra)}):")
    if extra:
        for name in extra:
            print(f"  - {name}")
    else:
        print("  (none)")

    print(f"\nNON-READY STATUS ({len(non_ready_items)}):")
    if non_ready_items:
        for filename, status in non_ready_items:
            print(f"  - filename={filename!r}, status={status!r}")
    else:
        print("  (none)")

    print("=" * 60)
    print(
        f"SUMMARY: {len(local_files)} local, "
        f"{len(remote_filenames)} in library, "
        f"{len(missing)} missing, {len(extra)} extra"
    )
    print("=" * 60)


if __name__ == "__main__":
    main()
