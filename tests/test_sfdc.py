"""
The Metadata API manifest builders - pure string assembly, no network. The
retrieve/deploy calls themselves are exercised for real by verify.py and
spike.py against a live org, not here.
"""

import io
import zipfile

from flowtool.sfdc import (
    ORG_SUMMARY_TYPE_GROUPS,
    _is_unmanaged,
    _parse_lwc_meta,
    build_deploy_package,
    build_multi_type_package,
    build_package,
)


class TestBuildDeployPackage:
    """
    The multi-type deploy manifest, generalized from build_package (Flow-only)
    so one deploy can bundle Object/Field/Apex/Flow members together - the
    Phase 4 piece the multi-artifact plan calls for.
    """

    def test_matches_build_package_for_a_single_flow(self):
        # build_package becomes a thin wrapper over this - must stay
        # byte-identical, since server.py and validate_flow still call it.
        old = build_package("My_Flow", "<Flow/>", "62.0")
        new = build_deploy_package(
            {"flows/My_Flow.flow": "<Flow/>"}, {"Flow": ["My_Flow"]}, "62.0"
        )
        assert old == new

    def test_bundles_several_types_in_one_zip(self):
        zip_bytes = build_deploy_package(
            files={
                "objects/Invoice__c.object": "<CustomObject/>",
                "objects/Invoice__c/fields/Amount__c.field-meta.xml": "<CustomField/>",
                "classes/Helper.cls": "public class Helper {}",
                "classes/Helper.cls-meta.xml": "<ApexClass/>",
            },
            types={
                "CustomObject": ["Invoice__c"],
                "CustomField": ["Invoice__c.Amount__c"],
                "ApexClass": ["Helper"],
            },
            api_version="62.0",
        )
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            names = set(archive.namelist())
            package_xml = archive.read("package.xml").decode()

        assert names == {
            "package.xml",
            "objects/Invoice__c.object",
            "objects/Invoice__c/fields/Amount__c.field-meta.xml",
            "classes/Helper.cls",
            "classes/Helper.cls-meta.xml",
        }
        assert "<members>Invoice__c</members>" in package_xml
        assert "<members>Invoice__c.Amount__c</members>" in package_xml
        assert "<members>Helper</members>" in package_xml
        assert package_xml.count("<types>") == 3
        assert "<name>CustomObject</name>" in package_xml
        assert "<name>CustomField</name>" in package_xml
        assert "<name>ApexClass</name>" in package_xml

    def test_no_types_produces_an_empty_package(self):
        # A degenerate case that should never crash - an empty plan approved
        # with nothing left to deploy, say.
        zip_bytes = build_deploy_package({}, {}, "62.0")
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            package_xml = archive.read("package.xml").decode()
        assert "<types>" not in package_xml
        assert "<version>62.0</version>" in package_xml


class TestBuildMultiTypePackage:
    def test_one_types_block_per_type_with_its_own_members(self):
        xml = build_multi_type_package(
            {"ApexClass": ["*"], "Flow": ["MyFlow"]}, "62.0"
        )
        assert "<met:types><met:members>*</met:members><met:name>ApexClass</met:name></met:types>" in xml
        assert "<met:types><met:members>MyFlow</met:members><met:name>Flow</met:name></met:types>" in xml
        assert "<met:version>62.0</met:version>" in xml
        assert xml.startswith("<met:unpackaged>")
        assert xml.endswith("</met:unpackaged>")

    def test_more_than_one_member_of_the_same_type(self):
        xml = build_multi_type_package({"Flow": ["A", "B"]}, "62.0")
        assert "<met:members>A</met:members><met:members>B</met:members>" in xml


