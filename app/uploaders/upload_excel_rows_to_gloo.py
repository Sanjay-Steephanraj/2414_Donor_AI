import os
import re
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv


# ============================================================
# CONFIGURATION
# ============================================================

PROJECT_DIR = Path(__file__).resolve().parent.parent

load_dotenv(
    PROJECT_DIR / ".env"
)

XLSX_FILE = r"C:\Users\sanja\Downloads\Merged_Ministry_Details_Gloo_Normalized_cleaned.xlsx"

OUTPUT_DIR = Path("gloo_txt_files")


# ============================================================
# GLOO CREDENTIALS
# ============================================================

GLOO_API_KEY = os.getenv("GLOO_API_KEY") or os.getenv("API_KEY")
GLOO_PUBLISHER_ID = os.getenv("PUBLISHER_ID")


# ============================================================
# GLOO API ENDPOINTS
# ============================================================

GLOO_UPLOAD_URL = os.getenv(
    "GLOO_UPLOAD_URL",
    "https://platform.ai.gloo.com/ingestion/v2/files"
)


# ============================================================
# VALIDATION
# ============================================================

required_env = {
    "GLOO_API_KEY (or API_KEY)": GLOO_API_KEY,
    "PUBLISHER_ID": GLOO_PUBLISHER_ID,
}

missing = [
    key
    for key, value in required_env.items()
    if not value
]

if missing:
    raise ValueError(
        "Missing environment variables:\n"
        + "\n".join(
            f"  - {key}"
            for key in missing
        )
    )


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def clean_value(value):
    """
    Convert Excel values into clean strings.
    """

    if pd.isna(value):
        return ""

    value = str(value).strip()

    # Avoid Excel converting numeric values such as
    # 801ACH or numeric codes ending in .0
    if value.endswith(".0"):
        try:
            value = str(
                int(float(value))
            )
        except ValueError:
            pass

    return value


def safe_filename(value):
    """
    Convert ministry name/code into a Windows-safe filename.
    """

    value = clean_value(value)

    # Replace Windows-invalid filename characters.
    value = re.sub(
        r'[<>:"/\\|?*]',
        "_",
        value
    )

    # Replace whitespace with underscore.
    value = re.sub(
        r"\s+",
        "_",
        value
    )

    return value[:150]


def is_valid_location(value):
    """
    Determine whether a location field contains useful data.
    """

    value = clean_value(value)

    if not value:
        return False

    if value.lower() in {
        "-",
        "general",
        "not active",
        "unknown",
        "not specified",
    }:
        return False

    return True


# ============================================================
# LOCATION PROCESSING
# ============================================================

def build_location(row):
    """
    Build:

    Country: ...
    State: ...
    Continent: ...
    Hierarchy: ...

    from the normalized XLSX columns.
    """

    country = clean_value(
        row.get("Country", "")
    )

    state = clean_value(
        row.get("State / Region", "")
    )

    continent = clean_value(
        row.get("Continent", "")
    )

    original_location = clean_value(
        row.get("Location", "")
    )

    # --------------------------------------------------------
    # Remove placeholder values
    # --------------------------------------------------------

    if country.lower() in {
        "not specified",
        "unknown",
        "nan",
    }:
        country = ""

    if state.lower() in {
        "not specified",
        "unknown",
        "nan",
    }:
        state = ""

    if continent.lower() in {
        "not specified",
        "unknown",
        "nan",
    }:
        continent = ""

    # --------------------------------------------------------
    # If normalized fields are unavailable, preserve
    # original location rather than inventing geography.
    # --------------------------------------------------------

    if not country and not state and not continent:

        if is_valid_location(
            original_location
        ):
            return (
                f"Country: {original_location}\n"
                f"State: Not specified\n"
                f"Continent: Not specified\n"
                f"Hierarchy: {original_location}"
            )

        return (
            "Country: Not specified\n"
            "State: Not specified\n"
            "Continent: Not specified\n"
            "Hierarchy: Not specified"
        )

    # --------------------------------------------------------
    # Build hierarchy
    # --------------------------------------------------------

    hierarchy_parts = []

    if continent:
        hierarchy_parts.append(
            continent
        )

    if country:
        hierarchy_parts.append(
            country
        )

    if state:
        hierarchy_parts.append(
            state
        )

    hierarchy = " > ".join(
        hierarchy_parts
    )

    return (
        f"Country: {country or 'Not specified'}\n"
        f"State: {state or 'Not specified'}\n"
        f"Continent: {continent or 'Not specified'}\n"
        f"Hierarchy: {hierarchy or 'Not specified'}"
    )


