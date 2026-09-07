"""Status-code aliases that stay stable across Starlette renames."""

from __future__ import annotations

# Starlette renamed HTTP_422_UNPROCESSABLE_ENTITY to ..._CONTENT and deprecated
# the old name; referencing the number directly keeps both versions quiet.
HTTP_422_UNPROCESSABLE = 422
HTTP_413_TOO_LARGE = 413
