"""Restore the verified frozen engine after linuxdeploy rewrites ELF resources.

Run this after Tauri creates its AppDir and before the AppImage output plugin.
The output is a fresh tree; neither the deployed AppDir nor staging is modified.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess


def prepare(appdir, bundle, output):
    appdir = Path(appdir).resolve(strict=True)
    bundle = Path(bundle).resolve(strict=True)
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("The output AppDir already exists; select a fresh directory")
    output = output.resolve()
    for source in (appdir, bundle):
        if output.is_relative_to(source) or source.is_relative_to(output):
            raise ValueError("The output and input paths must not overlap")
    if not appdir.is_dir() or not bundle.is_dir():
        raise ValueError("Select a deployed AppDir and a complete staged bundle")
    engines = list(appdir.rglob("vidgrab-engine"))
    if len(engines) != 1:
        raise ValueError("The deployed AppDir must contain exactly one frozen engine")
    deployed = engines[0]
    if not deployed.resolve(strict=True).is_relative_to(appdir):
        raise ValueError("The deployed engine points outside the AppDir")
    relative = deployed.parent.relative_to(appdir)
    if relative == Path(".") or deployed.parent.is_symlink():
        raise ValueError("Select a complete AppDir with an engine resource directory")
    staged = bundle / "engine"
    required = [staged / "vidgrab-engine", staged / "_internal/tools/manifest.json"]
    required.extend(staged / "_internal/tools" / name for name in ("deno", "ffmpeg", "ffprobe"))
    required.extend(
        staged / "_internal/notices" / name
        for name in ("LICENSE", "THIRD_PARTY.md", "GPLv3.ffmpeg.txt", "FONTS_LICENSE.txt")
    )
    if staged.is_symlink() or not all(path.is_file() for path in required):
        raise ValueError("Use a complete staged engine with its verified bundled tools and license notices")
    shutil.copytree(appdir, output, symlinks=True)
    destination = output / relative
    shutil.rmtree(destination)
    shutil.copytree(staged, destination, symlinks=True)
    return destination / "vidgrab-engine"


def main():
    from check_abi import audit_tree, glibc_version

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--appdir", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-glibc", type=glibc_version, required=True)
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()
    try:
        engine = prepare(arguments.appdir, arguments.bundle, arguments.output)
        abi = audit_tree(arguments.output, max_glibc=arguments.max_glibc)
        checked = subprocess.run([str(engine), "--self-check"], check=True, capture_output=True, text=True,
            env={**os.environ, "PATH": "", "PYTHONPATH": "", "PYTHONHOME": ""}, timeout=30)
        receipt = {"appdir": str(arguments.output.resolve()), "engine": str(engine),
            "abi": abi, "components": json.loads(checked.stdout)}
        if arguments.report:
            with arguments.report.open("x") as file:
                file.write(json.dumps(receipt, indent=2) + "\n")
    except subprocess.CalledProcessError as error:
        parser.error(error.stderr.strip() or str(error))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({"appdir": receipt["appdir"], "elf_files": abi["elf_files"],
        "required_glibc": abi["required_glibc"], "components": receipt["components"]}, indent=2))


if __name__ == "__main__":
    main()
