"""Games that light Razer devices through Razer's Chroma SDK, lit here instead.

Without Synapse, a game finds no Chroma SDK and lights nothing.  The daemon
answers the SDK's REST API (:mod:`.rest`) and draws what games show
(:mod:`.bridge`); games that load the SDK's DLL instead are given a stand-in
for it, built from ``studio/chroma``, that passes their calls on to the same
API.
"""
from .bridge import ChromaBridge
from .grid import ChromaError
from .rest import PORT, ChromaServer

__all__ = ['PORT', 'ChromaBridge', 'ChromaError', 'ChromaServer']
