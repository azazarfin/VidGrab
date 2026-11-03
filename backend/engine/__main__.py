"""Frozen engine: startup credential arrives on stdin, readiness on stdout."""

import argparse
import asyncio
import ipaddress
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


def fixture_network_guard(event, arguments):
    """The P02 process may contact the controlled fixture and local IPC only."""
    if event == "socket.getaddrinfo":
        host = arguments[0]
    elif event == "socket.connect":
        connection, address = arguments[:2]
        if connection.family == socket.AF_UNIX:
            return
        host = address[0]
    else:
        return
    try:
        allowed = ipaddress.ip_address(host).is_loopback
    except ValueError:
        allowed = False
    if not allowed:
        raise PermissionError("External networking is disabled in the P02 fixture spike")


class ReadyServer(uvicorn.Server):
    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if self.started:
            port = sockets[0].getsockname()[1]
            print(json.dumps({"spike_version": 1, "port": port, "pid": os.getpid(), "components": self.component_versions}), flush=True)


def main():
    parser = argparse.ArgumentParser(description="VidGrab packaged fixture engine")
    parser.add_argument("--self-check", action="store_true")
    arguments = parser.parse_args()
    paths, versions = check_components()
    if arguments.self_check:
        print(json.dumps({"spike_version": 1, "components": versions}, sort_keys=True))
        return
    bootstrap = Bootstrap.model_validate_json(sys.stdin.buffer.readline(16385))
    runtime = RuntimeConfig(Path(bootstrap.data_dir), Path(bootstrap.download_dir))
    sys.addaudithook(fixture_network_guard)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        app = create_engine_app(runtime, token=bootstrap.token, port=listener.getsockname()[1], components=paths)
        server = ReadyServer(uvicorn.Config(
            app, host="127.0.0.1", log_config=None, access_log=False,
            loop="asyncio", lifespan="on", timeout_graceful_shutdown=1,
        ))
        server.component_versions = versions
        asyncio.run(server.serve(sockets=[listener]))


if __name__ == "__main__":
    main()
