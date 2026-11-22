#!/usr/bin/env python3
"""Verify the files a recipe ships against its own sha512sums.

abuild checks every entry in `source=` against `sha512sums=` before it builds,
including the ones that are not downloaded but sit next to the APKBUILD: the
mission-center PGO scripts and its vendored nvtop tarball. Editing one of those
without re-hashing it is therefore a build failure, and a slow one - the shard
fetches three tarballs, reports `1 computed checksum did NOT match`, and dies
six minutes in, once per architecture. Nothing else in CI notices: the recipe
freshness checks only compare upstream versions, and `dependency_digest()` in
ci-matrix.py deliberately ignores everything after `sha512sums=`.

So check it here instead:

    python3 scripts/check-distfiles.py

Every `source=` entry with no URL is a file that must exist in the recipe's
directory, must be listed in `sha512sums=`, and must hash to what is listed.
Every `sha512sums=` entry naming a file that exists there is checked the same
way, so a stale entry for a file no longer referenced is also an error.
"""

import hashlib
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APKS = sorted((ROOT / "aports" / "apps").glob("*/APKBUILD"))

# A source line is local when it names no URL, with or without a `dest::`
# prefix. `https://…/x-$pkgver.tar.gz` is remote; `x-$pkgver.sh` is not.
REMOTE = re.compile(r"://")
# Top-level `name=value` assignments, enough to expand the pins a source line
# uses: pkgver and the `_foo_commit` style hashes recipes define for themselves.
ASSIGN = re.compile(r'^([a-zA-Z_][a-zA-Z0-9_]*)="([^$"]*)"$')
VAR = re.compile(r"\$\{?([a-zA-Z_][a-zA-Z0-9_]*)\}?")
SUM = re.compile(r"^([0-9a-f]{64}|[0-9a-f]{128})\s+(\S+)\s*$")


def quoted_block(text, name):
    """The lines inside `name="…"`, or None if the variable is not set."""
    match = re.search(r'^' + name + r'="([^"]*)"', text, re.M)
    if match is None:
        return None
    return match.group(1).split()


def expand(value, variables):
    """Substitute the simple variables a source line can contain."""
    def substitute(match):
        return variables.get(match.group(1), match.group(0))

    previous = None
    while previous != value:
        previous = value
        value = VAR.sub(substitute, value)
    return value


def check(recipe):
    """Return a list of problems with this recipe's local distfiles."""
    text = recipe.read_text()
    directory = recipe.parent

    variables = {}
    for line in text.splitlines():
        assignment = ASSIGN.match(line)
        if assignment:
            variables[assignment.group(1)] = assignment.group(2)

    sources = quoted_block(text, "source")
    if sources is None:
        return [str(recipe) + ": no source= list"]

    listed = {}
    in_sums = False
    for line in text.splitlines():
        if line.startswith("sha512sums="):
            in_sums = True
            continue
        if in_sums:
            if line.strip() == '"':
                break
            entry = SUM.match(line)
            if entry is None:
                return [str(recipe) + ": unparseable sha512sums= block"]
            listed[entry.group(2)] = entry.group(1)

    problems = []
    wanted = {}
    referenced = set()
    for entry in sources:
        entry = expand(entry, variables)
        dest, sep, url = entry.partition("::")
        name, remote = (dest, url) if sep else (entry, entry)
        referenced.add(name)
        if REMOTE.search(remote):
            # A committed tarball for a URL source is a pre-downloaded distfile
            # under the URL's own basename; abuild uses it instead of fetching.
            stripped = remote.split("?")[0].split("#")[0]
            referenced.add(os.path.basename(stripped.split("://", 1)[-1]))
            continue
        wanted[name] = entry

    for name in wanted:
        if not (directory / name).is_file():
            problems.append("%s: %s is in source= but not in the directory"
                            % (recipe, name))
        elif name not in listed:
            problems.append("%s: %s has no sha512sums= entry" % (recipe, name))

    for name, digest in listed.items():
        path = directory / name
        if name not in wanted:
            # A tarball committed for a URL source is a pre-downloaded distfile
            # and abuild uses it instead of fetching; only a hashed file that no
            # source line mentions at all is a leftover from a bump.
            if name not in referenced and path.is_file():
                problems.append("%s: %s is hashed but not in source="
                                % (recipe, name))
            continue
        if path.is_file():
            actual = hashlib.sha512(path.read_bytes()).hexdigest()
            if actual != digest:
                problems.append("%s: %s does not match sha512sums=\n"
                                "         have %s\n         want %s"
                                % (recipe, name, actual, digest))
    return problems


def main():
    if not APKS:
        sys.exit("no APKBUILDs found under aports/apps")
    problems = []
    for recipe in APKS:
        problems.extend(check(recipe))
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print("fix with: abuild checksum (in the recipe's directory)",
              file=sys.stderr)
        return 1
    print("local distfiles match sha512sums= in %d recipes" % len(APKS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
