"""Shared HTTP service for the dish workflow."""

from .application import DishService
from .config import ServiceConfig

__all__ = ["DishService", "ServiceConfig"]
