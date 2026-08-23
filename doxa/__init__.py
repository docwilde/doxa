"""DOXA -- a terminal for working with a Claude agent whose memory you can audit.

See /README.md for the project pitch and /PHASE0_FINDINGS.md for the SDK
lifecycle findings this package's engine is built on.
"""

from . import _lore_bootstrap  # noqa: F401 -- sys.path shim, see module docstring

from .version import resolve_version

# ONE version, from pyproject.toml (checkout) or the distribution metadata
# built from it (installed) -- never a second literal to forget to bump.
__version__ = resolve_version()

__all__: list[str] = ["__version__"]
