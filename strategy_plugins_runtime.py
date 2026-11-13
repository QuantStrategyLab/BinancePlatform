"""Strategy plugin surface reserved for layout parity with other platforms.

Binance live runtime does not mount strategy-plugin alerts; this module exists
so the platform HTTP/layout split stays consistent with QuantPlatformKit's
``platform_http`` template without introducing a star-import shim.
"""

from __future__ import annotations

__all__: list[str] = []
