#!/usr/bin/env python3
"""Refresh aports/apps/uflinux-files/APKBUILD from upstream's default branch.

uflinux-files is not a Craft app: it has no releases and no tags, so there is no
"latest version" to resolve. The only addressable thing upstream publishes is a
commit, so this pins the recipe to the head of the default branch and re-hashes
the archive of that commit.

    git ls-remote --symref <repo> HEAD      default branch name and its head sha
    archive/<sha>.tar.gz                    the source, hashed and read for its
                                            [workspace.package] version

A new commit that also raises the workspace version becomes a new pkgver with
pkgrel reset. A new commit at the same version raises pkgrel instead, which
matters because a release that rebuilds only this package carries the previous
build over from the published repository: without a higher version-release apk
would keep offering the older file.

The GitHub REST API is deliberately not used, as in update-checksums.py:
unauthenticated requests are limited to 60/hour per IP and shared runners
routinely exceed that, while these endpoints are not rate limited.

    current    uflinux-files 0.1.0-r3              nothing to do
    updated    uflinux-files 0.1.0-r2 -> 0.1.0-r3 (_commit, sha512sums, pkgrel)
    failed     uflinux-files: <reason>             on stderr

The second field of an `updated` line is the package name, which is how
update.yml works out what to rebuild.

    --check   exit 1 if the recipe is not current (no writes)
"""

import argparse
import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECIPE = os.path.join(ROOT, "aports", "apps", "uflinux-files", "APKBUILD")
REPO = "UFlinux-project/uflinux3-filemanager"
ARCHIVE = "https://github.com/" + REPO + "/archive/"

USER_AGENT = "uflinux3-apps-update-files"


def head_commit():
    """The default branch's head commit, in one unauthenticated request.

    --symref makes git report which branch HEAD points at, so the script follows
    a rename of the default branch without being told about it.
    """
    result = subprocess.run(
        ["git", "ls-remote", "--symref", "https://github.com/" + REPO, "HEAD"],
        check=True, capture_output=True, text=True, timeout=120)
    sha = ""
    branch = ""
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or fields[1] != "HEAD":
            continue
        if fields[0].startswith("ref:"):
            branch = fields[0].split()[1]
        elif re.fullmatch(r"[0-9a-f]{40}", fields[0]):
            sha = fields[0]
    if not sha or not branch:
        raise RuntimeError("could not resolve HEAD of " + REPO)
    return sha, branch


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read()


def workspace_version(blob, url):
    """Read [workspace.package] version from the tarball's root Cargo.toml.

    Same check update-checksums.py makes: a version that disagrees with the
    source should fail while updating, not partway through a build.
    """
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        names = archive.getnames()
        roots = {n.split("/")[0] for n in names if "/" in n}
        if len(roots) != 1:
            raise RuntimeError(url + ": not a single-directory tarball")
        root = roots.pop()
        if root + "/Cargo.toml" not in names:
            raise RuntimeError(url + ": no Cargo.toml at the root")
        text = archive.extractfile(root + "/Cargo.toml").read().decode("utf-8")
    in_pkg = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_pkg = line == "[workspace.package]"
        elif in_pkg and line.startswith("version"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(url + ": no [workspace.package] version")


def read_recipe():
    """The four fields this script owns, as strings."""
    with open(RECIPE) as handle:
        text = handle.read()
    fields = {}
    for name in ("pkgver", "pkgrel", "_commit"):
        match = re.search(r'^' + name + r'="?([^"\s]*)"?$', text, re.MULTILINE)
        if not match:
            raise RuntimeError("no " + name + "= assignment in the recipe")
        fields[name] = match.group(1)
    match = re.search(r'^([0-9a-f]{128})\s+uflinux-files-[^\s"]+$', text,
                      re.MULTILINE)
    if not match:
        raise RuntimeError("no sha512sums entry in the recipe")
    fields["sha512"] = match.group(1)
    return fields, text


def rewrite(text, commit, version, release, sha512):
    """The recipe with the pin, version, release and checksum replaced."""
    # Each assignment keeps whichever style the recipe already used, so the diff
    # stays one line per field.
    text = re.sub(r'^_commit=?("[^"]*"|\S+)$', '_commit="' + commit + '"',
                  text, count=1, flags=re.MULTILINE)
    text = re.sub(r'^pkgver=?("[^"]*"|\S+)$', 'pkgver=' + version,
                  text, count=1, flags=re.MULTILINE)
    text = re.sub(r'^pkgrel=?("[^"]*"|\S+)$', 'pkgrel=' + str(release),
                  text, count=1, flags=re.MULTILINE)
    text = re.sub(r'^[0-9a-f]{128}(\s+)uflinux-files-\S+$',
                  sha512 + r"\1uflinux-files-" + version + ".tar.gz",
                  text, count=1, flags=re.MULTILINE)
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="fail instead of writing when the recipe is stale")
    args = parser.parse_args()

    try:
        old = read_recipe()[0]
        commit, branch = head_commit()
        blob = fetch(ARCHIVE + commit + ".tar.gz")
        sha512 = hashlib.sha512(blob).hexdigest()
        version = workspace_version(blob, ARCHIVE + commit + ".tar.gz")
    except (RuntimeError, OSError, subprocess.CalledProcessError,
            urllib.error.URLError) as error:
        print("failed     uflinux-files: " + str(error), file=sys.stderr)
        return 1

    # A new commit at the same upstream version still has to be a newer
    # package, or a partial release would publish a file apk considers equal to
    # the one already online and keep serving the old build.
    release = 0 if version != old["pkgver"] else int(old["pkgrel"]) + 1
    if commit == old["_commit"] and sha512 == old["sha512"]:
        print("current    uflinux-files " + version + "-r" + old["pkgrel"]
              + " (" + branch + ")")
        return 0

    delta = []
    if commit != old["_commit"]:
        delta.append("_commit")
    if version != old["pkgver"]:
        delta.append("pkgver")
    if release != int(old["pkgrel"]):
        delta.append("pkgrel")
    if sha512 != old["sha512"]:
        delta.append("sha512sums")
    print("updated    uflinux-files %s-r%s -> %s-r%s (%s)"
          % (old["pkgver"], old["pkgrel"], version, release, ", ".join(delta)))
    if args.check:
        return 1

    text = rewrite(read_recipe()[1], commit, version, release, sha512)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(RECIPE), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, RECIPE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print("wrote      " + os.path.relpath(RECIPE, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
