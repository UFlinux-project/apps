#!/usr/bin/env python3
"""Build uflinux3-apps APKs inside an Alpine chroot and index them.

Runs on a disposable Linux build host as root. The host only needs tar, curl
and (for a foreign architecture) qemu-user binfmt registration; every build
step happens inside a chroot unpacked from a locked Alpine minirootfs, so the
resulting APKs match what Alpine's own builders produce.

Stages
  packages  unpack the rootfs, install makedepends, run abuild, collect APKs
  index     generate and sign APKINDEX.tar.gz for every architecture found
  all       packages followed by index

Layout produced under --outdir:
  packages/<arch>/*.apk   freshly built packages, one directory per architecture
  repository/<arch>/      signed repository (APKs plus APKINDEX.tar.gz)
  repository/<key id>     the public signing key to drop in /etc/apk/keys
  pages/                  the same repository under repository_path/, plus a
                          landing page: this is what GitHub Pages serves
"""

import argparse
import contextlib
import hashlib
import io
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((PROJECT / "config/repository.json").read_text())
ARCHES = json.loads((PROJECT / "config/architectures.json").read_text())
APPS = json.loads((PROJECT / "config/apps.lock.json").read_text())["apps"]
PACKAGES = sorted([*APPS, "uflinux-files"])

# Which target architectures a host can execute without emulation, keyed by the
# `uname -m` of the host. Every architecture we build is 64-bit and its Alpine
# name matches `uname -m` exactly, so the table is nearly a tautology; it stays
# explicit because getting it wrong in the loose direction means the build
# proceeds without translation and dies on an exec format error. (A 32-bit ARM
# host would be the exception: `uname -m` says `armv7l` where Alpine says
# `armv7`.)
NATIVE = {
    "x86_64": {"x86_64"},
    "aarch64": {"aarch64"},
    "ppc64le": {"ppc64le"},
    "riscv64": {"riscv64"},
}

# Alpine arch names whose toolchain is a 32-bit process. See the LTO comment in
# stage_packages(); no architecture listed here is currently built.
ARCH32 = {"x86", "armv7"}

# Free space a Rust release build plus an Alpine toolchain comfortably needs.
MINIMUM_FREE_BYTES = 10 * 1024**3

# Concurrent rustc processes for a native aarch64 build. See the comment in
# stage_packages(); an explicit CARGO_BUILD_JOBS in the environment wins.
ARM64_BUILD_JOBS = "2"


def run(args, **kwargs):
    print("+", shlex.join(str(a) for a in args), flush=True)
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def memory_report():
    """One line describing the memory this build starts with.

    A build that needs more than the machine's RAM and swap does not fail with a
    message about memory: on a hosted runner it takes the whole virtual machine
    with it, and the job reports only that the runner received a shutdown signal.
    Printing what the build had before it starts is the cheapest way to tell that
    kind of failure apart from a build that genuinely failed.
    """
    try:
        lines = Path("/proc/meminfo").read_text().splitlines()
    except OSError:
        return "memory: /proc/meminfo unavailable"

    def field(name):
        for line in lines:
            if line.startswith(name + ":"):
                return int(line.split()[1]) * 1024
        return 0

    gib = 1024**3
    total, available = field("MemTotal"), field("MemAvailable")
    swap, swap_free = field("SwapTotal"), field("SwapFree")
    limit = "none"
    # The container's own ceiling, if it has one: what a build can actually use
    # can be far below what the machine has.
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            value = Path(path).read_text().strip()
        except OSError:
            continue
        limit = "unlimited" if value == "max" else f"{int(value) / gib:.1f} GiB"
        break
    return (f"memory: {total / gib:.1f} GiB total, {available / gib:.1f} GiB available, "
            f"swap {swap / gib:.1f} GiB ({(swap - swap_free) / gib:.1f} GiB used), "
            f"cgroup limit {limit}")


def digest(path, algorithm="sha512"):
    hasher = hashlib.new(algorithm)
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


# apk's version grammar, from apk-tools src/version.c:
#   digit{.digit}...{letter}{_suffix{#}}...{~hex}{-r#}
# The numbers are apk's own token enum values, used only to reject a version
# whose parts appear in an order apk would not accept.
# apk splits a version-release string into tokens; the ranks mirror the
# TOKEN_* enum in apk-tools src/version.c, so a token list can be compared with
# the same lockstep walk apk itself uses.
PART = {
    "initial_digit": 0,
    "digit": 1,
    "letter": 2,
    "suffix": 3,
    "suffix_no": 4,
    "commit_hash": 5,
    "revision_no": 6,
    "end": 7,
}

# apk's suffix enum: the empty name appears twice, and the gap between _rc and
# _cvs is the plain release, which is why a pre-release ranks below a version
# that simply ends.
SUFFIX_RANK = {
    "": 0,
    "alpha": 1,
    "beta": 2,
    "pre": 3,
    "rc": 4,
    "none": 5,
    "cvs": 6,
    "svn": 7,
    "git": 8,
    "hg": 9,
    "p": 10,
}

DIGIT_LIKE = {PART["initial_digit"], PART["digit"], PART["suffix_no"],
              PART["revision_no"]}
