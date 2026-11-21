#!/bin/sh
# PGO/BOLT training workload for Mission Center 1.2.0.
#
#   sh mission-center-pgo-train.sh <binary-path> <role> [<role> ...] [--session]
#
# <role> is a whitespace separated list understood by the driver: "ui" trains
# the missioncenter GTK application, "magpie" trains the missioncenter-magpie
# data-gathering server. The driver passes the single string "ui magpie" for
# the missioncenter binary and "magpie" for the magpie binary, so both forms
# have to work.
#
# Which run really trains what:
#   * The "ui" workload is what trains missioncenter. It also *exercises*
#     magpie, because the application spawns missioncenter-magpie from PATH
#     and polls it over an nng ipc:// socket. That does not produce a magpie
#     profile, though: Mission Center stops its child with child.kill(), i.e.
#     SIGKILL (src/magpie_client/client.rs, start_magpie_process_thread), and
#     a SIGKILLed process never reaches the LLVM/BOLT atexit hooks.
#   * The "magpie" workload is therefore what trains magpie. magpie installs
#     handlers for TERM/INT/QUIT (gng-src/src/main.rs, os_signals_listen) that
#     only clear the serve() loop condition, so a SIGTERM terminated server
#     returns from main normally and flushes its profile. --test, --help and
#     --version exit normally as well and are kept as cheap extra coverage.
#
# The program under training must always terminate normally, so the profile is
# flushed. Never SIGKILL it on the success path.
#
# Optional environment:
#   MC_TRAIN_SRCROOT  Mission Center source root (contains data/ and
#                     resources/). Used to compile the GSettings schema and to
#                     find the built gresource and the magpie hardware
#                     database. Without it the installed copies are used.
#   MC_TRAIN_MAGPIE   Path of a missioncenter-magpie binary to use for the ui
#                     workload when none is next to the trained binary.
#   LLVM_PROFILE_FILE Set by the driver. Never modified here.
set -eu

