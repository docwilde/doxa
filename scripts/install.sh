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
      for name in doxa doxa-rs doxa-daemon-rs lore-rs doxa-claude-sidecar.py .doxa-sidecar-current .doxa-install-sha; do
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
  for required_file in "$tui_manifest" "$daemon_manifest" "$checkout/Cargo.lock"; do
    [ -f "$required_file" ] || { printf 'doxa-install: missing %s\n' "$required_file" >&2; exit 1; }
  done

  printf 'doxa-install: building Rust frontend and daemon\n'
  CARGO_TARGET_DIR="$checkout/target" cargo build --release --locked --target "$host_target" --manifest-path "$tui_manifest" --bin doxa-rs || exit 1
  CARGO_TARGET_DIR="$checkout/target" cargo build --release --locked --target "$host_target" --manifest-path "$daemon_manifest" || exit 1
  CARGO_TARGET_DIR="$checkout/target" cargo build --release --locked --target "$host_target" --manifest-path "$tui_manifest" --package lore-core --bin lore-rs || exit 1
  lore_bin="$checkout/target/$host_target/release/lore-rs"
  tui_bin="$checkout/target/$host_target/release/doxa-rs"
  daemon_bin="$checkout/target/$host_target/release/doxa-daemon-rs"
  [ -f "$daemon_bin" ] || daemon_bin="$checkout/target/$host_target/release/doxa-daemon"
  [ -f "$tui_bin" ] && [ -f "$daemon_bin" ] && [ -f "$lore_bin" ] || {
    printf 'doxa-install: Rust build produced no frontend, daemon or native LORE carrier\n' >&2; exit 1;
  }

  mkdir -p "$bin_dir" || exit 1
  for name in doxa doxa-rs doxa-daemon-rs lore-rs doxa-claude-sidecar.py .doxa-sidecar-current .doxa-install-sha; do
    [ ! -d "$bin_dir/$name" ] || [ -L "$bin_dir/$name" ] || { printf 'doxa-install: %s is a directory\n' "$bin_dir/$name" >&2; exit 1; }
  done
  stage=$(mktemp -d "$bin_dir/.doxa-install.XXXXXXXX") || exit 1
  cp "$tui_bin" "$stage/doxa-rs" || exit 1
  cp "$daemon_bin" "$stage/doxa-daemon-rs" || exit 1
  cp "$lore_bin" "$stage/lore-rs" || exit 1
  chmod 755 "$stage/doxa-rs" "$stage/doxa-daemon-rs" "$stage/lore-rs" || exit 1
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
  for name in doxa doxa-rs doxa-daemon-rs lore-rs doxa-claude-sidecar.py .doxa-sidecar-current .doxa-install-sha; do
    if [ -e "$bin_dir/$name" ] || [ -L "$bin_dir/$name" ]; then
      cp -Pp "$bin_dir/$name" "$stage/backup/$name" || exit 1
    fi
  done

  installing=1
  for name in doxa-rs doxa-daemon-rs lore-rs doxa-claude-sidecar.py .doxa-sidecar-current .doxa-install-sha doxa; do
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
