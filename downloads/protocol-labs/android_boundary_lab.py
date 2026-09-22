#!/usr/bin/env python3
"""Reproducible host-side fixtures for the Android security article.

This is deliberately a Python model, not an Android application.  It exercises
four boundaries with only the standard library: URL origin allowlisting,
SQLite value binding and row ownership, ZIP extraction, and request replay
deduplication.  The Android/Kotlin snippets in the article still need testing
on the app's real minSdk/targetSdk/device matrix.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import time
import unittest
from urllib.parse import urlsplit
import zipfile


ALLOWED_ORIGIN = ("https", "docs.example.com", 443)


class PolicyError(ValueError):
    """Input crossed a boundary that the fixture intentionally rejects."""


def allowed_docs_url(raw: str) -> bool:
    """Accept exactly https://docs.example.com on the default/443 port."""
    if not isinstance(raw, str) or not raw:
        return False
    if "\\" in raw or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in raw):
        return False
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.scheme.lower() != ALLOWED_ORIGIN[0]:
        return False
    if (parsed.hostname or "").lower() != ALLOWED_ORIGIN[1]:
        return False
    effective_port = 443 if port is None else port
    return effective_port == ALLOWED_ORIGIN[2]


def redirect_chain_allowed(urls: list[str]) -> bool:
    """Every navigation hop must remain inside the policy."""
    return bool(urls) and all(allowed_docs_url(url) for url in urls)


def fixture_database() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE reports (id INTEGER PRIMARY KEY, owner_id TEXT, title TEXT, secret TEXT)"
    )
    db.executemany(
        "INSERT INTO reports(owner_id, title, secret) VALUES (?, ?, ?)",
        [
            ("alice", "A report", "alice-secret"),
            ("bob", "B report", "bob-secret"),
        ],
    )
    return db


def unsafe_reports(db: sqlite3.Connection, caller_value: str) -> list[tuple]:
    """A vulnerable analogue of concatenating caller input into selection."""
    sql = "SELECT id, owner_id, title, secret FROM reports WHERE owner_id = '" + caller_value + "'"
    return list(db.execute(sql))


def bound_reports(db: sqlite3.Connection, caller_value: str) -> list[tuple]:
    return list(
        db.execute(
            "SELECT id, owner_id, title, secret FROM reports WHERE owner_id = ?",
            (caller_value,),
        )
    )


def owned_report(db: sqlite3.Connection, report_id: int, authenticated_owner: str) -> tuple | None:
    """Binding prevents injection; this owner predicate enforces object access."""
    return db.execute(
        "SELECT id, owner_id, title, secret FROM reports WHERE id = ? AND owner_id = ?",
        (report_id, authenticated_owner),
    ).fetchone()


def safe_sort_expression(requested: str) -> str:
    allowed = {"newest": "id DESC", "oldest": "id ASC", "title": "title COLLATE NOCASE ASC"}
    try:
        return allowed[requested]
    except KeyError as exc:
        raise PolicyError("sort key is not allowlisted") from exc


def _safe_member(root: Path, info: zipfile.ZipInfo) -> Path:
    name = info.filename
    if not name or "\x00" in name or "\\" in name:
        raise PolicyError(f"unsafe ZIP entry name: {name!r}")
    posix = PurePosixPath(name)
    if posix.is_absolute() or any(part in ("", ".", "..") for part in posix.parts):
        raise PolicyError(f"unsafe ZIP entry name: {name!r}")

    mode = (info.external_attr >> 16) & 0xFFFF
    if stat.S_ISLNK(mode):
        raise PolicyError(f"symbolic link entry rejected: {name!r}")

    target = (root / Path(*posix.parts)).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PolicyError(f"ZIP entry escapes destination: {name!r}") from exc
    return target


