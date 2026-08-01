"""Named client-profile resolution shared by the Dish CLIs."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Sequence

from .errors import DishRuleError


PROFILE_NAMES = ("prod", "test")


@dataclass(frozen=True)
class ClientProfile:
    name: str
    service_url: str
    token: str


def add_profile_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile",
        choices=PROFILE_NAMES,
        default=argparse.SUPPRESS,
        help="client environment (default: DISH_PROFILE or prod)",
    )


def profile_from_argv(argv: Sequence[str]) -> str | None:
    for index, argument in enumerate(argv):
        if argument == "--profile":
            return argv[index + 1] if index + 1 < len(argv) else None
        if argument.startswith("--profile="):
            return argument.partition("=")[2]
    return None


def argv_without_profile(argv: Sequence[str]) -> list[str]:
    result: list[str] = []
    skip_value = False
    for argument in argv:
        if skip_value:
            skip_value = False
            continue
        if argument == "--profile":
            skip_value = True
            continue
        if argument.startswith("--profile="):
            continue
        result.append(argument)
    return result


def resolve_client_profile(requested: str | None, *, admin: bool) -> ClientProfile:
    configured = os.environ.get("DISH_PROFILE", "").strip()
    name = (requested or configured or "prod").strip().lower()
    if name not in PROFILE_NAMES:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "DISH_PROFILE must be prod or test",
            rule="dish_profile_invalid",
        )
    suffix = name.upper()
    token_prefix = "DISH_ADMIN_TOKEN" if admin else "DISH_SERVICE_TOKEN"
    named_configuration_present = bool(
        requested
        or configured
        or os.environ.get("DISH_SERVICE_URL_TEST")
        or os.environ.get("DISH_SERVICE_URL_PROD")
        or os.environ.get(f"{token_prefix}_TEST")
        or os.environ.get(f"{token_prefix}_PROD")
    )
    if named_configuration_present:
        service_url = os.environ.get(f"DISH_SERVICE_URL_{suffix}", "").strip()
        token = os.environ.get(f"{token_prefix}_{suffix}", "").strip()
    else:
        service_url = os.environ.get("DISH_SERVICE_URL", "").strip()
        token = os.environ.get(token_prefix, "").strip()
    return ClientProfile(name=name, service_url=service_url, token=token)
