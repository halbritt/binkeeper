"""Compatibility name for BinKeeper's narrow database connection seam."""

from binkeeper.database import ServingRoleUnavailableError, connect

__all__ = ["ServingRoleUnavailableError", "connect"]
