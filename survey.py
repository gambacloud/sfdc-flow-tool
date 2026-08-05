"""
Measure the gap between what an org contains and what this tool can model.

Until now gaps surfaced one at a time, whenever someone happened to open a flow
that hit one. This retrieves every flow in an org, parses each, and counts what
actually blocks them - so "what should we build next" is a number rather than
an opinion.

    python survey.py --org dev
    python survey.py --org dev --json report.json
    python survey.py --dir ./some/flows          # offline, from .flow files

Nothing is written to the org. Retrieve is read-only.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from flowtool.config import load_env
from flowtool.parse import UnsupportedFlow, parse_flow
from flowtool.xmlgen import generate

BAR_WIDTH = 28


# --------------------------------------------------------------------------
# Survey
# --------------------------------------------------------------------------


class Survey:
    def __init__(self) -> None:
        self.parsed: List[str] = []
        self.refused: Dict[str, List[str]] = {}          # flow -> detail lines
        self.codes: Counter = Counter()                   # code -> flows blocked
        self.flows_by_code: Dict[str, List[str]] = defaultdict(list)
        self.round_trip_failures: List[Tuple[str, str]] = []
        self.element_counts: Counter = Counter()

    def add(self, api_name: str, xml: str) -> None:
        try:
            flow = parse_flow(xml, api_name=api_name)
        except UnsupportedFlow as exc:
            self.refused[api_name] = exc.reasons
            # Count each code once per flow: a flow with three screens is one
            # flow blocked by screens, not three.
            for code in set(exc.codes):
                self.codes[code] += 1
                self.flows_by_code[code].append(api_name)
            return
        except Exception as exc:  # a parser crash is a finding too
            self.refused[api_name] = [f"the parser raised {type(exc).__name__}: {exc}"]
            self.codes["parser_error"] += 1
            self.flows_by_code["parser_error"].append(api_name)
            return

        self.parsed.append(api_name)
        for element in flow.elements:
            self.element_counts[type(element).__name__] += 1

        # A flow that parses but does not survive a round trip is worse than one
        # that is refused: it would look editable and lose something on deploy.
        try:
            returned = parse_flow(generate(flow), api_name=api_name)
        except Exception as exc:
            self.round_trip_failures.append((api_name, f"re-parse failed: {exc}"))
            return

        before = {e["name"]: e for e in flow.model_dump()["elements"]}
        after = {e["name"]: e for e in returned.model_dump()["elements"]}
        if before != after:
            changed = sorted(
                name for name in before
                if before.get(name) != after.get(name)
            ) or ["the flow's own fields"]
            self.round_trip_failures.append((api_name, f"changed: {', '.join(changed)}"))

    @property
    def total(self) -> int:
        return len(self.parsed) + len(self.refused)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def bar(count: int, largest: int) -> str:
    if largest <= 0:
        return ""
    filled = max(1, round(BAR_WIDTH * count / largest))
    return "#" * filled


def report(survey: Survey, verbose: bool) -> None:
    total = survey.total
    if not total:
        print("No flows found.")
        return

    parsed = len(survey.parsed)
    print()
    print(f"{total} flows:  {parsed} parse, {len(survey.refused)} refused "
          f"({parsed * 100 // total}% covered)")

    if survey.codes:
        print("\nWhat blocks them, by how many flows each blocks:\n")
        largest = survey.codes.most_common(1)[0][1]
        for code, count in survey.codes.most_common():
            print(f"  {count:>4}  {bar(count, largest):<{BAR_WIDTH}}  {code}")

        print("\nEach of those, spelled out:\n")
        for code, count in survey.codes.most_common():
            names = survey.flows_by_code[code]
            shown = ", ".join(names[:3])
            if len(names) > 3:
                shown += f", +{len(names) - 3} more"
            print(f"  {code}")
            print(f"      {count} flow{'' if count == 1 else 's'}: {shown}")

    if survey.element_counts:
        print("\nElements across the flows that do parse:\n")
        for name, count in survey.element_counts.most_common():
            print(f"  {count:>4}  {name}")

    # This is the one that matters most: it means a flow looks safe to edit and
    # is not. It should always be empty.
    if survey.round_trip_failures:
        print("\n!! Parsed but did NOT survive a round trip "
              f"({len(survey.round_trip_failures)}):\n")
        for name, detail in survey.round_trip_failures:
            print(f"  {name}: {detail}")
    elif survey.parsed:
        print("\nEvery flow that parsed also survived a round trip.")

    if verbose and survey.refused:
        print("\nRefusals in full:\n")
        for name, reasons in sorted(survey.refused.items()):
            print(f"  {name}")
            for reason in reasons:
                print(f"      - {reason}")

    if survey.codes:
        top, count = survey.codes.most_common(1)[0]
        print(f"\nBiggest single win: supporting {top} would unblock "
              f"{count} of {total} flows.")


def as_json(survey: Survey) -> dict:
    return {
        "total": survey.total,
        "parsed": sorted(survey.parsed),
        "refused": {name: reasons for name, reasons in sorted(survey.refused.items())},
        "codes": dict(survey.codes.most_common()),
        "flows_by_code": {k: sorted(v) for k, v in survey.flows_by_code.items()},
        "round_trip_failures": [
            {"flow": name, "detail": detail}
            for name, detail in survey.round_trip_failures
        ],
        "element_counts": dict(survey.element_counts.most_common()),
    }


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


def from_directory(directory: Path) -> Dict[str, str]:
    flows = {}
    for path in sorted(directory.rglob("*.flow*")):
        if path.suffix not in (".flow", ".xml"):
            continue
        name = path.name.split(".")[0]
        flows[name] = path.read_text(encoding="utf-8")
    return flows


async def from_org(org: Optional[str], use_cli: bool) -> Dict[str, str]:
    from flowtool.sfdc import retrieve_all_flows

    if use_cli:
        from flowtool.orgs import SfCliError, get_org

        try:
            found = get_org(org)
        except SfCliError as exc:
            print(exc, file=sys.stderr)
            return {}
        print(f"Org: {found.alias} ({found.username})")
        instance_url, token = found.instance_url, found.access_token
    else:
        instance_url = os.environ.get("SF_INSTANCE_URL") or input("Instance URL: ").strip()
        token = os.environ.get("SF_SESSION_ID") or getpass.getpass(
            "Session ID (hidden): "
        ).strip()

    if not instance_url or not token:
        print("Need an instance url and a token.", file=sys.stderr)
        return {}

    print("Retrieving every flow in one package (this takes a moment)...")
    return await retrieve_all_flows(instance_url, token)


# --------------------------------------------------------------------------


def main() -> int:
    load_env(Path(__file__).parent)

    parser = argparse.ArgumentParser(description="Measure org coverage.")
    parser.add_argument("--org", nargs="?", const="", metavar="ALIAS",
                        help="take credentials from the sf CLI")
    parser.add_argument("--dir", help="survey .flow files on disk instead of an org")
    parser.add_argument("--json", metavar="PATH", help="also write the report as JSON")
    parser.add_argument("--save", metavar="DIR",
                        help="keep the retrieved .flow files, for offline re-runs")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="list every refusal in full")
    args = parser.parse_args()

    if args.dir:
        flows = from_directory(Path(args.dir))
    else:
        flows = asyncio.run(from_org(args.org or None, args.org is not None))

    if not flows:
        print("Nothing to survey.")
        return 1

    if args.save:
        out = Path(args.save)
        out.mkdir(parents=True, exist_ok=True)
        for name, xml in flows.items():
            (out / f"{name}.flow").write_text(xml, encoding="utf-8")
        print(f"Saved {len(flows)} flows to {out}")

    survey = Survey()
    for name, xml in sorted(flows.items()):
        survey.add(name, xml)

    report(survey, args.verbose)

    if args.json:
        Path(args.json).write_text(
            json.dumps(as_json(survey), indent=2), encoding="utf-8"
        )
        print(f"\nWrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