if [ $# -lt 2 ]; then
    echo "usage: $0 <binary> <role> [--session]" >&2
    exit 2
fi

# Absolute path without depending on realpath(1).
abspath() {
    case $1 in
    /*) printf '%s\n' "$1" ;;
    *) printf '%s\n' "$PWD/$1" ;;
    esac
}

binary=$(abspath "$1")
role=$2
if [ ! -x "$binary" ]; then
    echo "PGO training: no executable: $binary" >&2
    exit 2
fi

train_ui=0
train_magpie=0
# shellcheck disable=SC2086  # word splitting on the role list is the point
for want in $role; do
    case $want in
    ui) train_ui=1 ;;
    magpie) train_magpie=1 ;;
    *)
        echo "PGO training: unknown training role: $want" >&2
        exit 2
        ;;
    esac
done
if [ "$train_ui" = 0 ] && [ "$train_magpie" = 0 ]; then
    echo "PGO training: empty training role" >&2
    exit 2
fi

# The ui workload needs a session bus for the org.gtk.Actions surface.
if [ "$train_ui" = 1 ] && [ "${3:-}" != --session ]; then
    exec dbus-run-session -- sh "$0" "$binary" "$role" --session
fi

app_id=io.missioncenter.MissionCenter
root_path=/io/missioncenter/MissionCenter

scratch=$(mktemp -d)
app=
compositor=
magpie_server=
schemas_dir=
window_path=
cleanup() {
    status=$?
    if [ "$status" -ne 0 ]; then
        for log in "$scratch"/*.log; do
            [ -f "$log" ] || continue
            cat "$log" >&2 || :
        done
    fi
    [ -z "$compositor" ] || kill "$compositor" 2>/dev/null || :
    # Only kill the trained processes when something went wrong; a normal run
    # ends them through the quit/close actions or SIGTERM so that they can
    # still write their profiles.
    if [ "$status" -ne 0 ]; then
        [ -z "$app" ] || kill "$app" 2>/dev/null || :
        [ -z "$magpie_server" ] || kill "$magpie_server" 2>/dev/null || :
    fi
    rm -rf "$scratch"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

export XDG_RUNTIME_DIR="$scratch/runtime"
export XDG_CONFIG_HOME="$scratch/config"
export XDG_DATA_HOME="$scratch/data"
export XDG_CACHE_HOME="$scratch/cache"
export HOME="$scratch/home"
mkdir -p "$XDG_RUNTIME_DIR" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_CACHE_HOME" \
    "$HOME" "$scratch/bin"
chmod 700 "$XDG_RUNTIME_DIR"

# Mission Center reads its settings, resources and hardware database from the
# build tree when the recipe points us at it, which keeps the training run
# independent of what is already installed. GSETTINGS_SCHEMA_DIR is searched
# before XDG_DATA_DIRS, so GTK's own schemas stay visible.
if [ -n "${MC_TRAIN_SRCROOT:-}" ] && [ -d "$MC_TRAIN_SRCROOT/data" ]; then
    mkdir -p "$scratch/schemas"
    cp "$MC_TRAIN_SRCROOT"/data/*.gschema.xml "$scratch/schemas/" 2>/dev/null || :
    if glib-compile-schemas "$scratch/schemas" 2>/dev/null &&
        [ -f "$scratch/schemas/gschemas.compiled" ]; then
        schemas_dir=$scratch/schemas
        export GSETTINGS_SCHEMA_DIR="$schemas_dir${GSETTINGS_SCHEMA_DIR:+:$GSETTINGS_SCHEMA_DIR}"
    fi
    if [ -f "$MC_TRAIN_SRCROOT/resources/missioncenter.gresource" ]; then
        export MC_RESOURCE_DIR="$MC_TRAIN_SRCROOT/resources"
    fi
    if [ -f "$MC_TRAIN_SRCROOT/subprojects/magpie/platform-linux/hwdb/hw.db" ]; then
        export MC_MAGPIE_HW_DB="$MC_TRAIN_SRCROOT/subprojects/magpie/platform-linux/hwdb/hw.db"
    fi
fi

gset() {
    if [ -n "$schemas_dir" ]; then
        gsettings --schemadir "$schemas_dir" set "$app_id" "$@" >/dev/null 2>&1 || :
    else
        gsettings set "$app_id" "$@" >/dev/null 2>&1 || :
    fi
}

# ---------------------------------------------------------------- magpie role
if [ "$train_magpie" = 1 ]; then
    # The missioncenter binary also gets the "magpie" role from the driver, but
    # only magpie understands --addr. Use the help text as the capability probe
    # instead of guessing from the file name.
    if "$binary" --help >"$scratch/magpie-help.log" 2>&1 &&
        grep -q -- '--addr' "$scratch/magpie-help.log"; then
        # Every one of these is a normal exit, so each one flushes a profile.
        "$binary" --version >>"$scratch/magpie-help.log" 2>&1 || :
        "$binary" --test >"$scratch/magpie-test.log" 2>&1 || :

        # The real server run. magpie passes the --addr value straight to
        # nng_listen, so the plain nng transport form is used, exactly like the
        # application does (client.rs: .arg("--addr").arg(socket_addr) with
        # ipc:///tmp/magpie_<rand>.ipc).
        "$binary" --addr "ipc://$scratch/magpie-train.ipc" \
            >"$scratch/magpie-server.log" 2>&1 &
        magpie_server=$!
        # Cover the blocking warm-up refresh (3 s deadline) plus a few serve()
        # iterations, then shut down with a signal magpie handles.
        sleep 5
        kill "$magpie_server" 2>/dev/null || :
        if ! wait "$magpie_server"; then
            echo "PGO training: warning: magpie server did not exit cleanly" >&2
        fi
        magpie_server=
    else
        echo "PGO training: $binary is not magpie, skipping the magpie workload" >&2
    fi
fi

# -------------------------------------------------------------------- ui role
if [ "$train_ui" = 1 ]; then
    # The application spawns the gatherer by the bare name missioncenter-magpie
    # through PATH (src/magpie_client/client.rs). If it cannot spawn it, the
    # app shows a fatal dialog and spins forever, so a missing gatherer has to
    # be a hard error here rather than a training run that times out.
    bindir=$(dirname "$binary")
    if [ -f "$bindir/missioncenter-magpie" ]; then
        PATH="$bindir:$PATH"
    elif [ -n "${MC_TRAIN_MAGPIE:-}" ] && [ -f "$MC_TRAIN_MAGPIE" ]; then
        ln -s "$(abspath "$MC_TRAIN_MAGPIE")" "$scratch/bin/missioncenter-magpie"
        PATH="$scratch/bin:$PATH"
    elif [ -f "$bindir/magpie" ]; then
        ln -s "$bindir/magpie" "$scratch/bin/missioncenter-magpie"
        PATH="$scratch/bin:$PATH"
    elif ! command -v missioncenter-magpie >/dev/null 2>&1; then
        echo "PGO training: no missioncenter-magpie for $binary to spawn" >&2
        echo "PGO training: set MC_TRAIN_MAGPIE or install the gatherer first" >&2
        exit 1
    fi
    export PATH

    export WAYLAND_DISPLAY=wayland-0
    export WLR_BACKENDS=headless WLR_RENDERER=pixman
    export GDK_BACKEND=wayland GSK_RENDERER=cairo GTK_A11Y=none

    # Start headless with a modal-free configuration: the first-run dialog is
    # dismissed by clearing the setting before the window is mapped.
    gset first-time-running false
    gset window-selected-page "'apps-page'"
    gset window-interface-style "'system'"
    gset performance-page-memory2-use-bytes true
    gset apps-page-hide-is-zero true
    gset performance-smooth-graphs false
    gset performance-show-gpus true

    labwc -C "$XDG_CONFIG_HOME" >"$scratch/compositor.log" 2>&1 &
    compositor=$!
    i=0
    while [ ! -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ]; do
        i=$((i + 1))
        if [ "$i" -ge 100 ] || ! kill -0 "$compositor" 2>/dev/null; then
            cat "$scratch/compositor.log" >&2
            exit 1
        fi
        sleep 0.1
    done

    "$binary" >"$scratch/app.log" 2>&1 &
    app=$!

    # Wait for the application to export its application actions.
    i=0
    until gdbus call --session --dest "$app_id" --object-path "$root_path" \
        --method org.gtk.Actions.List >"$scratch/actions.log" 2>&1; do
        i=$((i + 1))
        if [ "$i" -ge 100 ] || ! kill -0 "$app" 2>/dev/null; then
            echo "PGO training: could not reach $app_id at $root_path" >&2
            if ! kill -0 "$app" 2>/dev/null; then
                if wait "$app"; then
                    app_status=0
                else
                    app_status=$?
                fi
                echo "PGO training: process exited with status $app_status: $binary" >&2
                app=
            fi
            cat "$scratch/actions.log" "$scratch/app.log" >&2
            exit 1
        fi
        sleep 0.1
    done

    # The window template in resources/ui/window.blp has no id:, so the object
    # path of the "win" action group is not known statically. Find it on the
    # bus, preferring the deepest exported object, and fall back to the path
    # GTK/GTK-4 uses for the first window.
    discovered=$(
        gdbus introspect --session --dest "$app_id" --object-path "$root_path" --recurse 2>/dev/null |
            sed -n 's/^[[:space:]]*node \([^ {]*\) {$/\1/p' |
            grep -v "^${root_path}\$" | sort -r || :
    )
    for candidate in $discovered; do
        if gdbus introspect --session --dest "$app_id" --object-path "$candidate" 2>/dev/null |
            grep -q 'interface org.gtk.Actions'; then
            window_path=$candidate
            break
        fi
    done
    if [ -z "$window_path" ]; then
        window_path=$root_path/window/1
        if ! gdbus introspect --session --dest "$app_id" --object-path "$window_path" 2>/dev/null |
            grep -q 'interface org.gtk.Actions'; then
            # No usable window action group. Application actions alone are a
            # weaker but still valid training run.
            window_path=
        fi
    fi

    # org.gtk.Actions.Activate takes (s, av, a{sv}); SetState takes (s, v) and
    # rejects a third argument.
    activate() {
        target=$1
        name=$2
        shift 2
        gdbus call --session --dest "$app_id" --object-path "$target" \
            --method org.gtk.Actions.Activate "$name" "${1:-[]}" '{}' \
            >"$scratch/actions.log" 2>&1 || :
    }
    set_state() {
        gdbus call --session --dest "$app_id" --object-path "$1" \
            --method org.gtk.Actions.SetState "$2" "$3" \
            >"$scratch/actions.log" 2>&1 || :
    }
    app_activate() {
        activate "$root_path" "$@"
    }
    win_activate() {
        [ -n "$window_path" ] || return 0
        activate "$window_path" "$@"
    }
    win_set_state() {
        [ -n "$window_path" ] || return 0
        set_state "$window_path" "$@"
    }

    # Mission Center polls the gatherer on a timer and redraws the graphs, so
    # the workload has to stay on screen long enough to collect real work.
    sleep 2
    win_activate select-tab-apps
    sleep 1
    win_activate select-tab-services
    sleep 1
    win_activate select-tab-performance
    sleep 1

    win_set_state toggle-search '<true>'
    sleep 0.4
    win_activate toggle-search
    sleep 0.4
    win_set_state toggle-search '<false>'
    sleep 0.4
    win_set_state toggle-sidebar '<false>'
    sleep 0.5
    win_activate toggle-sidebar
    sleep 0.5

    # The interface-style action takes the adwaita names, not the enum nicks
    # used by the gsettings key (src/window.rs).
    for style in force-dark force-light default; do
        win_activate interface-style "[<'$style'>]"
        sleep 0.6
    done

    # open-wiki is deliberately not activated: it launches a URI in the default
    # browser, which does not exist here.
    app_activate preferences
    sleep 1
    app_activate keyboard-shortcuts
    sleep 0.8
    app_activate about
    sleep 0.8
    # Reads the system information over the gatherer socket.
    app_activate system-about
    sleep 0.8
    app_activate first-run-dialog
    sleep 0.6

    # Normal shutdown runs the LLVM/BOLT atexit hooks that flush the profile.
    if ! gdbus call --session --dest "$app_id" --object-path "$root_path" \
        --method org.gtk.Actions.Activate quit '[]' '{}' >"$scratch/actions.log" 2>&1; then
        # Closing the only window quits the application as well: it is built
        # with ApplicationFlags::empty().
        if [ -z "$window_path" ] || ! gdbus call --session --dest "$app_id" \
            --object-path "$window_path" --method org.gtk.Actions.Activate \
            close '[]' '{}' >"$scratch/actions.log" 2>&1; then
            echo "PGO training: could not ask $app_id to quit" >&2
            cat "$scratch/actions.log" "$scratch/app.log" >&2
            exit 1
        fi
    fi
    if ! wait "$app"; then
        cat "$scratch/app.log" >&2
        exit 1
    fi
    app=
fi

echo "PGO training passed: $binary"
