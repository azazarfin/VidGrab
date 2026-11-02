"""Explicit legacy import utility; never starts the app or resumes jobs."""

import argparse
import asyncio
import os
from pathlib import Path

from backend.db.migrations import import_legacy_database
from backend.runtime import RuntimeConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Selected, quiescent legacy SQLite database")
    parser.add_argument("--data-dir", type=Path, help="New directory; must not contain vidgrab.db")
    parser.add_argument("--download-dir", type=Path, help="Default media directory for missing settings")
    args = parser.parse_args()
    environ = dict(os.environ)
    if args.data_dir:
        environ["VIDGRAB_DATA_DIR"] = str(args.data_dir)
    if args.download_dir:
        environ["VIDGRAB_DOWNLOAD_DIR"] = str(args.download_dir)
    result = asyncio.run(import_legacy_database(args.source, RuntimeConfig.from_env(environ)))
    print(f"Imported database: {result.database_path}")
    print(f"Preserved legacy snapshot: {result.backup_path}")


if __name__ == "__main__":
    main()
