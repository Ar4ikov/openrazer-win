"""OpenRGB, and the programs written against its SDK, lighting the daemon's devices.

The daemon answers OpenRGB's SDK protocol (:mod:`.server`, :mod:`.protocol`)
the way an OpenRGB server does, with each of its devices as one RGB
controller (:mod:`.controller`); what clients ask for is put on the devices
by :mod:`.bridge`.  OpenRGB's GUI reaches it from its *SDK Client* tab.
"""
from .bridge import OpenRGBBridge
from .protocol import PORT, VERSION
from .server import OpenRGBServer

__all__ = ['PORT', 'VERSION', 'OpenRGBBridge', 'OpenRGBServer']
