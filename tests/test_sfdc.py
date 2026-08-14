"""
The Metadata API manifest builders - pure string assembly, no network. The
retrieve/deploy calls themselves are exercised for real by verify.py and
spike.py against a live org, not here.
"""

from flowtool.sfdc import ORG_SUMMARY_TYPES, _is_unmanaged, build_multi_type_package


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

    def test_org_summary_types_covers_what_metadata_kb_worker_parses(self):
        # Objects, formulas (carried on CustomObject), flows, Apex, LWC/Aura,
        # profiles, custom metadata - metadata-kb.html's own stated scope.
        assert set(ORG_SUMMARY_TYPES) == {
            "CustomObject", "ApexClass", "ApexTrigger", "Flow",
            "LightningComponentBundle", "AuraDefinitionBundle", "Profile",
            "CustomMetadata",
        }
        assert all(members == ["*"] for members in ORG_SUMMARY_TYPES.values())


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
