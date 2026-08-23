#!/bin/sh
# doxa installer -- POSIX sh, no bashisms (developed and tested against
# dash, the strictest common /bin/sh).
#
#   curl -fsSL https://raw.githubusercontent.com/docwilde/doxa/main/scripts/install.sh | sh
#
# Read it first if you'd rather not pipe a stranger's script into a shell:
#
#   curl -fsSL https://raw.githubusercontent.com/docwilde/doxa/main/scripts/install.sh -o install.sh
#   less install.sh   # then: sh install.sh
#
# Install a specific tag/branch/sha instead of main's HEAD:
#
#   curl -fsSL .../install.sh | sh -s -- v0.5.0
#
# PIPE SAFETY: this entire script is one function, called on the LAST
# line. `sh` reads a `curl | sh` pipe as a stream of source text; if the
# connection drops mid-transfer, sh hits EOF partway through main()'s
# body. Either the function never closes (a syntax error -- nothing
# defined, nothing run) or it closes just short of the final `main "$@"`
# line (a function gets DEFINED but never CALLED). Both are a no-op. A
# script that runs top-level commands as they arrive would instead
# execute half an installer against a truncated read -- do not restructure
# this file to do that. Everything, including variable assignments that
# look side-effect-free today, belongs inside main().

INSTALL_SH_VERSION="1.0.0"
DOXA_REPO_URL="https://github.com/docwilde/doxa"
DOXA_RAW_BASE="https://raw.githubusercontent.com/docwilde/doxa"

