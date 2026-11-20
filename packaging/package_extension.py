"""Clean, deterministic, non-destructive browser extension packager."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXTENSION_DIR = ROOT / "extension"
FIXED_ZIP_TIME = (2026, 11, 20, 0, 0, 0)
EXCLUDED_NAMES = {
    ".DS_Store",
    "Thumbs.db",
    "__pycache__",
    "manifest.json",
    "manifest.firefox.json",
}
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".swp", ".swo", ".tmp", ".bak", ".orig")


def derive_chrome_origin(public_key_b64: str) -> str:
    raw = base64.b64decode(public_key_b64.strip(), validate=True)
    digest = hashlib.sha256(raw).hexdigest()[:32]
    identifier = "".join(chr(ord("a") + int(ch, 16)) for ch in digest)
    return f"chrome-extension://{identifier}/"


def _collect_shared_files(source: Path) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    for root, dirs, names in os.walk(source, followlinks=False):
        root_path = Path(root)
        # Never walk symlinks or hidden/test/cache directories
        kept_dirs = []
        for name in sorted(dirs):
            candidate = root_path / name
            if candidate.is_symlink():
                raise ValueError(f"Refused symlink directory in extension sources: {candidate}")
            if name in EXCLUDED_NAMES or name.startswith(".") or name in ("tests", "test", "node_modules"):
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs

        for name in sorted(names):
            candidate = root_path / name
            if candidate.is_symlink():
                raise ValueError(f"Refused symlink file in extension sources: {candidate}")
            if not candidate.is_file():
                continue
            if name in EXCLUDED_NAMES or name.startswith(".") or name.endswith(EXCLUDED_SUFFIXES):
                continue
            rel = candidate.relative_to(source).as_posix()
            files.append((rel, candidate))
    files.sort(key=lambda item: item[0])
    return files


def _write_deterministic_zip(archive_path: Path, manifest_obj: dict, shared_files: list[tuple[str, Path]]) -> str:
    manifest_bytes = (json.dumps(manifest_obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    entries: list[tuple[str, bytes]] = [("manifest.json", manifest_bytes)]
    for rel, file_path in shared_files:
        entries.append((rel, file_path.read_bytes()))
    entries.sort(key=lambda item: item[0])

    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for arcname, payload in entries:
            info = zipfile.ZipInfo(filename=arcname, date_time=FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            zf.writestr(info, payload)

    return hashlib.sha256(archive_path.read_bytes()).hexdigest()


def _validate_manifest_common(manifest: dict, target: str) -> None:
    if manifest.get("manifest_version") != 3:
        raise ValueError(f"{target} manifest_version must be 3")
    if not manifest.get("name") or not manifest.get("version"):
        raise ValueError(f"{target} manifest must specify name and version")
    for perm in ("storage", "nativeMessaging"):
        if perm not in manifest.get("permissions", []):
            raise ValueError(f"{target} manifest missing required permission: {perm}")


def package_extensions(
    source_dir: Path,
    output_dir: Path,
    *,
    chrome_key: str | None = None,
    omit_chrome_key: bool = False,
    chrome_origin_override: str | None = None,
    firefox_id: str | None = None,
) -> dict:
    source = Path(source_dir).resolve(strict=True)
    if not (source / "manifest.json").is_file() or not (source / "manifest.firefox.json").is_file():
        raise ValueError("Extension source must contain manifest.json and manifest.firefox.json")

    output_raw = Path(output_dir)
    if output_raw.is_symlink() or output_raw.exists():
        raise ValueError("Choose a fresh output directory; package_extension.py never overwrites or deletes existing files")
    output = output_raw.resolve()
    if output == (ROOT / "dist").resolve():
        raise ValueError("Refusing to write directly into tracked dist/; choose a fresh staging directory")
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Output and extension source directories must not overlap")

    shared_files = _collect_shared_files(source)
    if not shared_files:
        raise ValueError("No extension payload files found")

    chrome_manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if "browser_specific_settings" in chrome_manifest:
        raise ValueError("Chrome manifest.json must not contain browser_specific_settings")
    if omit_chrome_key:
        chrome_manifest.pop("key", None)
    elif chrome_key:
        chrome_manifest["key"] = chrome_key.strip()
    _validate_manifest_common(chrome_manifest, "chrome")
    if "service_worker" not in chrome_manifest.get("background", {}):
        raise ValueError("Chrome manifest must define background.service_worker")

    if chrome_origin_override:
        chrome_origin = chrome_origin_override
    elif chrome_manifest.get("key"):
        chrome_origin = derive_chrome_origin(chrome_manifest["key"])
    else:
        chrome_origin = None

    firefox_manifest = json.loads((source / "manifest.firefox.json").read_text(encoding="utf-8"))
    firefox_manifest.pop("key", None)
    if firefox_id:
        firefox_manifest.setdefault("browser_specific_settings", {}).setdefault("gecko", {})["id"] = firefox_id.strip()
    _validate_manifest_common(firefox_manifest, "firefox")
    gecko_id = firefox_manifest.get("browser_specific_settings", {}).get("gecko", {}).get("id")
    if not gecko_id:
        raise ValueError("Firefox manifest must define browser_specific_settings.gecko.id")
    if "scripts" not in firefox_manifest.get("background", {}):
        raise ValueError("Firefox manifest must define background.scripts")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".ext-pkg-", dir=output.parent) as scratch:
        staging = Path(scratch) / "staged"
        staging.mkdir()

        chrome_zip = staging / "vidgrab-chrome-extension.zip"
        firefox_zip = staging / "vidgrab-firefox-extension.zip"
        firefox_xpi = staging / "vidgrab-firefox-extension.xpi"

        chrome_sha = _write_deterministic_zip(chrome_zip, chrome_manifest, shared_files)
        firefox_sha = _write_deterministic_zip(firefox_zip, firefox_manifest, shared_files)
        firefox_xpi.write_bytes(firefox_zip.read_bytes())

        checksums = [
            f"{chrome_sha}  {chrome_zip.name}",
            f"{firefox_sha}  {firefox_xpi.name}",
            f"{firefox_sha}  {firefox_zip.name}",
        ]
        (staging / "SHA256SUMS.txt").write_text("\n".join(sorted(checksums)) + "\n", encoding="utf-8")

        receipt = {
            "package_version": 1,
            "chrome_version": chrome_manifest["version"],
            "firefox_version": firefox_manifest["version"],
            "chrome_origin": chrome_origin,
            "firefox_id": gecko_id,
            "shared_file_count": len(shared_files),
            "artifacts": {
                chrome_zip.name: {"sha256": chrome_sha, "bytes": chrome_zip.stat().st_size},
                firefox_zip.name: {"sha256": firefox_sha, "bytes": firefox_zip.stat().st_size},
                firefox_xpi.name: {"sha256": firefox_sha, "bytes": firefox_xpi.stat().st_size},
            },
        }
        (staging / "extension-manifest.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        staging.rename(output)

    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=EXTENSION_DIR, help="Extension source directory")
    parser.add_argument("--output", type=Path, required=True, help="Fresh output directory (must not already exist)")
    parser.add_argument("--chrome-key", default=os.environ.get("VIDGRAB_CHROME_KEY"), help="Override Chrome manifest key")
    parser.add_argument(
        "--omit-chrome-key",
        action="store_true",
        default=os.environ.get("VIDGRAB_OMIT_CHROME_KEY") == "1",
        help="Omit public key from Chrome manifest for Chrome Web Store upload",
    )
    parser.add_argument(
        "--chrome-origin",
        default=os.environ.get("VIDGRAB_CHROME_ORIGIN"),
        help="Override expected chrome-extension://<id>/ origin metadata",
    )
    parser.add_argument(
        "--firefox-id",
        default=os.environ.get("VIDGRAB_FIREFOX_ID"),
        help="Override browser_specific_settings.gecko.id for Firefox",
    )
    args = parser.parse_args()
    try:
        receipt = package_extensions(
            args.source,
            args.output,
            chrome_key=args.chrome_key,
            omit_chrome_key=args.omit_chrome_key,
            chrome_origin_override=args.chrome_origin,
            firefox_id=args.firefox_id,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