class TestOrgSummaryTypeGroups:
    def test_every_group_this_build_reads_is_covered(self):
        all_types = {t for g in ORG_SUMMARY_TYPE_GROUPS for t in g["types"]}
        assert all_types == {
            "CustomObject", "ApexClass", "ApexTrigger", "Flow",
            "LightningComponentBundle", "AuraDefinitionBundle", "Profile",
            "CustomMetadata",
        }

    def test_profiles_is_the_only_group_off_by_default(self):
        # 35% of a real org's knowledge base on its own - field-level
        # security repeats per field, per profile.
        off_by_default = {g["group"] for g in ORG_SUMMARY_TYPE_GROUPS if not g["default"]}
        assert off_by_default == {"profiles"}

    def test_group_keys_are_unique(self):
        keys = [g["group"] for g in ORG_SUMMARY_TYPE_GROUPS]
        assert len(keys) == len(set(keys))


class TestIsUnmanaged:
    def test_no_namespace_and_unmanaged_state_passes(self):
        assert _is_unmanaged({"namespace_prefix": None, "manageable_state": "unmanaged"})

    def test_a_namespace_is_excluded_even_with_no_manageable_state(self):
        # listMetadata does not always fill in manageableState, but a
        # namespace alone already means "not the org's own".
        assert not _is_unmanaged({"namespace_prefix": "sales_sfa_flows", "manageable_state": None})

    def test_manageable_state_installed_is_excluded(self):
        # What a subscriber org's listMetadata sets on anything that arrived
        # through an installed managed package.
        assert not _is_unmanaged({"namespace_prefix": None, "manageable_state": "installed"})

    def test_no_manageable_state_at_all_defaults_to_unmanaged(self):
        # Most local components: listMetadata just omits the field.
        assert _is_unmanaged({"namespace_prefix": None, "manageable_state": None})

    def test_beta_and_editable_states_are_excluded_too(self):
        for state in ("beta", "deprecatedEditable", "installedEditable", "released"):
            assert not _is_unmanaged({"namespace_prefix": None, "manageable_state": state})


class TestParseLwcMeta:
    """The inverse of xmlgen_lwc.py's _meta_xml - what retrieve_lwc_component
    reconstructs the IR's structured fields from."""

    def test_empty_sidecar_falls_back_to_defaults(self):
        assert _parse_lwc_meta("") == (False, [], None)

    def test_exposed_with_targets_and_version(self):
        meta = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<LightningComponentBundle xmlns="http://soap.sforce.com/2006/04/metadata">\n'
            "    <apiVersion>60.0</apiVersion>\n"
            "    <isExposed>true</isExposed>\n"
            "    <targets>\n"
            "        <target>lightning__RecordPage</target>\n"
            "        <target>lightning__AppPage</target>\n"
            "    </targets>\n"
            "</LightningComponentBundle>\n"
        )
        assert _parse_lwc_meta(meta) == (True, ["lightning__RecordPage", "lightning__AppPage"], "60.0")

    def test_not_exposed_with_no_targets(self):
        meta = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<LightningComponentBundle xmlns="http://soap.sforce.com/2006/04/metadata">\n'
            "    <apiVersion>60.0</apiVersion>\n"
            "    <isExposed>false</isExposed>\n"
            "</LightningComponentBundle>\n"
        )
        assert _parse_lwc_meta(meta) == (False, [], "60.0")

    def test_round_trips_through_the_real_generator(self):
        # Not a hand-built fixture: generate_lwc's own output, fed back in -
        # pins down that the two halves actually agree with each other.
        from flowtool.ir_lwc import LightningComponent
        from flowtool.xmlgen_lwc import generate_lwc

        component = LightningComponent(
            api_name="foo",
            js="export default class Foo extends LightningElement {}",
            html="<template></template>",
            api_version="61.0",
            is_exposed=True,
            targets=["lightning__HomePage"],
        )
        files = generate_lwc(component)
        is_exposed, targets, api_version = _parse_lwc_meta(files["lwc/foo/foo.js-meta.xml"])
        assert (is_exposed, targets, api_version) == (True, ["lightning__HomePage"], "61.0")
