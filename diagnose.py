"""
Isolate exactly where org authentication breaks.

Runs four checks in order, from "can we reach the host at all" to "does the
SOAP Metadata endpoint accept this token". The first one that fails tells you
what is actually wrong, instead of every failure looking like INVALID_SESSION_ID.

    python diagnose.py --org dev
    python diagnose.py                # prompts, like spike.py

No credential is ever printed - tokens are shown only as a length and a short
masked prefix.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from typing import Optional, Tuple

import httpx

from flowtool.sfdc import METADATA_NS, _fault_string, _normalise

API_VERSION = "62.0"


def mask(token: str) -> str:
    if len(token) < 12:
        return f"<{len(token)} chars - suspiciously short>"
    return f"{token[:8]}...{token[-4:]} ({len(token)} chars)"


def ok(label: str, detail: str = "") -> None:
    print(f"  [ OK ] {label}" + (f" - {detail}" if detail else ""))


def bad(label: str, detail: str = "") -> None:
    print(f"  [FAIL] {label}" + (f" - {detail}" if detail else ""))


async def check_host(client: httpx.AsyncClient, base: str) -> bool:
    """Unauthenticated: does this host serve the Salesforce API at all?"""
    print("\n1. Host reachable and serving the API")
    try:
        resp = await client.get(f"{base}/services/data/")
    except httpx.HTTPError as exc:
        bad("connection", str(exc))
        return False

    if resp.is_redirect:
        bad("redirected", f"-> {resp.headers.get('location', '?')}")
        print("       This host is not the API host. Use <domain>.my.salesforce.com.")
        return False
    if resp.status_code != 200:
        bad(f"HTTP {resp.status_code}", resp.text[:200])
        return False

    versions = resp.json()
    latest = versions[-1]["version"] if versions else "?"
    ok("reachable", f"latest API version here is {latest}")
    if latest != API_VERSION:
        print(f"       (we request {API_VERSION}; that is fine as long as it is <= {latest})")
    return True


async def check_rest_token(client: httpx.AsyncClient, base: str, token: str) -> bool:
    """Is the token valid at all, independent of SOAP?"""
    print("\n2. Token accepted by the REST API")
    resp = await client.get(
        f"{base}/services/data/v{API_VERSION}/limits",
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code == 200:
        ok("token valid")
        return True

    detail = resp.text[:300]
    bad(f"HTTP {resp.status_code}", detail)
    if "INVALID_SESSION_ID" in detail:
        print("       The token itself is rejected - this is not a SOAP problem.")
        print("       Re-authenticate:  sf org login web --alias dev")
    return False


async def check_identity(client: httpx.AsyncClient, base: str, token: str) -> bool:
    """Which org and user does this token actually belong to?"""
    print("\n3. Token identity")
    resp = await client.get(
        f"{base}/services/oauth2/userinfo",
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code != 200:
        bad(f"HTTP {resp.status_code}", resp.text[:200])
        return False

    info = resp.json()
    ok("identity", f"{info.get('preferred_username')} @ {info.get('organization_id')}")
    token_host = httpx.URL(info.get("urls", {}).get("partner", base)).host
    if token_host and token_host != httpx.URL(base).host:
        bad("host mismatch", f"token belongs to {token_host}, we are calling {httpx.URL(base).host}")
        return False
    return True


async def check_soap(
    client: httpx.AsyncClient, base: str, token: str, rest_ok: bool
) -> bool:
    """The actual path the deployer uses."""
    print("\n4. SOAP Metadata endpoint")
    endpoint = f"{base}/services/Soap/m/{API_VERSION}"
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
        f' xmlns:met="{METADATA_NS}">'
        f"<soapenv:Header><met:SessionHeader><met:sessionId>{token}</met:sessionId>"
        "</met:SessionHeader></soapenv:Header>"
        "<soapenv:Body><met:describeMetadata>"
        f"<met:asOfVersion>{API_VERSION}</met:asOfVersion>"
        "</met:describeMetadata></soapenv:Body></soapenv:Envelope>"
    )
    resp = await client.post(
        endpoint,
        content=envelope.encode("utf-8"),
        headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": '""'},
    )

    if resp.is_redirect:
        bad("redirected", f"-> {resp.headers.get('location', '?')}")
        return False
    if resp.status_code == 200:
        ok("describeMetadata accepted", "the deploy path will work")
        return True

    fault = _fault_string(resp.text)
    bad(f"HTTP {resp.status_code}", fault or resp.text[:300])
    if fault and "INVALID_SESSION_ID" in fault:
        if rest_ok:
            print("       REST accepted this token but SOAP did not. That usually means")
            print("       the org restricts API access, or the user lacks the")
            print("       'Modify Metadata Through Metadata API Functions' permission.")
        else:
            print("       Both REST and SOAP rejected it, so the credential itself is")
            print("       the problem - see step 2 above, not this step.")
    return False


async def run(instance_url: str, token: str) -> int:
    base = _normalise(instance_url)
    print(f"Instance : {instance_url}")
    print(f"Endpoint : {base}")
    print(f"Token    : {mask(token)}")
    if token != token.strip():
        bad("token has surrounding whitespace - that alone can break the header")

    async with httpx.AsyncClient(timeout=60.0) as client:
        if not await check_host(client, base):
            return 1
        rest_ok = await check_rest_token(client, base, token)
        if rest_ok:
            await check_identity(client, base, token)
        soap_ok = await check_soap(client, base, token, rest_ok)

    print()
    if soap_ok:
        print("All good - spike.py --validate should work now.")
        return 0
    print("Stopped at the first failing step above.")
    return 1


def credentials(use_cli: bool, alias: Optional[str]) -> Optional[Tuple[str, str]]:
    if use_cli:
        from flowtool.orgs import SfCliError, get_org

        try:
            org = get_org(alias)
        except SfCliError as exc:
            print(exc, file=sys.stderr)
            return None
        print(f"Credentials from sf CLI org {org.alias} ({org.username})\n")
        return org.instance_url, org.access_token

    instance_url = os.environ.get("SF_INSTANCE_URL") or input("Instance URL: ").strip()
    token = os.environ.get("SF_SESSION_ID") or getpass.getpass("Session ID (hidden): ").strip()
    return (instance_url, token) if instance_url and token else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--org", nargs="?", const="", metavar="ALIAS",
                        help="take credentials from the sf CLI")
    args = parser.parse_args()

    creds = credentials(args.org is not None, args.org or None)
    if not creds:
        return 2
    return asyncio.run(run(*creds))


if __name__ == "__main__":
    raise SystemExit(main())
