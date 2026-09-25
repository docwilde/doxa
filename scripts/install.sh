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

  for command_name in git cargo rustc uv python3 mktemp cp mv readlink; do
    command -v "$command_name" >/dev/null 2>&1 || {
      printf 'doxa-install: %s is required\n' "$command_name" >&2; exit 1;
    }
  done
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || {
    printf 'doxa-install: Python 3.11 or newer is required for the LORE and Claude sidecars\n' >&2; exit 1;
  }
  host_target=$(rustc -vV | sed -n 's/^host: //p')
  [ -n "$host_target" ] || { printf 'doxa-install: cannot determine Rust host target\n' >&2; exit 1; }

  bin_dir="${DOXA_RUST_BIN_DIR:-$HOME/.local/bin}"
  doxa_home="${DOXA_HOME:-$HOME/.doxa}"
  case "$bin_dir:$doxa_home" in
    /*:/*) : ;;
    *) printf 'doxa-install: DOXA_RUST_BIN_DIR and DOXA_HOME must be absolute\n' >&2; exit 1 ;;
  esac

  checkout=$(mktemp -d) || exit 1
  stage=""
  installing=0
  new_sidecar=""
  cleanup() {
    trap - EXIT HUP INT TERM
    if [ "$installing" -eq 1 ]; then
      restore_failed=0
      for name in doxa doxa-rs doxa-daemon-rs doxa-claude-sidecar.py .doxa-sidecar-current; do
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
    [ -z "$new_sidecar" ] || rm -rf "$new_sidecar"
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
  claude_script="$checkout/rust/doxa-claude/claude_sidecar.py"
  for required_file in "$tui_manifest" "$daemon_manifest" "$claude_script" "$checkout/pyproject.toml" "$checkout/uv.lock"; do
    [ -f "$required_file" ] || { printf 'doxa-install: missing %s\n' "$required_file" >&2; exit 1; }
  done

  printf 'doxa-install: building Rust frontend and daemon\n'
  CARGO_TARGET_DIR="$checkout/target" cargo build --release --locked --target "$host_target" --manifest-path "$tui_manifest" --bin doxa-rs || exit 1
  CARGO_TARGET_DIR="$checkout/target" cargo build --release --locked --target "$host_target" --manifest-path "$daemon_manifest" || exit 1
  tui_bin="$checkout/target/$host_target/release/doxa-rs"
  daemon_bin="$checkout/target/$host_target/release/doxa-daemon-rs"
  [ -f "$daemon_bin" ] || daemon_bin="$checkout/target/$host_target/release/doxa-daemon"
  [ -f "$tui_bin" ] && [ -f "$daemon_bin" ] || {
    printf 'doxa-install: Rust build produced no frontend or daemon\n' >&2; exit 1;
  }

  # The Python package remains private to this versioned environment. The
  # launcher puts it first on PATH so Rust's python3 resolution finds it from
  # every working directory, without exposing the retired Python doxa CLI.
  [ ! -L "$doxa_home" ] || { printf 'doxa-install: DOXA_HOME must not be a symlink\n' >&2; exit 1; }
  mkdir -p "$doxa_home" || exit 1
  chmod 700 "$doxa_home" || exit 1
  sidecar_root="$doxa_home/sidecars"
  [ ! -L "$sidecar_root" ] || { printf 'doxa-install: sidecar root must not be a symlink\n' >&2; exit 1; }
  mkdir -p "$sidecar_root" || exit 1
  chmod 700 "$sidecar_root" || exit 1
  sidecar_env="$sidecar_root/$sha"
  [ ! -L "$sidecar_env" ] || { printf 'doxa-install: sidecar environment must not be a symlink\n' >&2; exit 1; }
  if [ -e "$sidecar_env" ] || [ -L "$sidecar_env" ]; then
    [ -d "$sidecar_env" ] && [ ! -L "$sidecar_env" ] || {
      printf 'doxa-install: sidecar path is not a directory: %s\n' "$sidecar_env" >&2; exit 1;
    }
  else
    new_sidecar="$sidecar_env"
    uv venv --python python3 "$sidecar_env" || exit 1
    VIRTUAL_ENV="$sidecar_env" uv sync --frozen --active --no-dev --no-editable --project "$checkout" || exit 1
  fi
  chmod 700 "$sidecar_env" || exit 1
  "$sidecar_env/bin/python" -c 'import doxa.lore_bridge, doxa.engine, lore_core, claude_agent_sdk' || {
    printf 'doxa-install: sidecar import check failed\n' >&2; exit 1;
  }

  mkdir -p "$bin_dir" || exit 1
  for name in doxa doxa-rs doxa-daemon-rs doxa-claude-sidecar.py .doxa-sidecar-current; do
    [ ! -d "$bin_dir/$name" ] || { printf 'doxa-install: %s is a directory\n' "$bin_dir/$name" >&2; exit 1; }
  done
  stage=$(mktemp -d "$bin_dir/.doxa-install.XXXXXXXX") || exit 1
  cp "$tui_bin" "$stage/doxa-rs" || exit 1
  cp "$daemon_bin" "$stage/doxa-daemon-rs" || exit 1
  cp "$claude_script" "$stage/doxa-claude-sidecar.py" || exit 1
  chmod 755 "$stage/doxa-rs" "$stage/doxa-daemon-rs" || exit 1
  chmod 644 "$stage/doxa-claude-sidecar.py" || exit 1
  cat > "$stage/doxa" <<'SH'
#!/bin/sh
bin_dir=$(CDPATH= cd "$(dirname "$0")" && pwd) || exit 1
sidecar_bin=$(readlink "$bin_dir/.doxa-sidecar-current") || exit 1
PATH="$sidecar_bin:$PATH"
DOXA_LORE_PYTHON="$sidecar_bin/python3"
export PATH DOXA_LORE_PYTHON
exec "$bin_dir/doxa-rs" "$@"
SH
  chmod 755 "$stage/doxa" || exit 1
  ln -s "$sidecar_env/bin" "$stage/.doxa-sidecar-current" || exit 1
  mkdir "$stage/backup" || exit 1
  for name in doxa doxa-rs doxa-daemon-rs doxa-claude-sidecar.py .doxa-sidecar-current; do
    if [ -e "$bin_dir/$name" ] || [ -L "$bin_dir/$name" ]; then
      cp -Pp "$bin_dir/$name" "$stage/backup/$name" || exit 1
    fi
  done

  installing=1
  for name in doxa-rs doxa-daemon-rs doxa-claude-sidecar.py .doxa-sidecar-current doxa; do
    mv -f "$stage/$name" "$bin_dir/$name" || exit 1
  done
  installing=0
  new_sidecar=""
  printf 'doxa-install: installed Rust doxa at %s/doxa\n' "$bin_dir"
  resolved_doxa=$(command -v doxa 2>/dev/null || :)
  if [ "$resolved_doxa" != "$bin_dir/doxa" ]; then
    printf 'doxa-install: PATH resolves doxa to %s; add %s before older installs or run %s/doxa directly\n' \
      "${resolved_doxa:-nothing}" "$bin_dir" "$bin_dir" >&2
  fi
  "$bin_dir/doxa" doctor --engine codex || true
}

main "$@"
