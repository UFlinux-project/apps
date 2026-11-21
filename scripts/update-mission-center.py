#!/usr/bin/env python3
"""Refresh aports/apps/mission-center/APKBUILD from upstream's newest release.

Mission Center is not a Craft app either, but unlike uflinux-files it does
publish tagged releases, so this resolves the newest stable one instead of
following a branch head:

    releases/permalink/latest           the newest release, by redirect
    archive/<tag>/...tar.gz             the source, hashed and read for its
                                        [package] version
    git fetch --filter=blob:none        the two submodule pins the tag records

The recipe builds two binaries out of two cargo workspaces, and the second one
is a submodule, so a release is not one source but three: Mission Center itself,
gng at the commit `subprojects/magpie` points at, and app-detection at the commit
gng's own `platform-linux/crates/app-rummage` points at. All three must be the
set the tag recorded, or the build is a mixture upstream never tested. GitLab's
archive endpoint has no submodule support at all - it ships empty directories -
so the pins are read out of the repositories with a blobless fetch and each
archive is downloaded and hashed separately. A blobless fetch costs about a
second, needs no API token, and is not rate limited, which is why neither the
GitLab REST API nor `git submodule update` is used here.

The nvtop archive magpie's build.rs unpacks is vendored in this repository as a
`.noauto` distfile so that the build stays offline. This script checks that the
vendored file is intact and that its pin is still the one gng asks for, but it
never rewrites it: a distfile whose name contains its own commit cannot be
replaced by editing one line, and the recipe's `source=` line and the checksum
below it would have to move together. When the pin does move, the script fails
and says so.

    current    mission-center 1.2.0-r0            nothing to do
    updated    mission-center 1.2.0-r0 -> 1.2.1-r0 (_magpie_commit, ...)
    failed     mission-center: <reason>           on stderr

The second field of an `updated` line is the package name, which is how
update.yml works out what to rebuild.

    --check   exit 1 if the recipe is not current (no writes)
"""

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECIPE = os.path.join(ROOT, "aports", "apps", "mission-center", "APKBUILD")
ORIGIN = "https://gitlab.com/mission-center-devs/mission-center"
GNG = "https://gitlab.com/mission-center-devs/gng"
APP_DETECTION = "https://gitlab.com/mission-center-devs/app-detection"
# A release's tag is its version with a leading v, and only a plain three-part
# version is stable: the same rule update-checksums.py applies to the Craft apps.
STABLE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
USER_AGENT = "uflinux3-apps-update-mission-center"
# Recipe variable -> the archive whose checksum line is keyed by that pin.
SUBMODULES = {
    "_magpie_commit": (GNG, "gng", "subprojects/magpie"),
    "_rummage_commit": (APP_DETECTION, "app-detection",
                        "platform-linux/crates/app-rummage"),
}


