"""Minimal dev-only Redis stand-in.

fakeredis's TcpFakeServer relies on `fcntl` for non-blocking sockets and
hangs indefinitely against redis-py's async client on Windows (no
fcntl). This implements just the RESP2 surface the app actually uses
(PING, SET [EX], GET, DEL) over asyncio streams, which works the same
on every platform. Not a general Redis replacement — dev/local only.
"""

from __future__ import annotations

import asyncio
import time

_store: dict[str, tuple[bytes, float | None]] = {}


def _now() -> float:
    return time.monotonic()


def _get(key: str) -> bytes | None:
    entry = _store.get(key)
    if entry is None:
        return None
    value, expires_at = entry
    if expires_at is not None and expires_at <= _now():
        _store.pop(key, None)
        return None
    return value


async def _read_command(reader: asyncio.StreamReader) -> list[bytes] | None:
    line = await reader.readline()
    if not line:
        return None
    line = line.strip()
    if not line.startswith(b"*"):
        return None
    count = int(line[1:])
    args: list[bytes] = []
    for _ in range(count):
        head = (await reader.readline()).strip()
        length = int(head[1:])
        data = await reader.readexactly(length + 2)
        args.append(data[:-2])
    return args


def _bulk(value: bytes | None) -> bytes:
    if value is None:
        return b"$-1\r\n"
    return b"$" + str(len(value)).encode() + b"\r\n" + value + b"\r\n"


def _simple(value: str) -> bytes:
    return b"+" + value.encode() + b"\r\n"


def _integer(value: int) -> bytes:
    return b":" + str(value).encode() + b"\r\n"


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            args = await _read_command(reader)
            if args is None:
                break
            cmd = args[0].upper()
            if cmd == b"PING":
                writer.write(_simple("PONG"))
            elif cmd == b"SET":
                key, value = args[1].decode(), args[2]
                expires_at = None
                i = 3
                while i < len(args):
                    opt = args[i].upper()
                    if opt in (b"EX", b"PX") and i + 1 < len(args):
                        seconds = int(args[i + 1])
                        expires_at = _now() + (seconds if opt == b"EX" else seconds / 1000)
                        i += 2
                    else:
                        i += 1
                _store[key] = (value, expires_at)
                writer.write(_simple("OK"))
            elif cmd == b"GET":
                writer.write(_bulk(_get(args[1].decode())))
            elif cmd == b"DEL":
                n = 0
                for k in args[1:]:
                    if _store.pop(k.decode(), None) is not None:
                        n += 1
                writer.write(_integer(n))
            elif cmd == b"HELLO":
                writer.write(b"%0\r\n")
            else:
                writer.write(b"-ERR unsupported command\r\n")
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    finally:
        writer.close()


async def main() -> None:
    server = await asyncio.start_server(_handle, "127.0.0.1", 6379)
    print("mini-redis listening on 127.0.0.1:6379")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
