"""PyInstaller entry point."""

import sys

from ingest.app import main

if __name__ == "__main__":
    sys.exit(main())
