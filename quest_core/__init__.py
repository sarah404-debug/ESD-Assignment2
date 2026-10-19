"""Code shared by the Quest App's HTTP service and its gRPC service.

Both services import this package, so they use the same storage (the single
source of truth for quests) instead of each keeping its own copy.
"""
