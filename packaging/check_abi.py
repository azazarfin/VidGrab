"""Audit every shipped ELF before declaring a Linux glibc baseline."""

import argparse
import json
from pathlib import Path
import re
import subprocess


def inspect_versions(path):
    result = subprocess.run(["readelf", "--version-info", str(path)], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError(f"Cannot inspect {path}: {result.stderr.strip()}")
    return result.stdout


def audit_tree(tree, *, max_glibc=None, inspect=inspect_versions):
    tree = Path(tree).resolve(strict=True)
    if not tree.is_dir():
        raise ValueError("Select a complete bundle directory")
    seen = set()
    requirements = {}
    maximum = (0,)
    for entry in sorted(tree.rglob("*")):
        path = entry.resolve(strict=True)
        if not path.is_relative_to(tree):
            raise ValueError(f"A bundle symlink points outside the shipped tree: {entry}")
        if not path.is_file() or path in seen:
            continue
        seen.add(path)
        with path.open("rb") as file:
            if file.read(4) != b"\x7fELF":
                continue
        versions = [tuple(map(int, version.split("."))) for version in re.findall(r"Name: GLIBC_([0-9.]+)", inspect(path))]
        required = max(versions, default=(0,))
        requirements[str(path.relative_to(tree))] = required
        maximum = max(maximum, required)
    if not requirements:
        raise ValueError("The selected tree contains no ELF files")
    if max_glibc is not None:
        ceiling = ".".join(map(str, max_glibc))
        offenders = [f"{path} needs glibc {'.'.join(map(str, version))}; selected ceiling is {ceiling}"
            for path, version in requirements.items() if version > max_glibc]
        if offenders:
            raise ValueError("\n".join(offenders))
    return {"elf_files": len(requirements), "required_glibc": ".".join(map(str, maximum)),
        "requirements": {path: ".".join(map(str, version)) for path, version in requirements.items()}}


def glibc_version(value):
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", value):
        raise argparse.ArgumentTypeError("Use a numeric glibc version such as 2.39")
    return tuple(map(int, value.split(".")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--max-glibc", type=glibc_version)
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()
    try:
        receipt = audit_tree(arguments.tree, max_glibc=arguments.max_glibc)
        output = json.dumps(receipt, indent=2) + "\n"
        if arguments.report:
            with arguments.report.open("x") as file:
                file.write(output)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(output, end="")


if __name__ == "__main__":
    main()
