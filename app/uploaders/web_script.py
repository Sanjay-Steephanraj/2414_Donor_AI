"""Backward-compatible entry point for the daily PDF crawler.

The old implementation performed local chunking and maintained a JSON manifest.
Use ``crawler.py`` instead; Gloo receives the complete PDF and SQLite records
the successfully uploaded versions.
"""
from crawler import main

if __name__ == "__main__":
    main()
