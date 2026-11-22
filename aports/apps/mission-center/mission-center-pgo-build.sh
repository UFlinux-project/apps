#!/bin/sh
# Build both shipped executables with PGO followed by BOLT.
#
# Usage: sh mission-center-pgo-build.sh <meson-build-dir> [target]
# Run with the current directory set to the Mission Center source root, the
# directory that holds Cargo.toml and subprojects/magpie. The script lives in
# the packaging repository rather than in the source bundle, so the source root
# is taken from the working directory and never from $0.
#
# Mission Center ships two binaries built from two separate cargo workspaces:
# `missioncenter` in the root workspace and `magpie` in the magpie submodule,
# installed as missioncenter-magpie. Every cargo-pgo call therefore runs inside
# the workspace it builds.
set -eu

if [ $# -lt 1 ]; then
    echo "usage: $0 <meson-build-dir> [target]" >&2
    exit 2
fi
out=$1
case $out in
    /*) ;;
    *) out=$PWD/$out ;;
esac
root=$(pwd)
if [ ! -f "$root/Cargo.toml" ]; then
    echo "run from the Mission Center source root: no Cargo.toml in $root" >&2
    exit 2
fi
if [ ! -f "$root/subprojects/magpie/Cargo.toml" ]; then
    echo "no subprojects/magpie/Cargo.toml in $root: is the submodule checked out?" >&2
    exit 2
fi
# Upstream's meson build injects BUILD_ROOT into its cargo invocation
# (src/meson.build): missioncenter's main.rs includes
# $BUILD_ROOT/src/config.rs, the file configure_file generates from
# config.rs.in into the meson build directory.  This script replaces that
# cargo invocation, so it has to provide the variable itself.  The argument
# above is exactly that directory, and the recipe's first meson compile has
# already produced the file.
if [ ! -f "$out/src/config.rs" ]; then
    echo "no meson-generated config.rs in $out/src: run meson compile first" >&2
    exit 2
fi
# Exporting is right here and wrong in the recipe.  The recipe's build() runs
# inside the abuild process, where a leaked BUILD_ROOT is read by cleanup() as
# "clean up this rootbld chroot"; this script is a child of that process, so the
# export dies with it.  It has to stay an export rather than a per-command
# assignment: pgo-driver.py reads the variable from its own environment.
export BUILD_ROOT="$out"
here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
train=${MC_PGO_TRAIN:-$here/mission-center-pgo-train.sh}
if [ ! -f "$train" ]; then
    echo "PGO+BOLT training script is missing: $train" >&2
    exit 2
fi
# magpie's build.rs downloads and patches nvtop itself, but its download_file()
# helper returns early, skipping both the network and the checksum, when the
# archive is already sitting in its drop-in directory. Handing over the vendored
# archive therefore keeps the pipeline offline with no patch bookkeeping here:
# build.rs unpacks it and applies the eleven patches from nvtop.json, so that
# file stays the only copy of the patch list.
nvtop_tarball=${MC_NVTOP_TARBALL:-}
nvtop_name=${nvtop_tarball##*/}
if [ -z "$nvtop_tarball" ] || [ ! -f "$nvtop_tarball" ]; then
    echo "MC_NVTOP_TARBALL must point at the vendored nvtop archive" >&2
    exit 2
fi
if ! printf '%s\n' "$nvtop_name" | grep -Eq '^[0-9a-f]{40}\.tar\.gz$'; then
    echo "MC_NVTOP_TARBALL must be named <40-hex-sha>.tar.gz, e.g. 3d4a953da02bc18886734613bb9f60ff80669de7.tar.gz" >&2
    exit 2
fi
host=$(rustc -vV | sed -n 's/^host: //p')
target=${2:-$host}
case "$target" in
    x86_64-*-linux-*|aarch64-*-linux-*) ;;
    *) echo "PGO+BOLT requires an x86_64 or aarch64 Linux target: $target" >&2; exit 2 ;;
esac
if [ "$target" != "$host" ]; then
    echo "PGO+BOLT training requires a native build ($host), got $target" >&2
    exit 2
fi
if [ -n "${CARGO_ENCODED_RUSTFLAGS:-}" ]; then
    echo "Use RUSTFLAGS instead of CARGO_ENCODED_RUSTFLAGS with cargo-pgo" >&2
    exit 2
fi
if [ "$(id -u)" -eq 0 ]; then
    echo "PGO+BOLT training must run as an ordinary user" >&2
    exit 2
fi
for tool in cargo cargo-pgo cargo-auditable llvm-bolt merge-fdata labwc dbus-run-session gdbus timeout python3; do
    command -v "$tool" >/dev/null || { echo "PGO+BOLT requires $tool" >&2; exit 2; }
done

