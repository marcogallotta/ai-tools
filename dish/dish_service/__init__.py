"""Shared HTTP service for the dish workflow."""

__all__ = ["DishService", "ServiceConfig"]


def __getattr__(name: str):
    if name == "DishService":
        from .application import DishService

        globals()[name] = DishService
        return DishService
    if name == "ServiceConfig":
        from .config import ServiceConfig

        globals()[name] = ServiceConfig
        return ServiceConfig
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
