"""
The pipeline, end to end:

    description -> IR -> graph -> approval -> XML -> checkOnly -> deploy

Nothing reaches the org without the user approving the graph first, and any
change to an approved flow - including one the model makes to fix a Salesforce
error - goes back through approval. The graph the user says yes to is always the
graph that gets deployed, because both come from the same IR.

    python forge.py "when an opportunity is won, mark its account hot" --org dev
    python forge.py --file request.txt --org dev --out build/
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import getpass
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# Windows consoles default to cp1252 and choke on anything outside it.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from flowtool.config import env_help, load_env
from flowtool.ir import Flow
from flowtool.llm import (
    AnthropicProvider,
    FlowGenerator,
    GeminiProvider,
    GenerationResult,
    LLMError,
    OllamaProvider,
    Provider,
)
from flowtool.mermaid import to_markdown, to_mermaid
from flowtool.sfdc import DeployResult, flow_builder_url, validate_flow
from flowtool.xmlgen import generate as generate_xml

MAX_SALESFORCE_REPAIRS = 3


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def heading(text: str) -> None:
    print(f"\n{'=' * 68}\n{text}\n{'=' * 68}")


def step(text: str) -> None:
    print(f"\n-- {text}")


def show_flow(flow: Flow) -> None:
    heading(f"{flow.label}  ({flow.api_name})")
    if flow.description:
        print(f"\n{flow.description}")
    print(f"\n{to_mermaid(flow)}\n")
    print(summarise(flow))


def summarise(flow: Flow) -> str:
    """A prose reading of the flow, for people who don't read Mermaid source."""
    lines = []
    if flow.process_type == "Flow":
        lines.append("Trigger: screen flow, run by a user")
    elif flow.start.object:
        lines.append(
            f"Trigger: {flow.start.object} / {flow.start.record_trigger_type} "
            f"/ {flow.start.trigger_type}"
        )
    else:
        lines.append("Trigger: autolaunched")
    lines.append(f"Elements: {len(flow.elements)}")
    if flow.variables:
        lines.append(f"Variables: {', '.join(v.name for v in flow.variables)}")

    # Status decides whether deploying starts running this against live records,
    # so it belongs on the screen the user approves.
    if flow.status == "Active":
        lines.append(f"Status: ACTIVE - runs against live records as soon as it deploys")
    else:
        lines.append(f"Status: Draft (deploys without running)")
    lines.append(f"API version: {flow.api_version}")
    return "\n".join(lines)


def apply_policy(flow: Flow, args: argparse.Namespace) -> Flow:
    """
    Status and API version are deployment policy, not business logic. Letting
    the model choose them means an offhand request can produce a flow that goes
    live the moment it deploys, so they are set here instead.
    """
    flow.status = "Active" if args.activate else "Draft"
    flow.api_version = args.api_version
    return flow


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------


def ask(prompt: str, options: List[str]) -> str:
    joined = "/".join(options)
    while True:
        answer = input(f"{prompt} [{joined}]: ").strip().lower()
        for option in options:
            if answer == option or (answer and option.startswith(answer)):
                return option
        print(f"  Please answer one of: {joined}")


def credentials(use_cli: bool, alias: Optional[str]) -> Optional[Tuple[str, str]]:
    if use_cli:
        from flowtool.orgs import SfCliError, get_org

        try:
            org = get_org(alias)
        except SfCliError as exc:
            print(exc, file=sys.stderr)
            return None
        print(f"Org: {org.alias} ({org.username}) at {org.instance_url}")
        return org.instance_url, org.access_token

    instance_url = os.environ.get("SF_INSTANCE_URL") or input("Instance URL: ").strip()
    token = os.environ.get("SF_SESSION_ID") or getpass.getpass("Session ID (hidden): ").strip()
    return (instance_url, token) if instance_url and token else None


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------


def design(
    generator: FlowGenerator, request: str, args: argparse.Namespace
) -> Optional[GenerationResult]:
    """Generate, show, and refine until the user approves or quits."""
    step("Designing the flow...")
    result = generator.generate(request)
    if result.repairs:
        print(f"   (repaired its own IR {result.repairs}x before it validated)")

    while True:
        apply_policy(result.flow, args)
        show_flow(result.flow)
        choice = ask("\nApprove this flow?", ["approve", "refine", "quit"])
        if choice == "approve":
            return result
        if choice == "quit":
            return None

        instruction = input("What should change? ").strip()
        if not instruction:
            continue
        step("Revising...")
        try:
            result = generator.refine(result, instruction)
        except LLMError as exc:
            print(f"   Could not revise: {exc}")
        else:
            if result.repairs:
                print(f"   (repaired its own IR {result.repairs}x)")


async def check(
    flow: Flow, instance_url: str, token: str, check_only: bool = True
) -> DeployResult:
    xml = generate_xml(flow)
    return await validate_flow(
        instance_url, token, flow.api_name, xml, api_version=flow.api_version,
        check_only=check_only,
    )


def report(result: DeployResult) -> List[str]:
    """Print the outcome and return the failure lines, ready to feed back."""
    print(f"   Status : {result.status}")
    print(f"   Success: {result.success}")
    if result.success:
        return []

    failures = [str(failure) for failure in result.failures]
    if not failures and result.error_message:
        failures = [result.error_message]
    for failure in failures:
        print(f"   {failure}")
    return failures


