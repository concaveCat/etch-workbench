"""Template plugin package.

This file makes the example a regular package. Copy the whole directory to
``plugins/<id>/`` and replace the placeholder modules.
"""

from __future__ import annotations

from .register import register
from .spec import ExampleSpec

__all__ = ["ExampleSpec", "register"]
