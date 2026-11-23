#!/usr/bin/env python3
"""Decide what the CI workflow builds and key its caches.

Writes GitHub Actions outputs:

  packages  one entry per (architecture, package) build shard
  full      "true" when every architecture was built, i.e. whether the result
            may replace the published repository
  scoped    "true" when only some packages were built, so the publish job has
            to carry the rest over from the published repository
  expect    comma separated package names a scoped run must find in the final
            repository, passed to build.py --expect-packages
  release   "true" when the event is a version tag
  epoch     SOURCE_DATE_EPOCH for reproducible APK and APKINDEX timestamps
  cache     digest of everything that changes dependency resolution
  repository  the path under the Pages root that the repository is served from

A tag, a manual run or the weekly schedule builds every architecture. Anything
else - a pull request, or a push to the branch the workflow watches - builds
x86_64 only, which keeps the common change cheap while still exercising the full
recipe and index pipeline. `full` says which case a run is, and `publish` gates
on it rather than keeping its own copy of the rule: publishing replaces the
entire repository, so a run that built one architecture must not deploy one
architecture and silently drop the rest.

`PACKAGES` narrows the build to the named packages, which is how an auto-update
release rebuilds only what it bumped. The publish job then fills the gaps from
the repository already online, so `scoped` tells it to do that and `expect`
tells it which names must be present either way.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# uflinux-files and mission-center are not Craft apps and are not version-locked
# in apps.lock.json: their pins live in their own recipes, which
# dependency_digest() already hashes.
EXTRA_PACKAGES = ["mission-center", "uflinux-files"]


def package_names(apps):
    return sorted([*apps, *EXTRA_PACKAGES])


def selected_packages(apps, wanted):
    """The packages to build, narrowed by an explicit request if there was one.

    An auto-update release names the packages it bumped so the run does not
    rebuild everything else; a name that is not a recipe is an error rather than
    a silently ignored typo, because the run would then publish a repository
    that quietly lacks the new version.
    """
    all_packages = package_names(apps)
    if not wanted:
        return all_packages
    requested = [name for name in wanted.replace(" ", "").split(",") if name]
    unknown = [name for name in requested if name not in all_packages]
    if unknown:
        raise ValueError("Unknown package(s): " + " ".join(unknown))
    return sorted(set(requested))


def configuration(arches, apps, event, ref, wanted=""):
    release = event == "push" and ref.startswith("refs/tags/v")
    if release and not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", ref[len("refs/tags/"):]):
        raise ValueError("Tag must be v followed by a numeric version, e.g. v3.24.1")
    if event not in {"push", "pull_request", "workflow_dispatch", "merge_group", "schedule"}:
        raise ValueError("Unsupported event " + repr(event))
    # Only a run that is meant to be published is worth building on every
    # architecture; a push to the watched branch is a cheap x86_64 smoke test.
    full = release or event in {"workflow_dispatch", "schedule"}
    selected = list(arches) if full else ["x86_64"]
    unknown = [arch for arch in selected if arch not in arches]
    if unknown:
        raise ValueError("architectures.json is missing " + " ".join(unknown))
    built = selected_packages(apps, wanted)
    return {
        # The index job needs no matrix of its own: it indexes whatever
        # architectures collect-packages.py found in the artifact spool.
        "packages": {"include": [
            {
                "arch": a,
                "runner": arches[a]["runner"],
                "package": p,
                # Decides one thing: whether the shard installs qemu-user. This
                # x86 runs directly on the x86_64 runner. ARM64 runners use
                # qemu-arm for armv7 because AArch32 support is optional.
                "emulated": "true" if a not in {"x86", "x86_64", "aarch64"} else "false",
            }
            for a in selected
            for p in built
        ]},
        "release": str(release).lower(),
        "full": str(full).lower(),
        "scoped": str(bool(wanted)).lower(),
        "expect": ",".join(built),
    }


def dependency_digest(arches, repository):
    # Everything that can change which dependencies are installed, or which
    # bytes a recipe feeds to the compiler, but not the source checksums:
    # bumping a version must still reuse the (expensive) dependency cache.
    digest = hashlib.sha256()
    digest.update(json.dumps({"arches": arches, "repository": repository}, sort_keys=True).encode())
    for recipe in sorted((ROOT / "aports/apps").glob("*/APKBUILD")):
        digest.update(recipe.read_bytes().split(b"sha512sums=")[0])
    digest.update((ROOT / "aports/apps/craft-common.sh").read_bytes())
    return digest.hexdigest()


def main():
    arches = json.loads((ROOT / "config/architectures.json").read_text())
    repository = json.loads((ROOT / "config/repository.json").read_text())
    apps = json.loads((ROOT / "config/apps.lock.json").read_text())["apps"]
    epoch = subprocess.check_output(["git", "log", "-1", "--format=%ct"], text=True).strip()
    outputs = {
        **configuration(arches, apps,
                        os.environ.get("EVENT", ""),
                        os.environ.get("REF", ""),
                        os.environ.get("PACKAGES", "")),
        "epoch": epoch,
        "cache": dependency_digest(arches, repository),
        # The publish job's summary quotes the path the repository is served
        # under; emitting it here keeps the workflow from hardcoding a third
        # copy of a layout rule that config/repository.json owns.
        "repository": repository["repository_path"],
    }
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        for key, value in outputs.items():
            output.write(f"{key}={json.dumps(value) if isinstance(value, dict) else value}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as failure:
        # The workflow's `on.push.tags: v*` accepts a tag this script rejects, and
        # a traceback buries the reason. Annotate it instead.
        print("::error::ci-matrix.py: " + str(failure), file=sys.stderr)
        sys.exit(1)
