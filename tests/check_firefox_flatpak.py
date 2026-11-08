"""Opt-in stock Firefox Flatpak/native-host lifecycle through a private Ubuntu VM."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.check_firefox_portal import main


if __name__ == '__main__':
    main('flatpak')
