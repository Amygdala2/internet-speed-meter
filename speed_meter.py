#!/usr/bin/env python3
"""Measure download throughput with ten sequential HTTP requests."""

import argparse
import math
import sys
import time
from dataclasses import dataclass
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

REQUEST_COUNT = 10
CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class Measurement:
    elapsed: float
    size: int


@dataclass(frozen=True)
class Summary:
    average_time: float
    total_bytes: int
    megabytes_per_second: float


def summarize(measurements: list[Measurement]) -> Summary:
    """Use total bytes / total download time, not a mean of individual speeds."""
    if not measurements:
        raise ValueError("No measurements")
    total_time = sum(item.elapsed for item in measurements)
    if total_time <= 0:
        raise ValueError("Measurement time must be positive")
    total_bytes = sum(item.size for item in measurements)
    return Summary(
        average_time=total_time / len(measurements),
        total_bytes=total_bytes,
        megabytes_per_second=total_bytes / total_time / 1_000_000,
    )


def download(url: str, timeout: float) -> Measurement:
    request = Request(
        url,
        headers={
            "User-Agent": "internet-speed-meter/1.0",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
        },
    )
    size = 0
    started = time.perf_counter()
    with urlopen(request, timeout=timeout) as response:
        while chunk := response.read1(CHUNK_SIZE):
            size += len(chunk)
        elapsed = time.perf_counter() - started
        declared_length = response.headers.get("Content-Length")
        if declared_length is not None and size != int(declared_length):
            raise ValueError(
                f"Incomplete download: expected {declared_length} bytes, got {size}"
            )
    if size == 0:
        raise ValueError("The server returned an empty response")
    return Measurement(elapsed=elapsed, size=size)


def http_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in {"http", "https"} and parsed.hostname
        parsed.port  # Validate the port, if supplied.
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Invalid HTTP(S) URL") from exc
    if not valid:
        raise argparse.ArgumentTypeError("Supply an absolute http:// or https:// URL")
    return value


def positive_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Timeout must be a positive number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("Timeout must be a finite positive number")
    return timeout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", type=http_url, help="HTTP(S) URL of a large image or file")
    parser.add_argument(
        "--timeout", type=positive_timeout, default=30.0,
        help="Socket timeout in seconds (default: 30)",
    )
    args = parser.parse_args(argv)
    measurements = []
    for number in range(1, REQUEST_COUNT + 1):
        try:
            item = download(args.url, args.timeout)
        except (HTTPError, URLError, HTTPException, OSError, ValueError) as exc:
            print(f"Request {number}/{REQUEST_COUNT} failed: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("Measurement cancelled", file=sys.stderr)
            return 130
        measurements.append(item)
        print(
            f"Request {number}/{REQUEST_COUNT}: {item.elapsed:.6f} s, {item.size} bytes",
            flush=True,
        )

    result = summarize(measurements)
    print(f"Average request time: {result.average_time:.6f} s")
    print(f"Downloaded: {result.total_bytes} bytes ({result.total_bytes / 1_000_000:.6f} MB)")
    print(f"Download speed: {result.megabytes_per_second:.6f} MB/s")
    print(f"Download speed: {result.megabytes_per_second * 8:.6f} Mbit/s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
