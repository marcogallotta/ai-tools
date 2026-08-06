"""Disposable TCP forwarding process for native PostgreSQL loss injection."""
from __future__ import annotations

import argparse
import json
import os
import select
import signal
import socket
import socketserver
import threading
from pathlib import Path


class _ForwardingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, handler, *, target_host: str, target_port: int):
        self.target_host = target_host
        self.target_port = target_port
        super().__init__(server_address, handler)


class _ForwardingHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, _ForwardingServer)
        with socket.create_connection((server.target_host, server.target_port), timeout=10.0) as target:
            client = self.request
            client.settimeout(None)
            target.settimeout(None)
            sockets = (client, target)
            while True:
                readable, _writable, exceptional = select.select(sockets, [], sockets, 1.0)
                if exceptional:
                    return
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    destination = target if source is client else client
                    destination.sendall(data)


def _write_ready(path: Path, *, host: str, port: int, target_host: str, target_port: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "format": "dish-postgresql-tcp-proxy-ready-v1",
                "pid": os.getpid(),
                "listen_host": host,
                "listen_port": port,
                "target_host": target_host,
                "target_port": target_port,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dish-postgresql-test-tcp-proxy")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--target-host", required=True)
    parser.add_argument("--target-port", type=int, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.listen_host not in {"127.0.0.1", "::1"}:
        raise SystemExit("test TCP proxy must bind loopback")
    if not (0 < args.listen_port <= 65535 and 0 < args.target_port <= 65535):
        raise SystemExit("TCP proxy ports must be between 1 and 65535")

    stop = threading.Event()

    def request_stop(_signum, _frame) -> None:
        stop.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, request_stop)

    with _ForwardingServer(
        (args.listen_host, args.listen_port),
        _ForwardingHandler,
        target_host=args.target_host,
        target_port=args.target_port,
    ) as server:
        server.timeout = 0.2
        _write_ready(
            args.ready_file,
            host=args.listen_host,
            port=args.listen_port,
            target_host=args.target_host,
            target_port=args.target_port,
        )
        while not stop.is_set():
            server.handle_request()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