# ============================================================
# CATEGORY PROCESSING
# ============================================================

def build_categories(row):
    """
    Combine Category 1, Category 2 and Category 3.
    """

    categories = []

    for column in [
        "Category 1",
        "Category 2",
        "Category 3",
    ]:

        value = clean_value(
            row.get(column, "")
        )

        if (
            value
            and value not in categories
        ):
            categories.append(
                value
            )

    if not categories:
        return "Not specified"

    return "\n".join(
        categories
    )


# ============================================================
# TXT CONTENT GENERATION
# ============================================================

def build_txt_content(row):
    """
    Generate one complete searchable ministry document.

    The TXT file explicitly contains:
        - Ministry Name
        - Ministry Code
        - Location
        - Description
        - Categories
    """

    # --------------------------------------------------------
    # MINISTRY INFORMATION
    # --------------------------------------------------------

    ministry_name = clean_value(
        row.get("Ministry name", "")
    )

    ministry_code = clean_value(
        row.get("Ministry code", "")
    )

    # --------------------------------------------------------
    # OTHER INFORMATION
    # --------------------------------------------------------

    description = clean_value(
        row.get("Description", "")
    )

    location = build_location(
        row
    )

    categories = build_categories(
        row
    )

    # --------------------------------------------------------
    # BUILD TXT DOCUMENT
    # --------------------------------------------------------

    content = f"""MINISTRY
Name: {ministry_name or "Not specified"}
Code: {ministry_code or "Not specified"}

LOCATION
{location}

DESCRIPTION
{description or "Not specified"}

CATEGORIES
{categories}
"""

    return content


# ============================================================
# GENERATE TXT FILES
# ============================================================

def generate_txt_files(df):
    """
    Create one TXT file for every Excel row.
    """

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    generated_files = []

    for index, row in df.iterrows():

        # ----------------------------------------------------
        # Get ministry name
        # ----------------------------------------------------

        ministry_name = clean_value(
            row.get(
                "Ministry name",
                ""
            )
        )

        # ----------------------------------------------------
        # Get ministry code
        # ----------------------------------------------------

        ministry_code = clean_value(
            row.get(
                "Ministry code",
                ""
            )
        )

        # ----------------------------------------------------
        # Fallback ministry name
        # ----------------------------------------------------

        if not ministry_name:
            ministry_name = (
                f"Ministry_{index + 1}"
            )

        # ----------------------------------------------------
        # Create filename using:
        #
        # Ministry_Name_Ministry_Code.txt
        # ----------------------------------------------------

        if ministry_code:

            filename = (
                f"{safe_filename(ministry_name)}_"
                f"{safe_filename(ministry_code)}.txt"
            )

        else:

            filename = (
                f"{safe_filename(ministry_name)}_"
                f"{index + 1:04d}.txt"
            )

        file_path = (
            OUTPUT_DIR / filename
        )

        # ----------------------------------------------------
        # Generate content
        # ----------------------------------------------------

        content = build_txt_content(
            row
        )

        # ----------------------------------------------------
        # Write TXT
        # ----------------------------------------------------

        file_path.write_text(
            content,
            encoding="utf-8"
        )

        generated_files.append(
            file_path
        )

        print(
            f"[CREATED] {file_path.name}"
        )

    return generated_files


# ============================================================
# GLOO AUTHENTICATION
# ============================================================

