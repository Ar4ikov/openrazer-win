"""What the lighting can follow from outside the devices.

The Windows accent colour lives here; games lighting devices through Razer's
Chroma SDK have a package of their own, :mod:`openrazer_win.chroma`.
"""
from .accent import AccentWatch, read_accent
from .settings import IntegrationSettings

__all__ = ['AccentWatch', 'IntegrationSettings', 'read_accent']