def extract_zip_safely(
    payload: bytes,
    destination: Path,
    *,
    max_entries: int = 32,
    max_total_bytes: int = 1_000_000,
    max_ratio: int = 100,
) -> list[Path]:
    """Extract into an empty private directory with path and resource limits."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve(strict=True)
    if any(root.iterdir()):
        raise PolicyError("destination must be empty")

    written: list[Path] = []
    declared_total = 0
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        infos = archive.infolist()
        if len(infos) > max_entries:
            raise PolicyError("too many ZIP entries")

        planned: list[tuple[zipfile.ZipInfo, Path]] = []
        for info in infos:
            target = _safe_member(root, info)
            declared_total += info.file_size
            if declared_total > max_total_bytes:
                raise PolicyError("declared uncompressed size exceeds limit")
            if info.file_size and info.file_size / max(info.compress_size, 1) > max_ratio:
                raise PolicyError("compression ratio exceeds limit")
            planned.append((info, target))

        actual_total = 0
        for info, target in planned:
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise PolicyError(f"refusing to overwrite: {info.filename!r}")
            with archive.open(info) as source, target.open("xb") as sink:
                while chunk := source.read(64 * 1024):
                    actual_total += len(chunk)
                    if actual_total > max_total_bytes:
                        raise PolicyError("actual uncompressed size exceeds limit")
                    sink.write(chunk)
            written.append(target)
    return written


def make_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def canonical_request(method: str, path: str, timestamp: int, nonce: str, body: bytes) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{body_hash}".encode("utf-8")


def sign_request(key: bytes, method: str, path: str, timestamp: int, nonce: str, body: bytes) -> str:
    return hmac.new(
        key,
        canonical_request(method, path, timestamp, nonce, body),
        hashlib.sha256,
    ).hexdigest()


class ReplayGate:
    def __init__(self, key: bytes, window_seconds: int = 60):
        self.key = key
        self.window_seconds = window_seconds
        self.seen_nonces: set[str] = set()

    def verify(
        self,
        method: str,
        path: str,
        timestamp: int,
        nonce: str,
        body: bytes,
        supplied_tag: str,
        *,
        now: int,
    ) -> tuple[bool, str]:
        if abs(now - timestamp) > self.window_seconds:
            return False, "stale"
        if nonce in self.seen_nonces:
            return False, "replay"
        expected = sign_request(self.key, method, path, timestamp, nonce, body)
        if not hmac.compare_digest(expected, supplied_tag):
            return False, "bad-authenticator"
        self.seen_nonces.add(nonce)
        return True, "accepted"


class UrlPolicyTests(unittest.TestCase):
    def test_exact_origin_cases(self) -> None:
        cases = {
            "https://docs.example.com/help": True,
            "https://DOCS.EXAMPLE.COM:443/help": True,
            "http://docs.example.com/help": False,
            "https://docs.example.com:444/help": False,
            "https://docs.example.com:0/help": False,
            "https://docs.example.com:65536/help": False,
            "https://docs.example.com.evil.test/help": False,
            "https://docs.example.com@evil.test/help": False,
            "https://evil.test/?next=docs.example.com": False,
            "https://docs.example.com\\@evil.test/help": False,
            "https://docs%2eexample.com/help": False,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(allowed_docs_url(raw), expected)

    def test_redirects_are_checked_per_hop(self) -> None:
        self.assertTrue(
            redirect_chain_allowed(
                ["https://docs.example.com/start", "https://docs.example.com/final"]
            )
        )
        self.assertFalse(
            redirect_chain_allowed(
                ["https://docs.example.com/start", "https://evil.test/final"]
            )
        )


class ProviderModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = fixture_database()

    def tearDown(self) -> None:
        self.db.close()

    def test_concatenation_is_injectable(self) -> None:
        rows = unsafe_reports(self.db, "alice' OR 1=1 --")
        self.assertEqual({row[1] for row in rows}, {"alice", "bob"})

    def test_bound_value_is_data(self) -> None:
        self.assertEqual(bound_reports(self.db, "alice' OR 1=1 --"), [])
        self.assertEqual(len(bound_reports(self.db, "alice")), 1)

    def test_row_owner_is_part_of_query(self) -> None:
        self.assertIsNone(owned_report(self.db, 2, "alice"))
        self.assertEqual(owned_report(self.db, 2, "bob")[3], "bob-secret")

    def test_identifier_uses_allowlist(self) -> None:
        self.assertEqual(safe_sort_expression("newest"), "id DESC")
        with self.assertRaises(PolicyError):
            safe_sort_expression("id DESC; DROP TABLE reports")


class ArchiveTests(unittest.TestCase):
    def test_nested_file_is_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            written = extract_zip_safely(make_zip({"images/a.txt": b"ok"}), out)
            self.assertEqual([path.relative_to(out).as_posix() for path in written], ["images/a.txt"])
            self.assertEqual((out / "images" / "a.txt").read_bytes(), b"ok")

    def test_traversal_and_backslash_are_rejected(self) -> None:
        for name in ("../escape.txt", "a/../../escape.txt", "/absolute.txt", "..\\escape.txt"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with self.assertRaises(PolicyError):
                    extract_zip_safely(make_zip({name: b"owned"}), root / "out")
                self.assertFalse((root / "escape.txt").exists())

    def test_symlink_entry_is_rejected(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "../outside")
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(PolicyError):
            extract_zip_safely(buffer.getvalue(), Path(tmp) / "out")

    def test_resource_limit_is_enforced(self) -> None:
        payload = make_zip({"large.txt": b"A" * 4096})
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(PolicyError):
            extract_zip_safely(payload, Path(tmp) / "out", max_total_bytes=1024)


class ReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = b"fixture-key-only"
        self.gate = ReplayGate(self.key)
        self.now = 1_700_000_000
        self.body = b'{"amount":100}'
        self.tag = sign_request(
            self.key, "POST", "/v1/pay", self.now, "nonce-001", self.body
        )

    def test_first_use_then_replay(self) -> None:
        first = self.gate.verify(
            "POST", "/v1/pay", self.now, "nonce-001", self.body, self.tag, now=self.now
        )
        second = self.gate.verify(
            "POST", "/v1/pay", self.now, "nonce-001", self.body, self.tag, now=self.now
        )
        self.assertEqual(first, (True, "accepted"))
        self.assertEqual(second, (False, "replay"))

    def test_authenticator_binds_body_and_path(self) -> None:
        changed = self.gate.verify(
            "POST", "/v1/refund", self.now, "nonce-001", self.body, self.tag, now=self.now
        )
        self.assertEqual(changed, (False, "bad-authenticator"))

    def test_stale_request_is_rejected(self) -> None:
        result = self.gate.verify(
            "POST", "/v1/pay", self.now, "nonce-001", self.body, self.tag, now=self.now + 61
        )
        self.assertEqual(result, (False, "stale"))


def demo() -> None:
    print("URL policy")
    for raw in (
        "https://docs.example.com/help",
        "https://docs.example.com:0/help",
        "https://docs.example.com:65536/help",
        "https://docs.example.com.evil.test/help",
        "https://docs.example.com@evil.test/help",
    ):
        print(f"  {allowed_docs_url(raw)!s:5}  {raw}")

    db = fixture_database()
    injected = unsafe_reports(db, "alice' OR 1=1 --")
    bound = bound_reports(db, "alice' OR 1=1 --")
    print(f"SQLite: concatenated rows={len(injected)}, bound rows={len(bound)}")
    db.close()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        try:
            extract_zip_safely(make_zip({"../escape.txt": b"x"}), root / "out")
        except PolicyError as exc:
            print(f"ZIP: rejected ({exc})")

    now = int(time.time())
    key = b"fixture-key-only"
    body = b'{"amount":100}'
    tag = sign_request(key, "POST", "/v1/pay", now, "nonce-001", body)
    gate = ReplayGate(key)
    first = gate.verify("POST", "/v1/pay", now, "nonce-001", body, tag, now=now)
    second = gate.verify("POST", "/v1/pay", now, "nonce-001", body, tag, now=now)
    print(f"Replay gate: first={first[1]}, second={second[1]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="print the four compact demonstrations")
    parser.add_argument("--test", action="store_true", help="run the unittest suite")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.demo:
        demo()
    if args.test or not args.demo:
        suite = unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__))
        result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(suite)
        return 0 if result.wasSuccessful() else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
