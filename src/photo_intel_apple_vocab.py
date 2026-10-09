#!/usr/bin/env python3
"""Dump the Apple Photos person list as JSON, for photo-intel's name vocabulary.

Runs on the export host (the only host with the Photos library). The output feeds
`photo_intel_names_admin.py refresh-vocab` on the processing host, which loads it into
the `apple_persons` table — the list of names the web app's People typeahead
is willing to suggest.

Why this exists as a separate step: Phase 1 derives `persons` from
`XMP:PersonInImage`, which also carries face tags baked into imported files
by other software. Roughly half the resulting vocabulary has no Apple Photos
person behind it. Only Apple can say which names are real, and only the export host
can ask Apple.

`osxphotos persons` takes several minutes on a library this size, so this is
a deliberate on-demand / scheduled step rather than something the export runs
every two hours. A stale vocabulary degrades gracefully: a newly named person
simply isn't suggested until the next refresh, and stays typeable meanwhile.

Usage
-----
    photo_intel_apple_vocab.py [-o out.json]
    photo_intel_apple_vocab.py -o - | ssh user@processing-host \\
        'cat > /tmp/apple_vocab.json'

`_UNKNOWN_` — osxphotos' bucket for detected-but-unnamed faces, usually the
largest bucket — is dropped; it is not a person.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

OSXPHOTOS = shutil.which("osxphotos") or "osxphotos"
LINE_RE = re.compile(r"^\s*(?P<name>.+):\s*(?P<count>\d+)\s*$")


def parse(text: str) -> dict[str, int]:
    """Parse `osxphotos persons` output into {name: photo_count}.

    Names containing characters that need it come back quoted, and some
    carry trailing whitespace from Photos itself — both are normalized here
    so the table matches what the pipeline sees in XMP.
    """
    out: dict[str, int] = {}
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue                       # the "persons:" header and blanks
        name = m.group("name").strip()
        if len(name) >= 2 and name[0] == name[-1] and name[0] in "\"'":
            name = name[1:-1]
        name = name.strip()
        if not name or name.upper() == "_UNKNOWN_":
            continue
        out[name] = int(m.group("count"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", default="-", help="output path, or - for stdout")
    args = ap.parse_args()

    try:
        proc = subprocess.run([OSXPHOTOS, "persons"], capture_output=True,
                              text=True, timeout=1800)
    except FileNotFoundError:
        print(f"error: osxphotos not found at {OSXPHOTOS}", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print("error: osxphotos persons timed out after 30 min", file=sys.stderr)
        return 1

    if proc.returncode != 0:
        print(f"error: osxphotos exited {proc.returncode}\n{proc.stderr[:500]}",
              file=sys.stderr)
        return 1

    people = parse(proc.stdout)
    if not people:
        # Refusing to emit an empty vocabulary matters: loading one would
        # empty apple_persons, and an empty table means "vocab unknown", which
        # silently reverts the UI to suggesting every name.
        print("error: parsed zero persons — refusing to emit an empty vocabulary",
              file=sys.stderr)
        return 1

    doc = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "source": "osxphotos persons",
        "count": len(people),
        "persons": dict(sorted(people.items(), key=lambda kv: (-kv[1], kv[0]))),
    }
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"

    if args.out == "-":
        sys.stdout.write(text)
    else:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"{len(people)} persons -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
