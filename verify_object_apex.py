"""
Object/Field/Apex shapes this tool emits, checked against a real dev org.

    python verify_object_apex.py --org dev

Development QA only - not part of the shipped tool (pyproject.toml only
packages server.py and survey.py as top-level modules; this stays a repo-root
script like verify.py, diagnose.py and spike.py). Sibling to verify.py, which
already does this for Flow shapes over the SOAP Metadata API; this covers the
newer Object/Field/Apex generators (flowtool/ir_object.py, flowtool/ir_apex.py)
the same way - checkOnly, so nothing is created or deployed.

Credentials come from the Salesforce CLI (`sf`) via flowtool.orgs.get_org -
the same "use the CLI, don't paste a token" path verify.py already relies on.
That is the actual role the CLI plays here: it resolves *who* the org is: the
checkOnly deploy itself goes through flowtool.sfdc.validate_bundle, the same
multi-type package builder and SOAP client the shipped tool now uses (Phase 4
of the multi-artifact plan), so this exercises the identical path.

Several shapes below bundle an object, its field and an Apex class in one
package.xml - one deploy, several component types. That answers the open
question Phase 4 flagged: does the Metadata API resolve an
Object -> Field -> Apex dependency within a single transaction. (It was
answered by hand, with a local package builder, before validate_bundle
existed - this script now calls the real thing instead.)

    python verify_object_apex.py --org dev
    python verify_object_apex.py --org dev --only apex
    python verify_object_apex.py --list
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from typing import Dict, List

from flowtool.ir_apex import ApexClass
from flowtool.ir_object import CustomField, CustomObject
from flowtool.sfdc import validate_bundle
from flowtool.xmlgen_apex import generate_apex
from flowtool.xmlgen_object import generate_field_delta, generate_object

API_VERSION = "62.0"


@dataclass
class Shape:
    group: str
    name: str
    files: Dict[str, str] = field(default_factory=dict)
    types: Dict[str, List[str]] = field(default_factory=dict)
    note: str = ""


# --------------------------------------------------------------------------
# Shapes
# --------------------------------------------------------------------------
#
# Each one packages real IR -> real xmlgen_object/xmlgen_apex output, the
# exact bytes the shipped tool would produce, so a pass here means the
# compiler's output is something Salesforce actually accepts - not just
# something the IR considers well-formed.

def _object_shape(
    group: str, name: str, obj: CustomObject, fields=(), note: str = "",
) -> Shape:
    """
    `fields`, if given, are embedded inside the object's own .object file -
    see xmlgen_object.py's module docstring for why that replaced deploying
    a new object's fields as separate CustomField members.
    """
    return Shape(
        group=group, name=name, note=note,
        files={f"objects/{obj.api_name}.object": generate_object(obj, list(fields))},
        types={"CustomObject": [obj.api_name]},
    )


def _field_shape(group: str, name: str, fld: CustomField, note: str = "") -> Shape:
    """
    A field on an object this deploy does *not* also create - a delta
    .object file holding only this field, confirmed live and against
    Salesforce's own `sf project convert source` output (see
    xmlgen_object.py's module docstring). There is no separate per-field
    file format; the standalone-CustomField-file convention this shape used
    before failed checkOnly live for every field tried, regardless of
    naming or extension.
    """
    return Shape(
        group=group, name=name, note=note,
        files={f"objects/{fld.object_api_name}.object": generate_field_delta([fld])},
        types={"CustomField": [f"{fld.object_api_name}.{fld.api_name}"]},
    )


def _apex_shape(group: str, name: str, cls: ApexClass, note: str = "") -> Shape:
    body, meta = generate_apex(cls)
    return Shape(
        group=group, name=name, note=note,
        files={
            f"classes/{cls.api_name}.cls": body,
            f"classes/{cls.api_name}.cls-meta.xml": meta,
        },
        types={"ApexClass": [cls.api_name]},
    )


def _merged(*shapes: Shape, group: str, name: str, note: str = "") -> Shape:
    """Combine several shapes' files/types into one package - one deploy."""
    files: Dict[str, str] = {}
    types: Dict[str, List[str]] = {}
    for shape in shapes:
        files.update(shape.files)
        for type_name, members in shape.types.items():
            types.setdefault(type_name, []).extend(members)
    return Shape(group=group, name=name, note=note, files=files, types=types)


_BUNDLE_OBJECT = CustomObject(
    api_name="FlowToolVerify_Bundle", label="Flow Tool Verify Bundle",
    plural_label="Flow Tool Verify Bundles",
)
_BUNDLE_FIELD = CustomField(
    api_name="Amount", label="Amount", type="Number",
    object_api_name=_BUNDLE_OBJECT.api_name, precision=18, scale=2,
)
_BUNDLE_APEX = ApexClass(
    api_name="FlowToolVerify_Helper",
    body="public class FlowToolVerify_Helper {\n"
         "    public static Integer doubled(Integer n) {\n"
         "        return n * 2;\n"
         "    }\n"
         "}",
)

