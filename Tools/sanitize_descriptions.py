"""Explicit maintenance: replace examples/metadata only; verify callsigns unchanged."""

import hashlib
from pathlib import Path
import sqlite3
import sys


EXAMPLES = [
    ("lookup", "SELECT callsign FROM callsigns WHERE callsign = 'W9KKN'",
     "Look up an exact callsign"),
    ("prefix_match", "SELECT callsign FROM callsigns WHERE callsign LIKE 'W9%' ORDER BY callsign",
     "Find callsigns starting with a prefix"),
    ("suffix_match", "SELECT callsign FROM callsigns WHERE callsign LIKE '%KKN' ORDER BY callsign",
     "Find callsigns ending with a suffix"),
    ("contains_match", "SELECT callsign FROM callsigns WHERE callsign LIKE '%AB%' ORDER BY callsign",
     "Find callsigns containing a substring"),
    ("partial_check", "SELECT callsign FROM callsigns WHERE callsign GLOB 'PA?F?Q' ORDER BY callsign",
     "Each question mark matches one unknown character"),
]


def fingerprint(connection):
    digest = hashlib.sha256()
    count = 0
    for (call,) in connection.execute("SELECT callsign FROM callsigns ORDER BY callsign"):
        value = call.encode("utf-8")
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
        count += 1
    return count, digest.hexdigest()


def sanitize(target):
    connection = sqlite3.connect(target.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
    try:
        connection.execute("BEGIN IMMEDIATE")
        before = fingerprint(connection)
        generated = connection.execute(
            "SELECT value FROM metadata WHERE key='generated'"
        ).fetchone()
        connection.execute("DELETE FROM examples")
        connection.executemany("INSERT INTO examples(name, query, description) VALUES (?, ?, ?)",
                               EXAMPLES)
        for _, query, _ in EXAMPLES:
            connection.execute("EXPLAIN " + query).fetchall()
        connection.execute("DELETE FROM metadata")
        rows = [
            ("callsigns", str(before[0]), "Total distinct stored callsigns in this database"),
            ("schema", "callsigns(callsign TEXT PRIMARY KEY)", "Callsign-only lookup schema"),
        ]
        if generated is not None:
            rows.append(("generated", generated[0],
                         "Original generation timestamp; not the last update timestamp"))
        connection.executemany("INSERT INTO metadata(key, value, description) VALUES (?, ?, ?)",
                               rows)
        if fingerprint(connection) != before:
            raise RuntimeError("Callsign content changed; rolling back.")
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("Integrity check failed; rolling back.")
        connection.commit()
        return before
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] in ("/H", "/?", "-h", "--help"):
        print("Usage: python sanitize_descriptions.py TARGET.DB\n"
              "Explicitly replaces examples/metadata with callsign-only descriptions.\n"
              "Never changes callsigns. Requires separate maintenance authorization.\n"
              "Does not establish database redistribution rights.")
        sys.exit(0 if len(sys.argv) == 2 else 2)
    count, digest = sanitize(Path(sys.argv[1]))
    print(f"PASS: {count} callsigns unchanged; SHA256 {digest}; integrity ok; examples compile.")
