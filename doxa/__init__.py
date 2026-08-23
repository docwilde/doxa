"""DOXA -- a terminal for working with a Claude agent whose memory you can audit.

See /README.md for the project pitch and /PHASE0_FINDINGS.md for the SDK
lifecycle findings this package's engine is built on.
"""

from . import _lore_bootstrap  # noqa: F401 -- sys.path shim, see module docstring

__version__ = "0.2.0"

__all__: list[str] = ["__version__"]
