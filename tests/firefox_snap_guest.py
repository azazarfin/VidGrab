"""Private event peer for the Firefox Snap check, only inside its Ubuntu VM."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.firefox_portal_guest import main


if __name__ == '__main__':
    main('snap')
