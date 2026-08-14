"""
The Metadata API manifest builders - pure string assembly, no network. The
retrieve/deploy calls themselves are exercised for real by verify.py and
spike.py against a live org, not here.
"""

from flowtool.sfdc import ORG_SUMMARY_TYPES, build_multi_type_package


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