LOWER = set("abcdefghijklmnopqrstuvwxyz")


def version_tokens(version):
    """Tokenise an apk version-release string the way apk-tools does.

    Returns ``(rank, number, raw, suffix)`` tuples, where ``rank`` mirrors the
    TOKEN_* enum, ``number`` is the numeric value of a digit-like token (None
    when zero padded, which apk then compares as text), and ``suffix`` is the
    rank of a version suffix. Raises ValueError for anything apk would reject,
    so a malformed pin fails loudly instead of sorting wrongly.
    """
    tokens = []

    def emit(kind, text, suffix=None):
        rank = PART[kind]
        number = None
        if rank in DIGIT_LIKE and text[0] != "0":
            number = int(text)
        tokens.append((rank, number, text, suffix))
        return rank

    def take_digits(text):
        match = re.match(r"[0-9]+", text)
        if not match:
            raise ValueError("malformed apk version: " + version)
        return match.group(), text[match.end():]

    group, rest = take_digits(version)
    state = emit("initial_digit", group)
    while rest:
        head, rest = rest[0], rest[1:]
        if head in LOWER:
            # A letter token is one character wide, and apk only accepts a
            # lowercase one; anything else ends the walk as an invalid token.
            if state > PART["digit"]:
                raise ValueError("malformed apk version: " + version)
            state = emit("letter", head)
        elif head in ".0123456789":
            if state <= PART["digit"]:
                kind = "digit"
            elif state in (PART["suffix"], PART["suffix_no"]):
                kind = "suffix_no"
            else:
                raise ValueError("malformed apk version: " + version)
            group, rest = take_digits(rest if head == "." else head + rest)
            state = emit(kind, group)
        elif head == "_":
            if state > PART["suffix_no"]:
                raise ValueError("malformed apk version: " + version)
            match = re.match(r"[a-z]+", rest)
            if not match:
                raise ValueError("malformed apk version: " + version)
            group, rest = match.group(), rest[match.end():]
            if group not in SUFFIX_RANK or group == "none":
                raise ValueError("unknown apk version suffix: _" + group)
            state = emit("suffix", group, SUFFIX_RANK[group])
        elif head == "~":
            if state >= PART["commit_hash"]:
                raise ValueError("malformed apk version: " + version)
            match = re.match(r"[0-9a-f]+", rest)
            if not match:
                raise ValueError("malformed apk version: " + version)
            group, rest = match.group(), rest[match.end():]
            state = emit("commit_hash", group)
        elif head == "-":
            if state >= PART["revision_no"] or not rest.startswith("r"):
                raise ValueError("malformed apk version: " + version)
            group, rest = take_digits(rest[1:])
            state = emit("revision_no", group)
        else:
            raise ValueError("malformed apk version: " + version)
    return tokens


def _token_cmp(left, right):
    """apk's token_cmp: how one pair of same-kind tokens orders."""
    rank, number, raw, suffix = left
    other_number, other_raw, other_suffix = right[1], right[2], right[3]
    if rank in DIGIT_LIKE and number is not None and other_number is not None:
        return (number > other_number) - (number < other_number)
    if rank == PART["suffix"]:
        return (suffix > other_suffix) - (suffix < other_suffix)
    if rank == PART["letter"]:
        # apk compares only the first character, and a letter token is one
        # character wide anyway.
        return (raw > other_raw) - (raw < other_raw)
    return (raw > other_raw) - (raw < other_raw)


def version_compare(left, right):
    """Compare two apk version-release strings: negative, zero, or positive.

    A faithful port of apk_version_compare(): walk both token lists in lockstep
    and stop at the first difference. When one version runs out, the longer one
    wins unless its next token is a pre-release suffix (_alpha, _beta, _pre,
    _rc), which ranks below the plain release.
    """
    a = version_tokens(left)
    b = version_tokens(right)
    for index in range(min(len(a), len(b))):
        if a[index][0] != b[index][0]:
            break
        result = _token_cmp(a[index], b[index])
        if result:
            return result
    else:
        index = min(len(a), len(b))
    tail_a = a[index] if index < len(a) else (PART["end"], None, "", None)
    tail_b = b[index] if index < len(b) else (PART["end"], None, "", None)
    if tail_a[0] == tail_b[0]:
        return 0
    if tail_a[0] == PART["suffix"] and tail_a[3] < SUFFIX_RANK["none"]:
        return -1
    if tail_b[0] == PART["suffix"] and tail_b[3] < SUFFIX_RANK["none"]:
        return 1
    return -1 if tail_a[0] > tail_b[0] else 1


def apk_metadata(path):
    """(name, version, origin) from an APK's own .PKGINFO.

    The tar container is uncompressed and .PKGINFO is stored plain, so this
    needs no apk tooling. `origin` is the recipe that produced the file, which
    is what identifies all of a package's subpackages as one unit.
    """
    with tarfile.open(path, mode="r:*") as archive:
        member = archive.extractfile(".PKGINFO")
        if member is None:
            raise RuntimeError(str(path) + " contains no .PKGINFO")
        fields = {}
        for line in member.read().decode("utf-8", "replace").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                fields[key.strip()] = value.strip()
    missing = [f for f in ("pkgname", "pkgver", "origin") if not fields.get(f)]
    if missing:
        raise RuntimeError(f"{path}: .PKGINFO has no {' nor '.join(missing)}")
    return fields["pkgname"], fields["pkgver"], fields["origin"]