SHAPES: List[Shape] = [
    _object_shape("object", "text name field", CustomObject(
        api_name="FlowToolVerify_Invoice", label="Flow Tool Verify Invoice",
        plural_label="Flow Tool Verify Invoices",
    )),
    _object_shape("object", "autonumber name field", CustomObject(
        api_name="FlowToolVerify_Invoice_AN", label="Flow Tool Verify Invoice AN",
        plural_label="Flow Tool Verify Invoice ANs",
        record_name_type="AutoNumber", record_name_display_format="INV-{0000}",
    )),
    _field_shape("field", "text field on a standard object", CustomField(
        api_name="FlowToolVerify_Note", label="Flow Tool Verify Note",
        type="Text", object_api_name="Account",
    )),
    _field_shape("field", "picklist field on a standard object", CustomField(
        api_name="FlowToolVerify_Stage", label="Flow Tool Verify Stage",
        type="Picklist", object_api_name="Account",
        picklist_values=["Draft", "Sent", "Paid"],
    )),
    _field_shape("field", "checkbox field on a standard object", CustomField(
        api_name="FlowToolVerify_Flag", label="Flow Tool Verify Flag",
        type="Checkbox", object_api_name="Account",
    )),
    _object_shape(
        "bundle", "object with an embedded field, one deploy", _BUNDLE_OBJECT,
        fields=[_BUNDLE_FIELD],
        note="the field is embedded in the .object file, not a separate "
             "CustomField member - see xmlgen_object.py's module docstring",
    ),
    _merged(
        _object_shape("bundle", "object", _BUNDLE_OBJECT, fields=[_BUNDLE_FIELD]),
        _apex_shape("bundle", "apex", _BUNDLE_APEX),
        group="bundle", name="object+field embedded + apex, one deploy",
        note="the Phase 4 question with a third component type in the mix: "
             "does an object (with its field embedded) and an unrelated Apex "
             "class resolve within one transaction",
    ),
    _apex_shape("apex", "a plain class compiles", ApexClass(
        api_name="FlowToolVerify_Plain",
        body="public class FlowToolVerify_Plain {\n"
             "    public FlowToolVerify_Plain() {}\n"
             "}",
    )),
]


async def run_shapes(org_alias: str, only: str) -> int:
    from flowtool.orgs import SfCliError, get_org

    try:
        org = get_org(org_alias)
    except SfCliError as problem:
        print(f"\n  cannot reach an org: {problem}")
        return 1

    print(f"\n  org: {org.alias or org.username}")
    chosen = [
        shape for shape in SHAPES
        if not only or only in f"{shape.group} {shape.name}".lower()
    ]

    limit = asyncio.Semaphore(3)

    async def check(shape: Shape):
        async with limit:
            result = await validate_bundle(
                org.instance_url, org.access_token, shape.files, shape.types,
                api_version=API_VERSION, check_only=True,
            )
        return shape, result

    done = await asyncio.gather(*(check(shape) for shape in chosen))

    failures = 0
    group = ""
    for shape, result in done:
        if shape.group != group:
            group = shape.group
            print(f"\n  {group}")
        failures += 0 if result.success else 1
        print(f"    {'ok  ' if result.success else 'FAIL'}  {shape.name}")
        if shape.note:
            print(f"            {shape.note}")
        for problem in result.failures[:3]:
            print(f"            {problem}")
        if result.error_message and not result.failures:
            print(f"            {result.error_message[:200]}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check Object/Field/Apex shapes this tool emits against an org."
    )
    parser.add_argument("--org", metavar="ALIAS", help="validate against this org (checkOnly)")
    parser.add_argument("--only", default="",
                        help="run only shapes whose group or name contains this")
    parser.add_argument("--list", action="store_true", help="list the shapes and exit")
    args = parser.parse_args()
    only = args.only.lower()

    if args.list:
        for shape in SHAPES:
            print(f"  {shape.group:8} {shape.name}")
        return 0

    if not args.org:
        parser.error("pass --org ALIAS (an org the sf CLI is authenticated against)")

    print(__doc__.strip().splitlines()[0])
    failures = asyncio.run(run_shapes(args.org, only))

    print()
    if failures:
        print(f"{failures} failed.")
        return 1
    print("All good.")
    return 0


if __name__ == "__main__":
    from pathlib import Path

    from flowtool.config import load_env

    load_env(Path(__file__).parent)
    sys.exit(main())
