"""Renew only the already-stored release refresh credential; never opens a browser."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from .release_hot import OperationError, atomic_json


def refresh_token(target, budget):
    path = target.path("refresh_file")
    if path.stat().st_mode & 0o077:
        raise OperationError(
            "TOKEN_PERMISSIONS", "refresh credential requires mode 0600"
        )
    state = json.loads(path.read_text())
    url = target.values["mcp_url"].removesuffix("/mcp") + "/token"
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": state["refresh_token"],
            "client_id": state["client_id"],
            "resource": target.values["mcp_url"],
        }
    ).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, body), timeout=budget.left()
        ) as response:
            tokens = json.load(response)
        value = tokens["access_token"].strip()
        if not value or "\n" in value or "\r" in value:
            raise ValueError("invalid credential")
        access = target.path("token_file")
        temporary = access.with_name("." + access.name + ".renew")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, access)
        atomic_json(
            path,
            {
                "client_id": state["client_id"],
                "refresh_token": tokens.get("refresh_token", state["refresh_token"]),
            },
        )
    except Exception as exc:
        raise OperationError(
            "INTERACTIVE_RENEWAL_REQUIRED",
            "stored refresh unavailable; run dish/scripts/dish-release-credential sign-in before deployment",
        ) from exc


def main():
    import argparse

    from .release_hot import Budget, target_config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("renew", "sign-in"))
    args = parser.parse_args()
    target = target_config("prod")
    if args.command == "renew":
        refresh_token(target, Budget(30))
        print("Credential refreshed")
        return 0
    import asyncio

    from fastmcp import Client
    from fastmcp.client.auth import OAuth

    class VisibleOAuth(OAuth):
        async def redirect_handler(self, authorization_url):
            print("OPEN THIS LINK TO SIGN IN:", authorization_url, flush=True)

    async def sign_in():
        auth = VisibleOAuth(
            mcp_url=target.values["mcp_url"],
            client_name="dish-release-conformance",
            callback_timeout=1500,
        )
        async with Client(target.values["mcp_url"], auth=auth) as client:
            await client.list_tools()
            tokens = await auth.token_storage_adapter.get_tokens()
            info = await auth.token_storage_adapter.get_client_info()
        atomic_json(
            target.path("refresh_file"),
            {"client_id": info.client_id, "refresh_token": tokens.refresh_token},
        )
        refresh_token(target, Budget(30))

    asyncio.run(sign_in())
    print("Credential written")
    return 0