main() {
  set -eu

  ref="${1:-main}"

  _info() { printf 'doxa-install: %s\n' "$*"; }
  _warn() { printf 'doxa-install: %s\n' "$*" >&2; }
  _fail() { printf 'doxa-install: %s\n' "$*" >&2; exit 1; }
  _need() { command -v "$1" >/dev/null 2>&1; }

  # Reads the answer from the CONTROLLING TERMINAL, never from stdin --
  # under `curl | sh`, fd 0 is the script itself, not a human. A run with
  # no controlling terminal at all (CI, a script calling this script) gets
  # the stated default instead of blocking forever, and says so.
  _confirm() {
    prompt="$1"; default="$2"
    if printf '%s ' "$prompt" > /dev/tty 2>/dev/null \
      && read -r reply < /dev/tty 2>/dev/null
    then
      :
    else
      _warn "no controlling terminal -- defaulting to '${default}' for: ${prompt}"
      reply="$default"
    fi
    reply=$(printf '%s' "${reply:-$default}" | tr '[:upper:]' '[:lower:]')
    case "$reply" in
      y | yes) return 0 ;;
      *) return 1 ;;
    esac
  }

  # $1 >= $2 ? (major.minor dotted versions only -- all this installer
  # ever compares)
  _ver_ge() {
    a_major=$(printf '%s' "$1" | cut -d. -f1)
    a_minor=$(printf '%s' "$1" | cut -d. -f2)
    b_major=$(printf '%s' "$2" | cut -d. -f1)
    b_minor=$(printf '%s' "$2" | cut -d. -f2)
    a_minor=${a_minor:-0}; b_minor=${b_minor:-0}
    if [ "$a_major" -gt "$b_major" ]; then return 0; fi
    if [ "$a_major" -lt "$b_major" ]; then return 1; fi
    [ "$a_minor" -ge "$b_minor" ]
  }

  _info "installer v${INSTALL_SH_VERSION}, target ref: ${ref}"

  # -- git --------------------------------------------------------------
  # Needed both to resolve the ref we're about to report and because `uv
  # tool install git+...` shells out to it internally.
  if ! _need git; then
    _fail "git is required. Fix: install git (e.g. \`apt install git\`, \`brew install git\`, \`dnf install git\`, \`pacman -S git\`), then re-run this installer."
  fi

  # -- curl ---------------------------------------------------------------
  if ! _need curl; then
    _fail "curl is required (this installer uses it to read doxa's minimum Python version). Fix: install curl, then re-run this installer."
  fi

  # -- python -------------------------------------------------------------
  # The minimum comes from the checkout being installed, not a literal
  # baked into this file -- so a future doxa raising its floor doesn't
  # also require remembering to edit this script.
  min_python=$(
    curl -fsSL "${DOXA_RAW_BASE}/${ref}/pyproject.toml" 2>/dev/null \
      | grep -m1 'requires-python' \
      | sed -n 's/.*>=[[:space:]]*\([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p'
  )
  if [ -z "$min_python" ]; then
    min_python="3.11"
    _warn "could not read requires-python from ref '${ref}' -- assuming ${min_python}+"
  fi

  if ! _need python3; then
    _fail "python3 (>=${min_python}) is required. Fix: install it (e.g. \`uv python install ${min_python}\` once uv is installed, or your OS package manager), then re-run this installer."
  fi
  pyver=$(python3 --version 2>&1 | sed -n 's/^Python \([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p')
  if [ -z "$pyver" ] || ! _ver_ge "$pyver" "$min_python"; then
    _fail "found python3 ${pyver:-unknown}, doxa needs ${min_python}+. Fix: install a newer python3 (e.g. \`uv python install ${min_python}\`), then re-run this installer."
  fi
  _info "python3 ${pyver} OK (>= ${min_python})"

  # -- uv -------------------------------------------------------------
  # Never installed silently: missing uv is always OFFERED, with the
  # official installer command shown verbatim, and a headless run still
  # gets a stated default (yes) rather than failing outright, because uv
  # is what the rest of this script needs to do anything.
  if ! _need uv; then
    _warn "uv is not installed (doxa installs via \`uv tool install\`)."
    if _confirm "Install uv now via the official installer? [Y/n]" "y"; then
      _info "running: curl -LsSf https://astral.sh/uv/install.sh | sh"
      if ! curl -LsSf https://astral.sh/uv/install.sh | sh; then
        _fail "the uv installer failed. Fix: install uv yourself -- https://docs.astral.sh/uv/getting-started/installation/ -- then re-run this installer."
      fi
      PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
      export PATH
      if ! _need uv; then
        _fail "uv installed but is not on PATH yet in this shell. Fix: open a new shell (or \`export PATH=\"\$HOME/.local/bin:\$PATH\"\`), then re-run this installer."
      fi
    else
      _fail "uv is required. Fix: install it -- https://docs.astral.sh/uv/getting-started/installation/ -- then re-run this installer."
    fi
  fi
  _info "$(uv --version 2>/dev/null) OK"

  # -- claude CLI: present AND authenticated -----------------------------
  if ! _need claude; then
    _fail "the claude CLI is required -- doxa authenticates through it and never reads ANTHROPIC_API_KEY. Fix: install it -- https://docs.claude.com/en/docs/claude-code -- then run: claude auth login"
  fi
  if ! claude auth status >/dev/null 2>&1; then
    _fail "claude auth login"
  fi
  _info "claude CLI present and authenticated"

  # -- install --------------------------------------------------------
  # git+URL only, deliberately: doxa is not on PyPI, and `uv tool install
  # --force` makes re-running this script (a later ref, or just to pick
  # up main's HEAD again) an update rather than a "already installed"
  # refusal -- the idempotency this installer promises.
  if [ "$ref" = "main" ]; then
    target="git+${DOXA_REPO_URL}"
  else
    target="git+${DOXA_REPO_URL}@${ref}"
  fi
  sha=$(git ls-remote "$DOXA_REPO_URL" "$ref" 2>/dev/null | cut -f1 | head -n1)
  if [ -n "$sha" ]; then
    _info "resolved ${ref} -> ${sha}"
  else
    _warn "could not resolve '${ref}' to a commit ahead of time -- continuing, uv will report a real error if it does not exist"
  fi

  _info "installing: uv tool install --force ${target}"
  if ! uv tool install --force "$target"; then
    _fail "uv tool install failed -- see the output above."
  fi

  # -- ~/.doxa: create if absent, NEVER touch an existing config --------
  doxa_home="${DOXA_HOME:-$HOME/.doxa}"
  if [ -d "$doxa_home" ]; then
    _info "${doxa_home} already exists -- left untouched"
  else
    mkdir -p "$doxa_home"
    chmod 700 "$doxa_home"
    _info "created ${doxa_home}"
  fi

  # -- doctor ---------------------------------------------------------
  # Read-only report from the checkout `uv tool install` just placed on
  # PATH. `|| true`: a check FAILING (e.g. a stale presence file from an
  # old crash) is real information, worth printing, and must never abort
  # an installer whose actual job -- getting doxa onto PATH -- already
  # succeeded a few lines up.
  if _need doxa; then
    doxa doctor || true
  else
    _info "doxa doctor: doxa is not on PATH in this shell yet -- open a new one and run: doxa doctor"
  fi

  _info "done. cd into a project and run: doxa"
}

main "$@"
