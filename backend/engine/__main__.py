"""Frozen engine: startup credential arrives on stdin, readiness on stdout."""

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import socket
import sys

from pydantic import BaseModel, ConfigDict, Field
import uvicorn

from backend.engine import create_engine_app
from backend.engine.resources import check_components
from backend.runtime import RuntimeConfig


class Bootstrap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_dir: str = Field(min_length=1)
    download_dir: str = Field(min_length=1)




class ReadyServer(uvicorn.Server):
    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if self.started:
            port = sockets[0].getsockname()[1]
            print(json.dumps({"spike_version": 1, "port": port, "pid": os.getpid(), "components": self.component_versions}), flush=True)


def _fetch_update(url: str, out_path: Path, max_bytes: int, timeout: int) -> None:
    import ssl
    import urllib.error
    import urllib.parse
    import urllib.request

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Remote update fetches require an HTTPS URL")
    if out_path.is_symlink():
        raise ValueError("Refused symlinked update download destination")

    class _SafeHttpsRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            nxt = urllib.parse.urlsplit(newurl)
            if nxt.scheme != "https" or not nxt.hostname:
                raise urllib.error.URLError("Refused downgrade redirect during update fetch")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    ssl_ctx = ssl.create_default_context()
    try:
        import certifi
        ssl_ctx.load_verify_locations(certifi.where())
    except ImportError:
        pass
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl_ctx),
        _SafeHttpsRedirect(),
    )
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "VidGrab-Updater/1", "Accept": "application/json, application/octet-stream"},
        method="GET",
    )
    tmp = out_path.with_name(f".{out_path.name}.part")
    total = 0
    try:
        with opener.open(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                raise RuntimeError(f"Update server returned HTTP {getattr(resp, 'status', 0)}")
            with tmp.open("wb") as fh:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("Update payload exceeded maximum allowed size")
                    fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp, out_path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _unpack_update(archive: Path, dest: Path, fmt: str) -> None:
    import posixpath
    import tarfile
    import zipfile

    if archive.is_symlink() or not archive.is_file():
        raise ValueError("Update archive must be a regular non-symlink file")
    if dest.exists() or dest.is_symlink():
        raise ValueError("Extraction destination must be a fresh directory")
    dest.mkdir(parents=True)

    def _clean_rel(raw_name: str) -> Path | None:
        if "\\" in raw_name or raw_name.startswith("/"):
            raise ValueError(f"Refused unsafe archive path: {raw_name}")
        norm = posixpath.normpath(raw_name)
        if norm in ("", "."):
            return None
        if norm.startswith("..") or "/../" in norm:
            raise ValueError(f"Refused traversal path in archive: {raw_name}")
        parts = [p for p in norm.split("/") if p]
        if any(p in (".", "..") for p in parts):
            raise ValueError(f"Refused invalid archive path segment: {raw_name}")
        return Path(*parts)

    if fmt in ("tar", "tar.gz"):
        mode = "r:gz" if fmt == "tar.gz" else "r:"
        with tarfile.open(archive, mode) as tf:
            for member in tf.getmembers():
                rel = _clean_rel(member.name)
                if member.issym() or member.islnk() or member.ischr() or member.isblk() or member.isfifo() or member.isdev():
                    raise ValueError(f"Refused special or link entry in archive: {member.name}")
                if rel is None:
                    continue
                target = dest / rel
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isreg():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    src_f = tf.extractfile(member)
                    if src_f is None:
                        raise ValueError(f"Unreadable archive member: {member.name}")
                    with src_f, target.open("wb") as out_f:
                        while True:
                            buf = src_f.read(65536)
                            if not buf:
                                break
                            out_f.write(buf)
                    if os.name != "nt":
                        os.chmod(target, 0o755 if (member.mode & 0o100) else 0o644)
                else:
                    raise ValueError(f"Refused unsupported archive entry type: {member.name}")
    elif fmt == "zip":
        with zipfile.ZipFile(archive, "r") as zf:
            for info in zf.infolist():
                # Reject Unix symlinks encoded in external_attr.
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError(f"Refused symlink entry in zip archive: {info.filename}")
                rel = _clean_rel(info.filename)
                if rel is None:
                    continue
                target = dest / rel
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info, "r") as src_f, target.open("wb") as out_f:
                        while True:
                            buf = src_f.read(65536)
                            if not buf:
                                break
                            out_f.write(buf)
                    if os.name != "nt":
                        unix_mode = (info.external_attr >> 16) & 0o777
                        os.chmod(target, 0o755 if (unix_mode & 0o100) else 0o644)
    else:
        raise ValueError(f"Unsupported update archive format: {fmt}")


def main():
    parser = argparse.ArgumentParser(description="VidGrab packaged media engine")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--fetch-update", action="store_true")
    parser.add_argument("--unpack-update", action="store_true")
    parser.add_argument("--url")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--max-bytes", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--timeout", type=int, default=6)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--dest", type=Path)
    parser.add_argument("--format", choices=("tar", "tar.gz", "zip"))
    arguments = parser.parse_args()
    if arguments.fetch_update:
        if not arguments.url or not arguments.out:
            parser.error("--fetch-update requires --url and --out")
        try:
            _fetch_update(arguments.url, arguments.out, arguments.max_bytes, arguments.timeout)
        except Exception as error:
            print(f"Update fetch unreachable: {type(error).__name__}", file=sys.stderr)
            sys.exit(1)
        return
    if arguments.unpack_update:
        if not arguments.archive or not arguments.dest or not arguments.format:
            parser.error("--unpack-update requires --archive, --dest, and --format")
        try:
            _unpack_update(arguments.archive, arguments.dest, arguments.format)
        except Exception as error:
            print(str(error), file=sys.stderr)
            sys.exit(1)
        return
    paths, versions = check_components()
    if arguments.self_check:
        print(json.dumps({"spike_version": 1, "components": versions}, sort_keys=True))
        return
    bootstrap = Bootstrap.model_validate_json(sys.stdin.buffer.readline(16385))
    runtime = RuntimeConfig(Path(bootstrap.data_dir), Path(bootstrap.download_dir))
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        app = create_engine_app(runtime, token=bootstrap.token, port=listener.getsockname()[1], components=paths,
                                component_versions=versions, media_mode="ordinary")
        server = ReadyServer(uvicorn.Config(
            app, host="127.0.0.1", log_config=None, access_log=False,
            loop="asyncio", lifespan="on", timeout_graceful_shutdown=1,
        ))
        server.component_versions = versions
        asyncio.run(server.serve(sockets=[listener]))


if __name__ == "__main__":
    main()
