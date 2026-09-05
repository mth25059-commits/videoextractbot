"""
Link sources.

Importing this package registers every provider in `base.REGISTRY`, so a handler
can ask `providers.find_for(url)` without knowing which sources are compiled in.
Each source lives on its own branch and adds exactly one line here.
"""

from .base import (Provider, REGISTRY, Resolved, ResolveError, Stream,  # noqa: F401
                   find_for, get, register)
from . import terabox                                                   # noqa: F401  (registers itself)
from . import faphouse                                                  # noqa: F401  (registers itself)