def get_gloo_api_key():
    """
    Return the Gloo API key used directly as a bearer token.
    """

    print(
        "\nAuthenticating with Gloo..."
    )

    print("Using Gloo API key authentication.")
    return GLOO_API_KEY


# ============================================================
# UPLOAD ONE FILE TO GLOO
# ============================================================

def upload_file_to_gloo(
    file_path,
    api_key
):
    """
    Upload one TXT document to Gloo.
    """

    headers = {
        "Authorization":
            f"Bearer {api_key}",
        "Accept":
            "application/json",
    }

    # --------------------------------------------------------
    # Multipart file upload
    # --------------------------------------------------------

    with open(
        file_path,
        "rb"
    ) as file:

        files = {
            "files": (
                file_path.name,
                file,
                "text/plain"
            )
        }

        data = {
            "publisher_id":
                GLOO_PUBLISHER_ID,
        }

        response = requests.post(
            GLOO_UPLOAD_URL,
            headers=headers,
            files=files,
            data=data,
            timeout=60,
        )

    # --------------------------------------------------------
    # Successful upload
    # --------------------------------------------------------

    if response.ok:

        print(
            f"[UPLOADED] {file_path.name}"
        )

        return True

    # --------------------------------------------------------
    # Failed upload
    # --------------------------------------------------------

    print(
        f"[FAILED] {file_path.name}"
    )

    print(
        f"Status: {response.status_code}"
    )

    print(
        f"Response: {response.text}"
    )

    return False


# ============================================================
# UPLOAD ALL FILES
# ============================================================

def upload_all_files(files):
    """
    Upload generated TXT files sequentially.
    """

    api_key = get_gloo_api_key()

    successful = 0
    failed = 0

    print(
        f"\nUploading {len(files)} files to Gloo..."
    )

    for file_path in files:

        try:

            success = (
                upload_file_to_gloo(
                    file_path,
                    api_key
                )
            )

            if success:
                successful += 1
            else:
                failed += 1

        except Exception as exc:

            failed += 1

            print(
                f"[ERROR] "
                f"{file_path.name}: "
                f"{exc}"
            )

        # ----------------------------------------------------
        # Avoid hammering the API
        # ----------------------------------------------------

        time.sleep(
            0.5
        )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print(
        "\n=============================="
    )

    print(
        "UPLOAD SUMMARY"
    )

    print(
        "=============================="
    )

    print(
        f"Successful : {successful}"
    )

    print(
        f"Failed     : {failed}"
    )

    print(
        "=============================="
    )


# ============================================================
# MAIN
# ============================================================

def main():

    # --------------------------------------------------------
    # Read Excel
    # --------------------------------------------------------

    print(
        f"Reading Excel file: {XLSX_FILE}"
    )

    df = pd.read_excel(
        XLSX_FILE
    )

    print(
        f"Found {len(df)} ministry records."
    )

    # --------------------------------------------------------
    # Verify required Excel columns
    # --------------------------------------------------------

    required_columns = [
        "Ministry name",
        "Ministry code",
        "Description",
        "Location",
        "Country",
        "State / Region",
        "Continent",
        "Category 1",
        "Category 2",
        "Category 3",
    ]

    missing_columns = [
        column
        for column in required_columns
        if column not in df.columns
    ]

    if missing_columns:

        print(
            "\nWARNING: The following Excel "
            "columns were not found:"
        )

        for column in missing_columns:
            print(
                f"  - {column}"
            )

        print(
            "\nAvailable Excel columns:"
        )

        for column in df.columns:
            print(
                f"  - {column}"
            )

        raise ValueError(
            "\nExcel column names do not "
            "match the expected structure."
        )

    # --------------------------------------------------------
    # Generate TXT files
    # --------------------------------------------------------

    txt_files = (
        generate_txt_files(df)
    )

    print(
        f"\nGenerated {len(txt_files)} TXT files."
    )

    # --------------------------------------------------------
    # Upload to Gloo
    # --------------------------------------------------------

    upload_all_files(
        txt_files
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