def head(url):
    """Follow redirects with HEAD and report the final URL."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT},
                                     method="HEAD")
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.geturl()


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read()


def latest_release():
    """The newest stable tag, from GitLab's permalink for the latest release.

    The permalink redirects to /releases/<tag>, which is the release GitLab
    itself considers newest - no API, no pagination, no guessing at whether a
    prerelease counts.
    """
    final = head(ORIGIN + "/-/releases/permalink/latest")
    tag = final.rsplit("/", 1)[-1].split("?")[0]
    if not STABLE.match(tag):
        raise RuntimeError("latest release " + repr(tag) + " is not a stable tag")
    return tag


def tree_pin(repository, ref, path):
    """The commit a gitlink at `path` points at, without downloading any blobs.

    `--filter=blob:none` fetches the commit and its trees only, and `--depth 1`
    keeps it to that one commit, so this costs one small round trip per
    repository instead of a clone. `git ls-tree` on the fetched tip then reports
    the gitlink the way the tag recorded it.
    """
    with tempfile.TemporaryDirectory() as scratch:
        git = ["git", "--git-dir", os.path.join(scratch, ".git")]
        subprocess.run(git + ["init", "-q", "--bare", os.path.join(scratch, ".git")],
                       check=True, capture_output=True, timeout=120)
        subprocess.run(git + ["remote", "add", "origin", repository],
                       check=True, capture_output=True, timeout=120)
        subprocess.run(git + ["fetch", "-q", "--depth", "1", "--filter=blob:none",
                              "origin", ref],
                       check=True, capture_output=True, timeout=300)
        listing = subprocess.run(git + ["ls-tree", "FETCH_HEAD", path],
                                 check=True, capture_output=True, text=True,
                                 timeout=120).stdout
    mode, kind, rest = (listing.split(None, 2) + ["", "", ""])[:3]
    if mode != "160000" or kind != "commit":
        raise RuntimeError(repository + " " + ref + ": " + path +
                           " is not a submodule")
    return rest.split()[0]


def archive_root(blob, url):
    """The single top-level directory of a source archive."""
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        roots = {name.split("/")[0] for name in archive.getnames() if "/" in name}
    if len(roots) != 1:
        raise RuntimeError(url + ": not a single-directory tarball")
    return roots.pop()


def package_version(blob, url):
    """Read [package] version from the tarball's root Cargo.toml.

    Same check update-checksums.py makes: a version that disagrees with the
    source should fail while updating, not partway through a build.
    """
    root = archive_root(blob, url)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        if root + "/Cargo.toml" not in archive.getnames():
            raise RuntimeError(url + ": no Cargo.toml at the root")
        text = archive.extractfile(root + "/Cargo.toml").read().decode("utf-8")
    in_pkg = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_pkg = line == "[package]"
        elif in_pkg and line.startswith("version"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(url + ": no [package] version")


def vendored_nvtop(blob, url):
    """The nvtop commit gng's third-party manifest asks for, and its sha256.

    Read out of the gng archive rather than typed in here, so that upstream
    moving the pin is something this script notices instead of something it
    quietly disagrees with.
    """
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        root = archive_root(blob, url)
        name = root + "/platform-linux/3rdparty/nvtop/nvtop.json"
        if name not in archive.getnames():
            raise RuntimeError(url + ": no nvtop.json")
        manifest = json.load(archive.extractfile(name))
    commit = manifest.get("directory", "").rsplit("-", 1)[-1]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError(url + ": nvtop.json has no commit in its directory")
    sha256 = manifest.get("source-hash", "")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise RuntimeError(url + ": nvtop.json has no source-hash")
    return commit, sha256


def read_recipe():
    """The fields this script owns, plus every checksum line, as strings."""
    with open(RECIPE) as handle:
        text = handle.read()
    fields = {}
    for name in ("pkgver", "pkgrel", "_magpie_commit", "_rummage_commit",
                 "_nvtop_commit"):
        match = re.search(r'^' + name + r'="?([^"\s]*)"?$', text, re.MULTILINE)
        if not match:
            raise RuntimeError("no " + name + "= assignment in the recipe")
        fields[name] = match.group(1)
    sums = dict((match.group(2), match.group(1)) for match in re.finditer(
        r'^([0-9a-f]{128})\s+([^\s"]+)$', text, re.MULTILINE))
    # The recipe also checksums the vendored nvtop archive and the three PGO
    # scripts it ships as distfiles, so the count is not pinned; the four
    # archives this script owns are, since a missing line would leave a stale
    # checksum behind after a bump.
    owned = ["mission-center-v" + fields["pkgver"] + ".tar.gz",
             "gng-" + fields["_magpie_commit"] + ".tar.gz",
             "app-detection-" + fields["_rummage_commit"] + ".tar.gz",
             "nvtop-" + fields["_nvtop_commit"] + ".tar.gz.noauto"]
    missing = [name for name in owned if name not in sums]
    if missing:
        raise RuntimeError("no sha512sums entry for " + ", ".join(missing))
    fields["sums"] = sums
    return fields, text


def check_vendored(fields):
    """The distfile on disk must still be the one the recipe checksums.

    The vendored archive is committed rather than downloaded, so a truncated
    copy or a line-ending accident is caught here, at the point where it is
    cheap to explain, instead of by a build that cannot fetch it.
    """
    name = "nvtop-" + fields["_nvtop_commit"] + ".tar.gz.noauto"
    path = os.path.join(os.path.dirname(RECIPE), name)
    if name not in fields["sums"]:
        raise RuntimeError("no sha512sums entry for " + name)
    if not os.path.isfile(path):
        raise RuntimeError("vendored distfile is missing: " + name)
    with open(path, "rb") as handle:
        digest = hashlib.sha512(handle.read()).hexdigest()
    if digest != fields["sums"][name]:
        raise RuntimeError("vendored distfile does not match its checksum: " + name)


def rewrite(text, pins, version, release, sums, old):
    """The recipe with the pins, version, release and checksums replaced."""
    # Each assignment keeps whichever style the recipe already used, so the diff
    # stays one line per field.
    text = re.sub(r'^pkgver=?("[^"]*"|\S+)$', 'pkgver=' + version,
                  text, count=1, flags=re.MULTILINE)
    text = re.sub(r'^pkgrel=?("[^"]*"|\S+)$', 'pkgrel=' + str(release),
                  text, count=1, flags=re.MULTILINE)
    for name, value in pins.items():
        text = re.sub(r'^' + name + r'=?("[^"]*"|\S+)$',
                      name + '="' + value + '"', text, count=1,
                      flags=re.MULTILINE)
    # The three archives this script owns are named after the pin they belong to
    # - the tag, or the submodule commit - so a bump renames the line as well as
    # rewrites the hash. The `source=` lines need nothing: they are built from
    # $pkgver and $_magpie_commit, which were just rewritten. Matching the old
    # name rather than the new one is what makes this a rename instead of a
    # stale line plus a missing one, and it leaves the distfiles this script does
    # not own - the vendored nvtop archive and the PGO scripts - untouched.
    for new_name, sha512 in sums.items():
        old_name = {
            "mission-center-v" + version + ".tar.gz":
                "mission-center-v" + old["pkgver"] + ".tar.gz",
            "gng-" + pins["_magpie_commit"] + ".tar.gz":
                "gng-" + old["_magpie_commit"] + ".tar.gz",
            "app-detection-" + pins["_rummage_commit"] + ".tar.gz":
                "app-detection-" + old["_rummage_commit"] + ".tar.gz",
        }.get(new_name, new_name)
        if not re.search(r'^[0-9a-f]{128}(\s+)' + re.escape(old_name) + r'$',
                         text, re.MULTILINE):
            raise RuntimeError("no sha512sums entry for " + old_name)
        text = re.sub(r'^[0-9a-f]{128}(\s+)' + re.escape(old_name) + r'$',
                      sha512 + r"\1" + new_name, text, count=1,
                      flags=re.MULTILINE)
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="fail instead of writing when the recipe is stale")
    args = parser.parse_args()

    try:
        old = read_recipe()[0]
        check_vendored(old)
        tag = latest_release()
        version = tag[1:]
        url = ORIGIN + "/-/archive/" + tag + "/mission-center-" + tag + ".tar.gz"
        blob = fetch(url)
        sha512 = hashlib.sha512(blob).hexdigest()
        if archive_root(blob, url) != "mission-center-" + tag:
            raise RuntimeError(url + ": unexpected top-level directory")
        if package_version(blob, url) != version:
            raise RuntimeError(url + ": Cargo.toml disagrees with the tag " + tag)
        pins = {}
        sums = {"mission-center-" + tag + ".tar.gz": sha512}
        parent = (ORIGIN, tag)
        for name, (repository, prefix, path) in SUBMODULES.items():
            commit = tree_pin(parent[0], parent[1], path)
            archive_url = (repository + "/-/archive/" + commit + "/" + prefix +
                           "-" + commit + ".tar.gz")
            child = fetch(archive_url)
            sums[prefix + "-" + commit + ".tar.gz"] = hashlib.sha512(child).hexdigest()
            pins[name] = commit
            if name == "_magpie_commit":
                want, _ = vendored_nvtop(child, archive_url)
                parent = (repository, commit)
                if want != old["_nvtop_commit"]:
                    raise RuntimeError(
                        "nvtop moved to " + want + "; re-vendor "
                        "aports/apps/mission-center/nvtop-" + want +
                        ".tar.gz.noauto and update _nvtop_commit and its "
                        "sha512sums entry by hand")
    except (RuntimeError, OSError, ValueError, KeyError, json.JSONDecodeError,
            subprocess.CalledProcessError, urllib.error.URLError) as error:
        print("failed     mission-center: " + str(error), file=sys.stderr)
        return 1

    # A tag that does not move the version still has to be a newer package, or a
    # partial release would publish a file apk considers equal to the one
    # already online and keep serving the old build.
    release = 0 if version != old["pkgver"] else int(old["pkgrel"]) + 1
    # Only the archives this script owns take part in the comparison; the PGO
    # scripts and the vendored distfile are checksummed by the recipe but are
    # not upstream-release state, and would otherwise look "stale" forever.
    ours = dict((name, old["sums"].get(name)) for name in sums)
    if (version == old["pkgver"] and sums == ours
            and pins == {k: old[k] for k in pins}):
        print("current    mission-center " + version + "-r" + old["pkgrel"])
        return 0

    delta = []
    if pins.get("_magpie_commit") != old["_magpie_commit"]:
        delta.append("_magpie_commit")
    if pins.get("_rummage_commit") != old["_rummage_commit"]:
        delta.append("_rummage_commit")
    if version != old["pkgver"]:
        delta.append("pkgver")
    if release != int(old["pkgrel"]):
        delta.append("pkgrel")
    if any(sums[name] != old["sums"].get(name) for name in sums):
        delta.append("sha512sums")
    print("updated    mission-center %s-r%s -> %s-r%s (%s)"
          % (old["pkgver"], old["pkgrel"], version, release, ", ".join(delta)))
    if args.check:
        return 1

    text = rewrite(read_recipe()[1], pins, version, release, sums, old)
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
