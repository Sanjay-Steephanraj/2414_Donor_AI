import argparse
import json
import os
import re
import time
from pathlib import Path

import requests
from dotenv import load_dotenv


APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_JSON_PATH = APP_DIR / "2414_Ministry_Details.json"
DEFAULT_OUTPUT_DIR = APP_DIR / "ministry_upload_files"

GLOO_UPLOAD_URL = "https://platform.ai.gloo.com/ingestion/v2/files"


def clean_filename(value):
    """Return a Windows-safe filename segment while keeping names readable."""

    value = str(value or "").strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" .")


def ministry_filename(record, seen_names):
    name = clean_filename(record.get("Ministry name"))
    code = clean_filename(record.get("Ministry code"))

    if not name or not code:
        raise ValueError(f"Missing ministry name or code in record: {record}")

    filename = f"{name} {code}.txt"

    if filename.lower() not in seen_names:
        seen_names.add(filename.lower())
        return filename

    serial = clean_filename(record.get("S No")) or str(len(seen_names) + 1)
    filename = f"{name} {code} {serial}.txt"
    seen_names.add(filename.lower())
    return filename


def ministry_text(record):
    lines = []

    for key in (
        "Ministry name",
        "Ministry code",
        "Location",
        "Description",
        "Category 1",
        "Category 2",
        "Category 3",
    ):
        value = record.get(key)

        if value is None or value == "":
            continue

        lines.append(f"{key}: {value}")

    return "\n".join(lines) + "\n"


def load_ministries(json_path):
    with open(json_path, "r", encoding="utf-8-sig") as file:
        records = json.load(file)

    if not isinstance(records, list):
        raise ValueError("Expected the ministry JSON file to contain a list.")

    return records


def write_text_files(records, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    seen_names = set()
    paths = []

    for record in records:
        if not isinstance(record, dict):
            raise ValueError(f"Expected each ministry record to be an object: {record}")

        filename = ministry_filename(record, seen_names)
        path = output_dir / filename

        with open(path, "w", encoding="utf-8") as file:
            file.write(ministry_text(record))

        paths.append(path)

    return paths


def get_api_key():
    api_key = os.getenv("GLOO_API_KEY") or os.getenv("API_KEY")
    if not api_key:
        raise ValueError("GLOO_API_KEY (or API_KEY) must be set in .env")
    return api_key


def upload_file(path, api_key, publisher_id):
    with open(path, "rb") as file:
        response = requests.post(
            GLOO_UPLOAD_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"files": (path.name, file, "text/plain")},
            data={"publisher_id": publisher_id},
            timeout=120,
        )

    if response.status_code not in (200, 201):
        raise requests.HTTPError(
            f"Upload failed for {path.name}: "
            f"{response.status_code} {response.text}",
            response=response,
        )

    return response


def upload_all(paths, delay_seconds):
    publisher_id = os.getenv("PUBLISHER_ID")

    if not publisher_id:
        raise ValueError("PUBLISHER_ID must be set in .env")

    api_key = get_api_key()
    uploaded = 0

    for index, path in enumerate(paths, start=1):
        print(f"[{index}/{len(paths)}] Uploading {path.name}")
        upload_file(path, api_key, publisher_id)
        uploaded += 1

        if delay_seconds and index < len(paths):
            time.sleep(delay_seconds)

    return uploaded


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Create one text file per ministry JSON record and upload each "
            "file to Gloo ingestion."
        )
    )
    parser.add_argument(
        "--json",
        default=str(DEFAULT_JSON_PATH),
        help="Path to 2414_Ministry_Details.json",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory where generated .txt files are written",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Create the .txt files and print the upload count without uploading.",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=0,
        help="Optional delay between uploads.",
    )
    return parser.parse_args()


def main():
    load_dotenv(APP_DIR.parent / ".env")
    load_dotenv(APP_DIR / ".env")

    args = parse_args()
    json_path = Path(args.json).resolve()
    output_dir = Path(args.output_dir).resolve()

    records = load_ministries(json_path)
    paths = write_text_files(records, output_dir)

    print(f"Created {len(paths)} text files in {output_dir}")
    print(f"Example file: {paths[0].name if paths else 'none'}")

    if args.dry_run:
        print("Dry run complete. No files were uploaded.")
        return

    uploaded = upload_all(paths, args.delay_seconds)
    print(f"Uploaded {uploaded} files to Gloo ingestion.")


if __name__ == "__main__":
    main()
