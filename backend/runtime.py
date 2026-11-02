"""Explicit, per-user runtime paths. Resolving paths never creates them."""

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

MIN_PYTHON = (3, 11)
INSTALL_ROOT = Path(__file__).resolve().parents[1]


class RuntimeCompatibilityError(RuntimeError):
    pass


class OutputPathError(ValueError):
    pass


def check_python_compatibility(version=None):
    version = sys.version_info if version is None else version
    if tuple(version[:2]) < MIN_PYTHON:
        raise RuntimeCompatibilityError("VidGrab requires Python 3.11 or newer")


@dataclass(frozen=True)
class RuntimeConfig:
    data_dir: Path
    download_dir: Path
    logs_dir: Path | None = None
    temp_dir: Path | None = None

    def __post_init__(self):
        data_dir = Path(self.data_dir).expanduser().resolve()
        for name, value in (
            ("data_dir", data_dir),
            ("download_dir", self.download_dir),
            ("logs_dir", self.logs_dir or data_dir / "logs"),
            ("temp_dir", self.temp_dir or data_dir / "tmp"),
        ):
            object.__setattr__(self, name, Path(value).expanduser().resolve())

    @property
    def db_path(self) -> Path:
        return self.data_dir / "vidgrab.db"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, *, platform: str | None = None, home: Path | None = None):
        environ = os.environ if environ is None else environ
        platform = sys.platform if platform is None else platform
        home = Path.home() if home is None else Path(home)
        if platform == "win32":
            base = Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "VidGrab"
        elif platform == "darwin":
            base = home / "Library" / "Application Support" / "VidGrab"
        else:
            base = Path(environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "vidgrab"
        return cls(
            data_dir=Path(environ.get("VIDGRAB_DATA_DIR") or base),
            download_dir=Path(environ.get("VIDGRAB_DOWNLOAD_DIR") or home / "Downloads" / "VidGrab"),
            logs_dir=Path(environ["VIDGRAB_LOG_DIR"]) if environ.get("VIDGRAB_LOG_DIR") else None,
            temp_dir=Path(environ["VIDGRAB_TEMP_DIR"]) if environ.get("VIDGRAB_TEMP_DIR") else None,
        )

    def validate(self):
        check_python_compatibility()
        for directory in (self.data_dir, self.logs_dir, self.temp_dir, self.download_dir):
            if directory.is_relative_to(INSTALL_ROOT):
                raise RuntimeCompatibilityError("Runtime directories must be outside the source/install tree")
        try:
            self.validate_output(self.download_dir, "%(title)s.%(ext)s")
        except OutputPathError as error:
            raise RuntimeCompatibilityError(str(error)) from error

    def validate_output(self, download_dir: str | Path, filename_template: str) -> tuple[Path, str]:
        """Check literal output paths without creating directories or altering templates."""
        if not str(download_dir).strip() or "\0" in str(download_dir):
            raise OutputPathError("Download directory cannot be blank or contain null characters")
        if not isinstance(filename_template, str) or not filename_template.strip() or "\0" in filename_template:
            raise OutputPathError("Filename template cannot be blank or contain null characters")
        try:
            directory = Path(download_dir).expanduser().resolve()
            output = directory / filename_template
            resolved_output = output.resolve()
            protected = (INSTALL_ROOT, self.data_dir, self.logs_dir, self.temp_dir)
            if any(path.is_relative_to(root.resolve()) for path in (directory, resolved_output) for root in protected):
                raise OutputPathError("Download outputs must be outside the source/install tree and application storage")
        except (OSError, ValueError) as error:
            if isinstance(error, OutputPathError):
                raise
            raise OutputPathError("Invalid download output path") from error
        return directory, str(output)

    def prepare(self):
        """Only explicit startup/initialization may create application directories."""
        self.validate()
        for directory in (self.data_dir, self.logs_dir, self.temp_dir, self.download_dir):
            directory.mkdir(parents=True, exist_ok=True)
