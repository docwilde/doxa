#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
# Pipe safety: keep all actions inside main, invoked only after the full file
# has been read by sh. A truncated curl | sh stream cannot run half an install.

main() {
  set -eu
  umask 077

  repo_url="${DOXA_RUST_REPO_URL:-https://github.com/docwilde/doxa}"
  ref="${1:-main}"
  if [ "$ref" = "--rust" ]; then
    shift
    ref="${1:-main}"
  fi
  [ "$#" -le 1 ] || { printf 'doxa-install: usage: sh install.sh [ref]\n' >&2; exit 1; }
  case "$ref" in
    "" | -* | *..* | *@\{* | *[!a-zA-Z0-9._/-]*)
      printf 'doxa-install: invalid ref %s\n' "$ref" >&2; exit 1 ;;
  esac

  for command_name in git cargo rustc mktemp cp mv; do
    command -v "$command_name" >/dev/null 2>&1 || {
      printf 'doxa-install: %s is required\n' "$command_name" >&2; exit 1;
    }
  done
  host_target=$(rustc -vV | sed -n 's/^host: //p')
  [ -n "$host_target" ] || { printf 'doxa-install: cannot determine Rust host target\n' >&2; exit 1; }

  bin_dir="${DOXA_RUST_BIN_DIR:-$HOME/.local/bin}"
  doxa_home="${DOXA_HOME:-$HOME/.doxa}"
  case "$bin_dir:$doxa_home" in
    /*:/*) : ;;
    *) printf 'doxa-install: DOXA_RUST_BIN_DIR and DOXA_HOME must be absolute\n' >&2; exit 1 ;;
  esac

  cache_dir="${DOXA_INSTALL_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/doxa/install}"
  case "$cache_dir" in /*) : ;; *) printf 'doxa-install: cache must be absolute\n' >&2; exit 1 ;; esac
  [ ! -L "$cache_dir" ] || { printf 'doxa-install: cache must not be a symlink\n' >&2; exit 1; }
  mkdir -p "$cache_dir"
  chmod 700 "$cache_dir"
  TMPDIR="$cache_dir"
  export TMPDIR
  checkout=$(mktemp -d "$cache_dir/checkout.XXXXXXXX") || exit 1
  stage=""
  installing=0
  cleanup() {
    trap - EXIT HUP INT TERM
    if [ "$installing" -eq 1 ]; then
      restore_failed=0
      for name in doxa doxa-rs doxa-daemon-rs doxa-remote lore-rs doxa-claude-sidecar.py .doxa-sidecar-current .doxa-install-sha; do
        rm -f "$bin_dir/$name" || { restore_failed=1; continue; }
        if [ -e "$stage/backup/$name" ] || [ -L "$stage/backup/$name" ]; then
          mv "$stage/backup/$name" "$bin_dir/$name" || restore_failed=1
        fi
      done
      [ "$restore_failed" -eq 0 ] || {
        printf 'doxa-install: rollback incomplete; backups remain in %s/backup\n' "$stage" >&2
        exit 1
      }
      installing=0
    fi
    [ -z "$stage" ] || rm -rf "$stage"
    rm -rf "$checkout"
  }
  trap 'cleanup' EXIT
  trap 'exit 1' HUP INT TERM

  printf 'doxa-install: fetching %s from %s\n' "$ref" "$repo_url"
  git -C "$checkout" init -q || exit 1
  git -C "$checkout" remote add origin "$repo_url" || exit 1
  git -C "$checkout" fetch --quiet --depth 1 origin "$ref" || exit 1
  git -C "$checkout" checkout --quiet --detach FETCH_HEAD || exit 1
  sha=$(git -C "$checkout" rev-parse HEAD) || exit 1
  tui_manifest="$checkout/rust/doxa-tui/Cargo.toml"
  daemon_manifest="$checkout/rust/doxa-daemon/Cargo.toml"
  remote_manifest="$checkout/rust/doxa-remote/Cargo.toml"
  for required_file in "$tui_manifest" "$daemon_manifest" "$remote_manifest" "$checkout/Cargo.lock"; do
    [ -f "$required_file" ] || { printf 'doxa-install: missing %s\n' "$required_file" >&2; exit 1; }
  done

  # Keep dependency artifacts between upgrades. Source checks and the locked
  # Cargo graph still select this checkout's actual binaries.
  build_dir="${DOXA_INSTALL_TARGET_DIR:-$cache_dir/target}"
  case "$build_dir" in /*) : ;; *) printf 'doxa-install: build cache must be absolute\n' >&2; exit 1 ;; esac
  [ ! -L "$build_dir" ] || { printf 'doxa-install: build cache must not be a symlink\n' >&2; exit 1; }
  mkdir -p "$build_dir"
  chmod 700 "$build_dir"
  printf 'doxa-install: building Rust frontend and daemon\n'
  CARGO_TARGET_DIR="$build_dir" cargo build --release --locked --target "$host_target" --manifest-path "$tui_manifest" --bin doxa-rs || exit 1
  CARGO_TARGET_DIR="$build_dir" cargo build --release --locked --target "$host_target" --manifest-path "$daemon_manifest" || exit 1
  CARGO_TARGET_DIR="$build_dir" cargo build --release --locked --target "$host_target" --manifest-path "$remote_manifest" --bin doxa-remote || exit 1
  CARGO_TARGET_DIR="$build_dir" cargo build --release --locked --target "$host_target" --manifest-path "$tui_manifest" --package lore-core --bin lore-rs || exit 1
  lore_bin="$build_dir/$host_target/release/lore-rs"
  tui_bin="$build_dir/$host_target/release/doxa-rs"
  daemon_bin="$build_dir/$host_target/release/doxa-daemon-rs"
  [ -f "$daemon_bin" ] || daemon_bin="$build_dir/$host_target/release/doxa-daemon"
  remote_bin="$build_dir/$host_target/release/doxa-remote"
  [ -f "$tui_bin" ] && [ -f "$daemon_bin" ] && [ -f "$remote_bin" ] && [ -f "$lore_bin" ] || {
    printf 'doxa-install: Rust build produced no frontend, daemon, remote adapter or native LORE carrier\n' >&2; exit 1;
  }

  # Protected Codex uses a private provider build. Keep the official CLI for
  # login/help and compile the pinned app server in its own reusable cache.
  # An explicit skip supports installations using only other engines; protected
  # Codex startup then refuses until this separate installer is run.
  if [ "${DOXA_INSTALL_CODEX_PROTECTED:-1}" = 1 ] && command -v codex >/dev/null 2>&1 && [ "$(uname -s)" = Linux ]; then
    command -v python3 >/dev/null 2>&1 || { printf 'doxa-install: Python 3.11+ is required only to build/install the private Codex provider\n' >&2; exit 1; }
    printf 'doxa-install: installing private fail-closed Codex app server (isolated Rust 1.95 toolchain/cache)\n'
    CARGO_TARGET_DIR="$build_dir" cargo build --release --locked --target "$host_target" --manifest-path "$tui_manifest" --package doxa-engines --bin doxa-codex-protected -j 1 || exit 1
    python3 "$checkout/scripts/install_codex_protected.py" --launcher "$build_dir/$host_target/release/doxa-codex-protected" || exit 1
  fi
  if [ "$(uname -s)" = Darwin ] && command -v codex >/dev/null 2>&1; then
    printf 'doxa-install: protected Codex requires Linux process supervision; Codex sessions remain unavailable on macOS\n' >&2
  fi

  mkdir -p "$bin_dir" || exit 1
  for name in doxa doxa-rs doxa-daemon-rs doxa-remote lore-rs doxa-claude-sidecar.py .doxa-sidecar-current .doxa-install-sha; do
    [ ! -d "$bin_dir/$name" ] || [ -L "$bin_dir/$name" ] || { printf 'doxa-install: %s is a directory\n' "$bin_dir/$name" >&2; exit 1; }
  done
  stage=$(mktemp -d "$bin_dir/.doxa-install.XXXXXXXX") || exit 1
  cp "$tui_bin" "$stage/doxa-rs" || exit 1
  cp "$daemon_bin" "$stage/doxa-daemon-rs" || exit 1
  cp "$remote_bin" "$stage/doxa-remote" || exit 1
  cp "$lore_bin" "$stage/lore-rs" || exit 1
  chmod 755 "$stage/doxa-rs" "$stage/doxa-daemon-rs" "$stage/doxa-remote" "$stage/lore-rs" || exit 1
  cat > "$stage/doxa" <<'SH'
#!/bin/sh
bin_dir=$(CDPATH= cd "$(dirname "$0")" && pwd) || exit 1
DOXA_LORE_RS="${DOXA_LORE_RS:-$bin_dir/lore-rs}"
export DOXA_LORE_RS
exec "$bin_dir/doxa-rs" "$@"
SH
  chmod 755 "$stage/doxa" || exit 1
  printf '%s\n' "$sha" > "$stage/.doxa-install-sha" || exit 1
  mkdir "$stage/backup" || exit 1
  for name in doxa doxa-rs doxa-daemon-rs doxa-remote lore-rs doxa-claude-sidecar.py .doxa-sidecar-current .doxa-install-sha; do
    if [ -e "$bin_dir/$name" ] || [ -L "$bin_dir/$name" ]; then
      cp -Pp "$bin_dir/$name" "$stage/backup/$name" || exit 1
    fi
  done

  installing=1
  for name in doxa-rs doxa-daemon-rs doxa-remote lore-rs .doxa-install-sha doxa; do
    # mv can treat a symlink to a directory as the destination directory,
    # leaving the old launcher pointer in place and writing inside its target.
    if [ -L "$bin_dir/$name" ]; then
      rm -f "$bin_dir/$name" || exit 1
    fi
    mv -f "$stage/$name" "$bin_dir/$name" || exit 1
  done
  rm -f "$bin_dir/doxa-claude-sidecar.py" "$bin_dir/.doxa-sidecar-current"
  installing=0
  printf 'doxa-install: installed Rust doxa at %s/doxa\n' "$bin_dir"
  resolved_doxa=$(command -v doxa 2>/dev/null || :)
  if [ "$resolved_doxa" != "$bin_dir/doxa" ]; then
    printf 'doxa-install: PATH resolves doxa to %s; add %s before older installs or run %s/doxa directly\n' \
      "${resolved_doxa:-nothing}" "$bin_dir" "$bin_dir" >&2
  fi
  # A desktop session may have a different PATH from this shell. Pin the
  # shortcut to the launcher just installed.
  # A missing desktop environment needs no special handling: XDG launchers
  # discover these per-user files when one is available.
  if [ "${DOXA_NO_LAUNCHER:-0}" != 1 ]; then
    "$bin_dir/doxa-rs" install-launcher "$bin_dir/doxa" || printf 'doxa-install: could not install desktop shortcut\n' >&2
  fi
  "$bin_dir/doxa" doctor --engine codex || true
}

main "$@"