def write_artifacts(flow: Flow, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{flow.api_name}.flow-meta.xml").write_text(generate_xml(flow), encoding="utf-8")
    (out / f"{flow.api_name}.md").write_text(to_markdown(flow), encoding="utf-8")
    (out / f"{flow.api_name}.ir.json").write_text(
        flow.model_dump_json(exclude_none=True, indent=2), encoding="utf-8"
    )
    print(f"   Wrote {out / flow.api_name}.{{flow-meta.xml, md, ir.json}}")


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


async def run(args: argparse.Namespace, provider: Provider, request: str) -> int:
    generator = FlowGenerator(provider)

    approved = design(generator, request, args)
    if approved is None:
        print("\nStopped. Nothing was sent to Salesforce.")
        return 1

    if args.out:
        step("Writing artifacts")
        write_artifacts(approved.flow, Path(args.out))

    if args.no_validate:
        print("\nSkipping validation (--no-validate).")
        return 0

    creds = credentials(args.org is not None, args.org or None)
    if not creds:
        return 2
    instance_url, token = creds

    for attempt in range(MAX_SALESFORCE_REPAIRS + 1):
        step(f"Validating against the org (checkOnly), attempt {attempt + 1}")
        result = await check(approved.flow, instance_url, token, check_only=True)
        failures = report(result)
        if result.success:
            break

        if attempt == MAX_SALESFORCE_REPAIRS:
            print("\nOut of repair attempts. The IR is in the artifacts if you want to edit it.")
            return 1

        if ask("\nAsk the model to fix these?", ["yes", "no"]) == "no":
            return 1

        step("Repairing from the org's errors...")
        try:
            approved = generator.repair_from_salesforce(approved, failures)
        except LLMError as exc:
            print(f"   Repair failed: {exc}")
            return 1

        # The flow changed, so the previous approval no longer covers it.
        print("\nThe flow changed. Review it again before it goes any further.")
        apply_policy(approved.flow, args)
        show_flow(approved.flow)
        if ask("\nApprove the revised flow?", ["approve", "quit"]) == "quit":
            return 1
        if args.out:
            write_artifacts(approved.flow, Path(args.out))

    print("\nValidation passed. This flow will deploy cleanly.")

    if not args.deploy:
        print("Re-run with --deploy to deploy it, or take the XML from the artifacts.")
        return 0

    heading("DEPLOY")
    print(f"About to deploy {approved.flow.api_name} to {instance_url}.")
    print("This writes metadata to the org.")
    if approved.flow.status == "Active":
        print(
            "\nStatus is ACTIVE. On deploy this flow starts running against live "
            f"{approved.flow.start.object or 'records'} immediately.\n"
            "Drop --activate to deploy it as a Draft instead."
        )
    if ask("Proceed?", ["yes", "no"]) == "no":
        print("Not deployed.")
        return 0

    step("Deploying")
    result = await check(approved.flow, instance_url, token, check_only=False)
    report(result)
    if result.success:
        link = await flow_builder_url(
            instance_url, token, approved.flow.api_name,
            api_version=approved.flow.api_version,
        )
        print(f"\n   Open it: {link}")
    print(f"\nTokens: {provider.usage}")
    return 0 if result.success else 1


PROVIDERS = {"anthropic": AnthropicProvider, "gemini": GeminiProvider, "ollama": OllamaProvider}


def choose_provider(name: Optional[str]) -> str:
    """Pick a provider from whichever key is actually set, unless told which."""
    if name:
        return name
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    if os.environ.get("OLLAMA_API_KEY"):
        return "ollama"
    # Never put a key in this file - it is the one place it would get committed.
    raise LLMError(env_help(Path(__file__).parent))


def build_provider(args: argparse.Namespace) -> Provider:
    name = choose_provider(args.provider)
    options = {"effort": args.effort}
    if args.model:
        options["model"] = args.model
    provider = PROVIDERS[name](**options)
    print(f"Model: {provider.name} / {provider.model}")
    return provider


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="   %(message)s")
    loaded = load_env(Path(__file__).parent)
    if loaded:
        print(f"Loaded from .env: {', '.join(loaded)}")

    parser = argparse.ArgumentParser(description="Describe a flow; get a deployable Flow.")
    parser.add_argument("request", nargs="?", help="what the flow should do")
    parser.add_argument("--file", help="read the request from a file instead")
    parser.add_argument("--org", nargs="?", const="", metavar="ALIAS",
                        help="take org credentials from the sf CLI")
    parser.add_argument("--out", help="directory to write XML, Markdown, and IR into")
    parser.add_argument("--deploy", action="store_true",
                        help="offer to deploy after validation passes")
    parser.add_argument("--activate", action="store_true",
                        help="deploy the flow Active. Without this it deploys as "
                             "a Draft and does not run.")
    parser.add_argument("--api-version", default="62.0",
                        help="Salesforce API version (default 62.0)")
    parser.add_argument("--no-validate", action="store_true",
                        help="design only; never contact the org")
    parser.add_argument("--provider", choices=sorted(PROVIDERS),
                        help="defaults to whichever API key is set")
    parser.add_argument("--model", help="overrides the provider's default model")
    parser.add_argument("--effort", default="medium",
                        choices=["medium", "high"])
    args = parser.parse_args()

    if args.file:
        request = Path(args.file).read_text(encoding="utf-8").strip()
    elif args.request:
        request = args.request
    else:
        parser.error("give a request as an argument or with --file")

    try:
        provider = build_provider(args)
    except LLMError as exc:
        print(exc, file=sys.stderr)
        return 2

    try:
        return asyncio.run(run(args, provider, request))
    except LLMError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. Nothing was sent to Salesforce.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