# Isolate both profile sets from ordinary builds and other architectures.
base=${CARGO_TARGET_DIR:-$root/target}
mkdir -p "$base"
base=$(CDPATH='' cd -- "$base" && pwd)
stage=$base/pgo-bolt/$target
mkdir -p "$stage/cargo-wrapper"
# cargo-pgo invokes `cargo build` internally. Keep the package's dependency
# audit section in every compilation, including the final PGO+BOLT binary.
UFLINUX_PGO_CARGO=$(command -v cargo)
export UFLINUX_PGO_CARGO
cat >"$stage/cargo-wrapper/cargo" <<'WRAPPER'
#!/bin/sh
if [ "${1:-}" = build ]; then
    exec "$UFLINUX_PGO_CARGO" auditable "$@"
fi
exec "$UFLINUX_PGO_CARGO" "$@"
WRAPPER
chmod +x "$stage/cargo-wrapper/cargo"
PATH="$stage/cargo-wrapper:$PATH"
export PATH
export CARGO_INCREMENTAL=0
# BOLT needs symbols and relocations until its final rewrite. abuild strips
# the installed artifact later. cargo-pgo adds --emit-relocs for the BOLT steps.
export CARGO_PROFILE_RELEASE_STRIP=none
# Alpine enables packed relative relocations by default. LLVM 22 BOLT has
# RELR addend rewriting bugs; use ordinary RELA relocations for the entire
# pipeline, overriding an earlier packaging flag without dropping other flags.
RUSTFLAGS="${RUSTFLAGS:-} -Clink-arg=-Wl,-z,nopack-relative-relocs"
# BOLT 22 mistakes `.warm` inside legacy Rust names such as
# `tantivy..reader..warming` for a split-function suffix. v0 mangling avoids
# those embedded dotted paths. Use it for all PGO and BOLT compilations.
RUSTFLAGS="$RUSTFLAGS -Csymbol-mangling-version=v0"
export RUSTFLAGS
# musl on AArch64 ignores DT_FINI, but BOLT prefers it over DT_FINI_ARRAY.
# Suppress the unused legacy tag so BOLT hooks the supported fini array and
# actually flushes its profiles. Keep this flag in every compilation stage.
case "$target" in
    aarch64-*-linux-musl*)
        RUSTFLAGS="${RUSTFLAGS:-} -Clink-arg=-Wl,-fini,0"
        export RUSTFLAGS
        ;;
esac
cd "$root"
cargo pgo info

# name|cargo package|workspace directory|training role, split on '|' below.
# Gatherer first: training the UI spawns a gatherer, so magpie has to be
# built and rewritten before the application is trained.  Fields are
# name|cargo package|workspace directory|training role, entries are space
# separated, so neither field may contain one.
workspaces="magpie|magpie|subprojects/magpie|magpie missioncenter|missioncenter|.|ui"
for entry in $workspaces; do
    saved_ifs=$IFS
    set -f
    IFS='|'
    set -- $entry
    IFS=$saved_ifs
    set +f
    name=$1
    package=$2
    dir=$3
    role=$4
    work=$stage/$name
    # Discard profiles from earlier invocations before any new training.
    rm -rf "$work/pgo-profiles" "$work/bolt-profiles"
    set -- --frozen --target "$target" --target-dir "$work" -p "$package" --bin "$name"
    if [ "$name" = magpie ]; then
        # The drop-in directory is $OUT_DIR/../../native, and cargo places
        # OUT_DIR below the target triple when --target is given.
        native=$work/$target/release/build/native
        mkdir -p "$native"
        install -m644 "$nvtop_tarball" "$native/$nvtop_name"
    fi
    binary=$work/$target/release/$name
    (cd "$root/$dir" && cargo pgo build -- "$@")
    LLVM_PROFILE_FILE="$work/pgo-profiles/$name-%m-%p.profraw" timeout 300 sh "$train" "$binary" "$role"
    if ! find "$work/pgo-profiles" -name '*.profraw' -type f -size +0c | grep -q .; then
        echo "PGO training produced no profiles for $name" >&2
        exit 1
    fi
    (cd "$root/$dir" && cargo pgo bolt build --with-pgo -- "$@")
    timeout 300 sh "$train" "$binary-bolt-instrumented" "$role"
    if ! find "$work/bolt-profiles/$name" -name '*.fdata' -type f -size +0c | grep -q .; then
        echo "BOLT training produced no profiles for $name" >&2
        exit 1
    fi
    (cd "$root/$dir" && cargo pgo bolt optimize --with-pgo -- "$@")
    timeout 300 sh "$train" "$binary-bolt-optimized" "$role"
done
# Publish only after both rewritten executables have passed every workload.
# These are exactly the two files the project's meson cargo-build targets copy,
# so the recipe can let meson "build" them with its no-op cargo shim and then
# install them as usual.
mkdir -p "$out/src/release"
install -m755 "$stage/missioncenter/$target/release/missioncenter-bolt-optimized" "$out/src/release/missioncenter"
install -m755 "$stage/magpie/$target/release/magpie-bolt-optimized" "$out/src/release/missioncenter-magpie"
echo "PGO+BOLT binaries: $out/src/release"