def index_records(blob):
    """Parse the APKINDEX member of an APKINDEX.tar.gz into one dict per package.

    Keys are apk's single-letter field names; the ones relied on here are P
    (name), V (version-release), A (architecture), o (origin) and C (checksum).
    """
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as archive:
        member = archive.extractfile("APKINDEX")
        if member is None:
            return
        record = {}
        for line in member.read().decode("utf-8", "replace").splitlines():
            key, sep, value = line.partition(":")
            if sep and len(key) == 1:
                record[key] = value
            elif not line and record:
                yield record
                record = {}
        if record:
            yield record


class Build:
    def __init__(self, args):
        self.args = args
        self.arch = args.arch
        self.root = Path(args.workdir) / "root"
        self.cache = Path(args.cache).resolve()
        self.out = Path(args.outdir).resolve()
        self.packages = self.out / "packages" / self.arch
        self.repository = self.out / "repository"
        # What GitHub Pages serves: the signed repository under
        # repository_path/, the signing key next to it, and a landing page.
        self.pages = self.out / "pages"
        self.key = CONFIG["signing_key_id"]
        # The published repository to complete a partial release from. Empty
        # means "nothing to carry over", which is what a local run wants.
        self.baseline = getattr(args, "baseline_url", "").strip().rstrip("/")
        self.expect = [p for p in getattr(args, "expect_packages", "").split(",") if p]
        self.epoch = os.environ.get("SOURCE_DATE_EPOCH", "")
        self.mounts = []
        self.env = {
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": "/root",
            "TERM": "dumb",
            "TZ": "UTC",
            "LC_ALL": "C.UTF-8",
            # abuild-sign accepts only RSA (SHA-1) and RSA256 (SHA-256) and
            # files the signature as .SIGN.<that>.<key id>. apk-tools 3.0.9 does
            # verify an EC signature, but it derives the digest from that member
            # name: measured on Alpine 3.24, SHA-256 is accepted and SHA-1,
            # SHA-384 and SHA-512 are all rejected as UNTRUSTED. So an EC key
            # has to be signed with SHA-256 under this misleading name, and
            # "RSA256" here means "ECDSA with a SHA-256 digest", not the key.
            "sigtype": "RSA256",
            # Keep downloads and Cargo state out of the recipe directory so an
            # Actions cache can restore them between runs.
            "SRCDEST": "/cache/distfiles",
            "CARGO_HOME": "/cache/cargo",
            "CARGO_TARGET_DIR": "/cache/target",
            "REPODEST": "/home/builder/packages",
        }
        if self.epoch:
            self.env["SOURCE_DATE_EPOCH"] = self.epoch

    # -- chroot plumbing ----------------------------------------------------

    def chroot(self, *command, **kwargs):
        env = dict(self.env, **kwargs.pop("extra_env", {}))
        argv = ["chroot", str(self.root), "/usr/bin/env", "-i",
                *[f"{key}={value}" for key, value in env.items()], *command]
        return run(argv, **kwargs)

    def write(self, name, content, mode=0o644):
        path = self.root / name.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(mode)
        return path

    def bind(self, source, destination):
        target = self.root / destination.lstrip("/")
        if Path(source).is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch(exist_ok=True)
        run(["mount", "--bind", source, target])
        self.mounts.append(target)

    @contextlib.contextmanager
    def mounted(self):
        try:
            self.bind("/dev", "dev")
            # Do not propagate nested device binds into the host's /dev when the
            # runner uses shared mount propagation.
            run(["mount", "--make-private", self.root / "dev"])
            for device in ("null", "zero", "full", "random", "urandom", "tty"):
                self.bind("/dev/" + device, "dev/" + device)
            self.bind("/dev/pts", "dev/pts")
            self.bind("/dev/shm", "dev/shm")
            (self.root / "proc").mkdir(exist_ok=True)
            run(["mount", "-t", "proc", "proc", self.root / "proc"])
            self.mounts.append(self.root / "proc")
            for name in ("apk", "cargo", "target", "distfiles"):
                host = self.cache / self.arch / name
                host.mkdir(parents=True, exist_ok=True)
                self.bind(host, "cache/" + name)
            yield
        finally:
            for path in reversed(self.mounts):
                if subprocess.run(["umount", "-l", str(path)], check=False).returncode:
                    print("warning: could not unmount", path, file=sys.stderr)
            self.mounts.clear()

    # -- inputs -------------------------------------------------------------

    def download(self, spec):
        cache = self.cache / "downloads"
        cache.mkdir(parents=True, exist_ok=True)
        path = cache / (spec["sha512"] + "-" + spec["url"].rsplit("/", 1)[1])
        if not path.exists():
            partial = path.with_suffix(".partial")
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(spec["url"], timeout=120) as response, partial.open("wb") as stream:
                        shutil.copyfileobj(response, stream)
                    if digest(partial) != spec["sha512"]:
                        raise RuntimeError("Checksum mismatch: " + spec["url"])
                    partial.replace(path)
                    break
                except OSError:
                    partial.unlink(missing_ok=True)
                    if attempt == 2:
                        raise
                    time.sleep(2 * (attempt + 1))
        elif digest(path) != spec["sha512"]:
            raise RuntimeError("Corrupt cached download: " + str(path))
        return path

    def preflight(self):
        if os.geteuid() != 0:
            raise RuntimeError("Run as root on a disposable Linux build host")
        if self.arch not in NATIVE.get(platform.machine(), set()):
            entry = next((p for p in Path("/proc/sys/fs/binfmt_misc").glob("qemu-" + ARCHES[self.arch]["qemu_user"] + "*")), None)
            if entry is None:
                raise RuntimeError("No binfmt entry for " + self.arch + "; install qemu-user-binfmt")
            information = entry.read_text()
            if "enabled" not in information or not re.search(r"^flags:.*F", information, re.M):
                raise RuntimeError("binfmt entry for " + self.arch + " is not enabled with the F (fix binary) flag")
        # The figure below is what a Craft build's target directory needs, and
        # only the build stage creates that. Indexing reads the APKs that already
        # exist and writes an index per architecture, so it must not demand the
        # same headroom - that would make "build on a big disk, index anywhere"
        # impossible and is the only thing standing between a small runner and a
        # finished repository.
        if self.args.stage in {"packages", "all"}:
            free = shutil.disk_usage(self.args.workdir).free
            if free < MINIMUM_FREE_BYTES:
                raise RuntimeError(f"Need at least {MINIMUM_FREE_BYTES // 1024**3} GiB free, found {free // 1024**3} GiB")
            print(memory_report(), flush=True)

    def unpack(self):
        marker = self.root / ".uflinux-apps-rootfs"
        fingerprint = hashlib.sha256(json.dumps(CONFIG["rootfs"][self.arch], sort_keys=True).encode()).hexdigest()
        if marker.exists():
            if marker.read_text() != fingerprint:
                raise RuntimeError("Rootfs lock changed: use a fresh --workdir")
        else:
            if self.root.exists() and any(self.root.iterdir()):
                raise RuntimeError("Incomplete rootfs: remove this build's --workdir and retry")
            archive = self.download(CONFIG["rootfs"][self.arch])
            self.root.mkdir(parents=True, exist_ok=True)
            run(["tar", "--numeric-owner", "-xpf", archive, "-C", self.root])
            marker.write_text(fingerprint)
        resolv = self.root / "etc/resolv.conf"
        resolv.unlink(missing_ok=True)
        resolv.write_bytes(Path("/etc/resolv.conf").read_bytes())
        # apk reads its download cache from this path; mounted() binds the
        # persistent host directory underneath it, so makedepends survive a
        # runner change and never need fetching twice.
        apk_cache = self.root / "etc/apk/cache"
        if apk_cache.is_dir() and not apk_cache.is_symlink():
            shutil.rmtree(apk_cache)
        elif apk_cache.is_symlink():
            apk_cache.unlink()
        apk_cache.symlink_to("/cache/apk")
        mirror, version = CONFIG["alpine"]["mirror"], CONFIG["alpine"]["version"]
        self.write("etc/apk/repositories", "".join(f"{mirror}/v{version}/{branch}\n" for branch in CONFIG["alpine"]["branches"]))
        # Running apk is also the smoke test that this architecture can be
        # executed at all: it is the first foreign binary the build runs. If the
        # NATIVE table above ever claims more than a kernel delivers, the failure
        # here is an exec format error from chroot, which says nothing about what
        # was assumed, so ask for the architecture name instead and report the
        # assumption that broke.
        try:
            reported = self.chroot("apk", "--print-arch", stdout=subprocess.PIPE).stdout.decode().strip()
        except subprocess.CalledProcessError as failure:
            raise RuntimeError(
                f"Cannot execute the {self.arch} chroot on this {platform.machine()} host "
                f"(exit {failure.returncode}). Either install and enable qemu-user-binfmt, "
                "or, if the kernel really can run this architecture, add it to NATIVE in "
                "scripts/build.py.") from failure
        if reported != self.arch:
            raise RuntimeError(f"The chroot reports {reported!r}, expected {self.arch!r}")
        project = self.root / "project"
        if project.exists():
            shutil.rmtree(project)
        shutil.copytree(PROJECT, project, ignore=shutil.ignore_patterns(".git", ".state", ".cache", "out", "__pycache__", "*.pyc"))

    # -- signing ------------------------------------------------------------

    def signing_key(self, owner=0):
        # Paths inside the chroot: openssl runs there, so it must never see a
        # host path, and abuild reads the key names from abuild.conf, which is
        # only meaningful as an in-chroot path.
        in_root = "/var/lib/uflinux-apps/keys"
        directory = self.root / in_root.lstrip("/")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # abuild-sign derives the public key name by appending ".pub" to the
        # private one, so the two have to be <key id> and <key id>.pub.
        private = directory / Path(self.key).stem
        private_in_root = in_root + "/" + Path(self.key).stem
        public = directory / self.key
        release = os.environ.get("APK_SIGNING_PRIVATE_KEY", "")
        if release:
            expected = os.environ.get("APK_SIGNING_PUBLIC_KEY", "").replace("\r\n", "\n").strip()
            if not expected:
                raise RuntimeError("Signing needs both APK_SIGNING_PRIVATE_KEY and APK_SIGNING_PUBLIC_KEY")
            # Never reuse an existing inode: an unprivileged child could still
            # hold a descriptor to the previous key.
            private.unlink(missing_ok=True)
            fd = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(release.strip() + "\n")
            # -pubout is not optional: `openssl pkey -in` prints the *private*
            # key by default, which can never equal a public key, so without it
            # this check rejects a perfectly good keypair. `pkey` is also what
            # makes this check work for any algorithm, EC included.
            actual = self.chroot("openssl", "pkey", "-pubout", "-in", private_in_root,
                                 stdout=subprocess.PIPE).stdout.decode().strip()
            if actual != expected:
                raise RuntimeError("The release public and private signing keys do not match")
            public.write_text(expected + "\n")
        elif not private.exists():
            # P-384. abuild-keygen cannot generate this key, because it only ever
            # runs `openssl genrsa`, so the throwaway has to be made here. The
            # public half goes through `pkey -pubout` rather than `ec`, so the
            # file is a SubjectPublicKeyInfo like any other, which is the one
            # shape apk reads from /etc/apk/keys.
            self.chroot("openssl", "ecparam", "-name", "secp384r1", "-genkey",
                        "-noout", "-out", private_in_root)
            result = self.chroot("openssl", "pkey", "-in", private_in_root,
                                 "-pubout", stdout=subprocess.PIPE)
            public.write_bytes(result.stdout)
        os.chown(directory, owner, owner)
        os.chown(private, owner, owner)
        private.chmod(0o600)
        public.chmod(0o644)
        shutil.copy2(public, self.root / "etc/apk/keys" / self.key)
        self.env.update(PACKAGER_PRIVKEY=private_in_root,
                        PACKAGER_PUBKEY=in_root + "/" + self.key)
        return public

    # -- stages -------------------------------------------------------------

    def stage_packages(self):
        self.unpack()
        with self.mounted():
            self.chroot("apk", "add", "--no-progress", "alpine-keys", "abuild", "alpine-sdk",
                        "git", "openssl", "shadow", "sudo")
            self.chroot("sh", "-ec", "id builder >/dev/null 2>&1 || adduser -D -u 1000 builder; addgroup builder abuild 2>/dev/null || true")
            self.signing_key(owner=1000)
            source = self.root / "project/aports"
            for path in (source, *source.rglob("*")):
                os.chown(path, 1000, 1000)
            # A restored Actions cache belongs to the host user, not to uid 1000.
            self.chroot("chown", "-R", "builder:builder", "/cache/cargo", "/cache/target", "/cache/distfiles")
            self.chroot("install", "-d", "-o", "builder", "-g", "builder", "/home/builder/packages", "/home/builder/.abuild")
            self.write("home/builder/.abuild/abuild.conf",
                       f'PACKAGER="{CONFIG["packager"]}"\n'
                       f'PACKAGER_PRIVKEY="{self.env["PACKAGER_PRIVKEY"]}"\n'
                       f'PACKAGER_PUBKEY="/var/lib/uflinux-apps/keys/{self.key}"\n')
            jobs = os.environ.get("CARGO_BUILD_JOBS", "")
            # Two independent reasons to override the release profile, and the
            # Craft profiles ask for thin LTO with a single codegen unit, so the
            # last link of the biggest binary is one single-threaded pass over
            # the whole program.
            #
            # Under qemu-user, the compile job count and that link are the two
            # places where an emulated build stops being a merely slower native
            # one. One job keeps instruction translation inside the runner's
            # memory, and the link is measured at over half an hour natively -
            # the phase most likely to run past the runner's time limit, and the
            # one `jobs` cannot shorten. Turning LTO off makes it parallel and
            # far shorter, for a slightly larger and marginally slower binary: a
            # trade worth making where the alternative is that the architecture
            # is not published at all. A native 64-bit build gets upstream's own
            # profile, and only aarch64 gets anything overridden at all - the
            # job count, not the profile.
            #
            # A 32-bit target needs LTO off for a second, harder reason: a
            # 32-bit rustc cannot address that link at all, however many cores
            # run it, so the override is about addressing rather than speed.
            # ARCH32 is currently empty of built architectures - `x86` and
            # `armv7` are not in config/architectures.json - so this branch is
            # dormant. Building one would also need a rootfs entry in
            # config/repository.json and, for a real 32-bit host, the `armv7l`
            # name difference noted by NATIVE above.
            emulated = self.arch not in NATIVE.get(platform.machine(), set())
            cargo = {} if not (emulated or self.arch in ARCH32) else {
                "CARGO_PROFILE_RELEASE_LTO": "false",
                "CARGO_PROFILE_RELEASE_CODEGEN_UNITS": "16",
            }
            if emulated:
                cargo["CARGO_BUILD_JOBS"] = "1"
            # aarch64 is native, so it keeps upstream's profile - and pays for
            # that in memory. The ARM runner is the same 4 vCPU / 16 GB machine
            # as the x86_64 one, but effectcraft, lightcraft and vectorcraft
            # have now lost the runner at the same point in the build, twice,
            # from a cold cache on a fresh VM each time. GitHub documents that a
            # workload which exhausts RAM and swap fails exactly this way: the
            # job ends with "the runner has received a shutdown signal" and exit
            # code 143 instead of an OOM report, because the VM stops responding
            # before the kernel can log one. The workflow adds a swapfile for
            # this architecture; limiting concurrent rustc is the part that
            # lowers the peak rather than absorbing it. Nothing about the
            # package changes, only the time it takes, and the slowest
            # successful aarch64 shard already runs for 49 minutes of the 350
            # the job is allowed.
            if self.arch == "aarch64":
                cargo.setdefault("CARGO_BUILD_JOBS", ARM64_BUILD_JOBS)
            if jobs:
                cargo["CARGO_BUILD_JOBS"] = jobs
            # BOLT executes instrumented binaries and requires a native worker.
            # Build its Alpine tool package locally before resolving Files'
            # dependencies; it is not available in the Alpine repositories.
            cargo["UFLINUX_FILES_PGO"] = "0" if emulated else "1"
            if "uflinux-files" in self.args.selected() and not emulated and self.arch in ("x86_64", "aarch64"):
                directory = "cd /project/aports/build-tools/llvm22-bolt && "
                self.chroot("sh", "-ec", directory + "abuild -F deps", extra_env={"SUDO_APK": "apk"})
                try:
                    self.chroot("su", "-s", "/bin/sh", "builder", "-c", directory + "abuild", extra_env=cargo)
                    self.chroot("sh", "-ec", "apk add /home/builder/packages/build-tools/*/llvm22-bolt-*.apk")
                finally:
                    self.chroot("sh", "-ec", directory + "abuild -F undeps", extra_env={"SUDO_APK": "apk"})
            try:
                for package in self.args.selected():
                    directory = f"cd /project/aports/apps/{package} && "
                    # qemu-user cannot elevate through a setuid binary, so resolve
                    # dependencies as root and compile unprivileged. Plain abuild
                    # still verifies that every makedepend is installed.
                    self.chroot("sh", "-ec", directory + "abuild -F deps", extra_env={"SUDO_APK": "apk", "UFLINUX_FILES_PGO": cargo["UFLINUX_FILES_PGO"]})
                    try:
                        self.chroot("su", "-s", "/bin/sh", "builder", "-c", directory + "abuild",
                                    extra_env=cargo)
                    finally:
                        self.chroot("sh", "-ec", directory + "abuild -F undeps", extra_env={"SUDO_APK": "apk"})
            finally:
                # The cache is uploaded by the Actions agent, which is neither root
                # nor builder, and the files above are the ones the build itself
                # created. Cargo leaves parts of the registry unreadable to anyone
                # but their owner, tar then reports "Cannot open: Permission
                # denied", and the whole upload is lost as a warning in the log -
                # so the next run rebuilds from nothing and the cache costs the
                # time it was supposed to save. Readable directories and files are
                # all the upload needs; the chown above is what the build needs.
                # This runs whatever the build did, because a cache from a failed
                # build is the one that most wants to be resumed.
                try:
                    self.chroot("chmod", "-R", "a+rX", "/cache/cargo", "/cache/target",
                                "/cache/distfiles", "/cache/apk")
                except subprocess.CalledProcessError as error:
                    print("warning: could not make the build cache readable:", error)
            # Build tools live in their own abuild repository and must not be
            # copied into the public apps repository.
            built = self.root / "home/builder/packages/apps"
            if self.packages.exists():
                shutil.rmtree(self.packages)
            self.packages.mkdir(parents=True, exist_ok=True)
            found = 0
            for apk in sorted(built.rglob("*.apk")):
                if apk.parent.name in (self.arch, "noarch"):
                    shutil.copy2(apk, self.packages / apk.name)
                    found += 1
            if not found:
                raise RuntimeError("abuild produced no packages for " + self.arch)
            print(f"built {found} packages into {self.packages}")

    def landing_page(self, architectures):
        origin = CONFIG["origin"]
        # The Pages workflow passes the URL this site is served from; the
        # fallback keeps the page useful for a local "build.py --stage index".
        base = os.environ.get("PAGES_BASE_URL", "").strip().rstrip("/")
        repo = base + "/" + CONFIG["repository_path"] if base else \
            "https://OWNER.github.io/uflinux3-apps/" + CONFIG["repository_path"]
        lines = [
            "<!doctype html>",
            '<meta charset="utf-8">',
            "<title>" + origin + "</title>",
            "<h1>" + origin + "</h1>",
            "<p>Third-party applications for <a href=\"https://github.com/"
            "UFlinux-project/uflinux3\">UFLinux</a>, built against Alpine "
            + CONFIG["alpine"]["version"] + ".</p>",
            # apk-tools reads top-level *.list files from /etc/apk/repositories.d
            # but no package ships that directory, so it has to be created first.
            # The URL is piped through tee rather than echoed inside the elevated
            # shell, because doas resets the environment and would lose $REPO.
            "<p>On a UFLinux system:</p>",
            "<pre>REPO=\"" + repo + "\"" + ("" if base else "   # this page, minus index.html"),
            "doas wget -qO /etc/apk/keys/" + self.key + " \"$REPO/" + self.key + "\"",
            "doas mkdir -p /etc/apk/repositories.d",
            "printf '%s\\n' \"$REPO\" | doas tee /etc/apk/repositories.d/"
            + origin + ".list &gt;/dev/null",
            "doas apk update</pre>",
            "<p>Indexed for: " + ", ".join(architectures) + ".</p>",
        ]
        (self.pages / "index.html").write_text("\n".join(lines) + "\n")

    def prune_duplicates(self, directory):
        """Drop every but the newest APK of each package name; return what is left.

        Two APKs of one name in one architecture directory make `apk index`
        describe both, and apk then picks the newer at install time while the
        repository ships both files. That happens when a carried-over package is
        also present in the spool at an older version, so it is resolved here in
        favour of the newest.
        """
        newest = {}
        for apk in directory.glob("*.apk"):
            name, version, _ = apk_metadata(apk)
            if name not in newest or version_compare(version, newest[name][0]) > 0:
                newest[name] = (version, apk)
        keep = {apk.name for _, apk in newest.values()}
        for apk in directory.glob("*.apk"):
            if apk.name not in keep:
                print("removing superseded " + apk.name)
                apk.unlink()
        return sorted(directory.glob("*.apk"))

    def carry_over(self, directory):
        """Add the packages this run did not build from the published repository.

        A release that builds a subset, or one where a shard failed, would
        otherwise publish a repository missing whatever it did not rebuild. The
        live Pages deployment is the baseline: its APKINDEX says what is
        published, and the APKs it names are fetched back into the spool so that
        `apk index` describes them again. Only entries whose origin is a recipe
        this repository builds are adopted, so a third-party APK sharing the
        directory can never be picked up, and everything this run did build is
        replaced by the fresh APK rather than kept beside it.

        apk verifies each APK's own signature before indexing it, so this only
        works while the published repository was signed with the current key.
        After a key rotation, run a full release before relying on this.

        Returns the set of origins now present in the directory.
        """
        arch = directory.name
        origins = set()
        apks = sorted(directory.glob("*.apk"))
        for apk in apks:
            origins.add(apk_metadata(apk)[2])
        if not self.baseline:
            return origins
        url = "/".join((self.baseline, CONFIG["repository_path"], arch,
                        "APKINDEX.tar.gz"))
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                published = response.read()
        except urllib.error.HTTPError as failure:
            # A 404 is the first release for this architecture, or the first one
            # after it was dropped from the matrix: nothing to keep.
            if failure.code != 404:
                raise RuntimeError(f"cannot read {url}: HTTP {failure.code}")
            print(f"note: nothing published for {arch} yet, nothing carried over")
            return origins
        except urllib.error.URLError as failure:
            raise RuntimeError(f"cannot read {url}: {failure.reason}")
        # One entry per package name, newest first: a published repository holds
        # one APK per name, but the index is not guaranteed to say so.
        wanted = {}
        for record in index_records(published):
            name, version, origin = (record.get("P", ""), record.get("V", ""),
                                     record.get("o", ""))
            if record.get("A") != arch or origin not in PACKAGES:
                continue
            if origin in origins or not re.fullmatch(r"[\w.+~-]+", name):
                continue
            if not re.fullmatch(r"[\w.+~-]+", version):
                continue
            if name not in wanted or version_compare(version, wanted[name][0]) > 0:
                wanted[name] = (version, origin)
        carried = set()
        for name, (version, origin) in sorted(wanted.items()):
            filename = f"{name}-{version}.apk"
            source = "/".join((self.baseline, CONFIG["repository_path"], arch, filename))
            destination = directory / filename
            try:
                with urllib.request.urlopen(source, timeout=300) as response:
                    destination.write_bytes(response.read())
            except urllib.error.HTTPError as failure:
                # The index promised the package but the file is gone. Skipping
                # it keeps the rest of the release intact; --expect-packages
                # turns it into a failure when the package was supposed to be
                # rebuilt anyway.
                print(f"::warning::{filename} is indexed but not published "
                      f"(HTTP {failure.code}); dropped from this release")
                continue
            except urllib.error.URLError as failure:
                raise RuntimeError(f"cannot download {source}: {failure.reason}")
            origins.add(origin)
            carried.add(origin)
            print("carried " + filename)
        if carried:
            print(f"carried {len(carried)} packages ({len(wanted)} files) "
                  f"previously published for {arch}")
        return origins

    def stage_index(self):
        self.unpack()
        with self.mounted():
            self.chroot("apk", "add", "--no-progress", "alpine-keys", "abuild", "apk-tools", "openssl")
            self.signing_key()
            shutil.rmtree(self.repository, ignore_errors=True)
            self.repository.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(self.pages, ignore_errors=True)
            (self.pages / CONFIG["repository_path"]).mkdir(parents=True)
            architectures = sorted(p for p in (self.out / "packages").iterdir() if p.is_dir()) if (self.out / "packages").is_dir() else []
            if not architectures:
                raise RuntimeError("No built packages to index under " + str(self.out / "packages"))
            if self.expect and not self.baseline:
                # A scoped run promises every package it did not rebuild is
                # already published, so it cannot check that promise without a
                # baseline. Better to say so loudly than to publish a partial
                # repository and call it a release.
                raise RuntimeError("--expect-packages needs a baseline: set "
                                   "PAGES_BASE_URL or pass --baseline-url")
            # Only the architectures that really produced an index are advertised
            # on the landing page: an empty directory is skipped below, and
            # promising one the site does not contain would be wrong.
            indexed = []
            for directory in architectures:
                origins = self.carry_over(directory)
                apks = self.prune_duplicates(directory)
                if not apks:
                    continue
                if self.expect:
                    missing = sorted(set(self.expect) - origins)
                    if missing:
                        raise RuntimeError(
                            f"{directory.name}: {' '.join(missing)} were neither "
                            "built by this run nor published before, so this "
                            "release would silently drop them")
                target = self.root / "repo" / directory.name
                if target.exists():
                    shutil.rmtree(target)
                target.mkdir(parents=True)
                for apk in apks:
                    shutil.copy2(apk, target / apk.name)
                relative = "/" + str(target.relative_to(self.root))
                try:
                    self.chroot("apk", "index", "--description", f"{CONFIG['origin']} {directory.name}",
                                "--rewrite-arch", directory.name, "--no-warnings",
                                "--output", relative + "/APKINDEX.tar.gz",
                                *[relative + "/" + apk.name for apk in apks])
                except subprocess.CalledProcessError:
                    # The usual cause is that the APKs were signed by a different
                    # key than the one being indexed with: every build shard has
                    # to see the same APK_SIGNING_PRIVATE_KEY, because apk index
                    # verifies each APK's own signature before it will describe it.
                    raise RuntimeError(
                        f"apk index failed for {directory.name}. If it reported "
                        "BAD signature, the APKs were built with a different "
                        "signing key than the one in APK_SIGNING_PRIVATE_KEY - "
                        "the build job needs the same secrets as the index job.")
                # See the `sigtype` comment: RSA256 is the SHA-256 digest, and
                # the only one apk will accept from an EC key.
                self.chroot("abuild-sign", "-t", "RSA256", relative + "/APKINDEX.tar.gz")
                final = self.repository / directory.name
                if final.exists():
                    shutil.rmtree(final)
                shutil.copytree(target, final)
                shutil.copytree(target, self.pages / CONFIG["repository_path"] /
                                directory.name)
                indexed.append(directory.name)
                print(f"indexed {len(apks)} packages for {directory.name}")
            if not indexed:
                raise RuntimeError("No built packages to index under " + str(self.out / "packages"))
            shutil.copy2(self.root / "etc/apk/keys" / self.key, self.repository / self.key)
            shutil.copy2(self.repository / self.key,
                         self.pages / CONFIG["repository_path"] / self.key)
            self.landing_page(indexed)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arch", choices=sorted(ARCHES), default="x86_64")
    parser.add_argument("--package", action="append", choices=PACKAGES,
                        help="Build only this package (repeatable); default: every recipe")
    parser.add_argument("--stage", choices=["packages", "index", "all"], default="all")
    parser.add_argument("--baseline-url", default=os.environ.get("PAGES_BASE_URL", ""),
                        help="URL of the deployed site to carry unpublished "
                             "packages from; empty disables the carry-over")
    parser.add_argument("--expect-packages", default="",
                        help="comma-separated package names that must be present "
                             "for every indexed architecture")
    parser.add_argument("--workdir", default=str(PROJECT / ".state" / "{arch}"))
    parser.add_argument("--cache", default=str(PROJECT / ".cache/build"))
    parser.add_argument("--outdir", default=str(PROJECT / "out"))
    args = parser.parse_args(argv)
    args.workdir = str(Path(args.workdir.format(arch=args.arch)).resolve())
    # preflight() measures free space on the workdir's filesystem, so it has to
    # exist before anything else looks at it.
    os.makedirs(args.workdir, exist_ok=True)
    unknown = sorted(set(args.expect_packages.split(",")) - set(PACKAGES) - {""})
    if unknown:
        parser.error("--expect-packages names no recipe: " + " ".join(unknown))
    selected = args.package or PACKAGES
    args.selected = lambda: selected
    build = Build(args)
    build.preflight()
    for stage in (["packages", "index"] if args.stage == "all" else [args.stage]):
        getattr(build, "stage_" + stage)()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as failure:
        print("build: " + str(failure), file=sys.stderr)
        sys.exit(1)
