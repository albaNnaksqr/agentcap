"""agentcap — capture agent trajectory + git environment at the source.
v0.2 trust core: snapshot -> canonical manifest -> reconstruct -> verify (hash-compare).
See docs/CAPTURE_v0.2_spec.md."""
from .snapshot import snapshot, load_manifest
from .verify import verify, reconstruct

__all__ = ["snapshot", "load_manifest", "verify", "reconstruct"]
