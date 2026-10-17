"""portal_preflight against a scripted fake org - no network."""

import asyncio

from flowtool.portal_preflight import (
    BLOCKING, ERROR, FAIL, PASS, SKIP, WARNING,
    OrgQueryError, PortalSpec, format_report, run_preflight,
)


class FakeOrg:
    """Answers queries by substring match; an Exception value is raised."""

    def __init__(self, answers, describes=None):
        self.answers = answers
        self.describes = describes or {}
        self.queries = []

    async def query(self, soql, tooling=False):
        self.queries.append(soql)
        for needle, result in self.answers.items():
            if needle in soql:
                if isinstance(result, Exception):
                    raise result
                return result
        raise OrgQueryError(f"unscripted query: {soql}", "UNSCRIPTED")

    async def describe(self, sobject):
        if sobject not in self.describes:
            raise OrgQueryError("no such object", "NOT_FOUND")
        return {"fields": [{"name": n} for n in self.describes[sobject]]}

    async def current_user_id(self):
        return "005XX"


def healthy():
    return {
        "FROM Organization": [{"Name": "Dev", "IsSandbox": True, "OrganizationType": "Developer Edition",
                               "DefaultAccountAccess": "Read", "DefaultContactAccess": "Read"}],
        "FROM Network LIMIT 1": [],
        "FROM UserLicense": [{"Name": "Customer Community", "Status": "Active", "TotalLicenses": 5, "UsedLicenses": 1}],
        "WHERE Name = ": [],
        "FROM Domain": [{"Domain": "x.my.site.com", "DomainType": "DNS"}],
        "FROM User": [{"Username": "a@b.c", "IsActive": True, "Profile": {"Name": "System Administrator", "PermissionsManageUsers": True}}],
        "FROM ServiceChannel": [{"DeveloperName": "sfdc_livemessage"}],
        "IsPersonAccount": OrgQueryError("No such column", "INVALID_FIELD"),
    }


def run(answers, spec=None, describes=None):
    org = FakeOrg(answers, describes)
    return asyncio.run(run_preflight(org, spec or PortalSpec("Customer Portal"))), org


def by_id(report):
    return {c.id: c for c in report.checks}


def test_healthy_org_can_proceed():
    report, _ = run(healthy())
    assert report.can_proceed
    assert not [c for c in report.checks if c.status in (FAIL, ERROR)]
    assert by_id(report)["miaw"].status == SKIP


def test_every_check_is_read_only():
    _, org = run(healthy())
    assert org.queries and all(q.lstrip().upper().startswith("SELECT") for q in org.queries)


def test_digital_experiences_disabled_blocks():
    a = healthy()
    a["FROM Network LIMIT 1"] = OrgQueryError("sObject type 'Network' is not supported", "INVALID_TYPE")
    report, _ = run(a)
    c = by_id(report)["digital_experiences"]
    assert c.status == FAIL and c.severity == BLOCKING and not report.can_proceed


def test_licenses_all_used_blocks():
    a = healthy()
    a["FROM UserLicense"] = [{"Name": "Customer Community", "Status": "Active", "TotalLicenses": 5, "UsedLicenses": 5}]
    c = by_id(run(a)[0])["licenses"]
    assert c.status == FAIL and "fully used" in c.message


def test_licenses_missing_blocks():
    a = healthy()
    a["FROM UserLicense"] = []
    assert by_id(run(a)[0])["licenses"].status == FAIL


def test_plus_license_model_queries_plus_names():
    a = healthy()
    a["FROM UserLicense"] = [{"Name": "Customer Community Plus", "Status": "Active", "TotalLicenses": 2, "UsedLicenses": 0}]
    report, org = run(a, PortalSpec("P", license="customer_community_plus"))
    assert by_id(report)["licenses"].status == PASS
    assert any("Customer Community Plus" in q for q in org.queries)


def test_site_name_taken_blocks():
    a = healthy()
    a["WHERE Name = "] = [{"Name": "Customer Portal", "UrlPathPrefix": "customerportal", "Status": "Live"}]
    c = by_id(run(a)[0])["site_name"]
    assert c.status == FAIL and "customerportal" in c.message


def test_soql_value_is_escaped():
    a = healthy()
    _, org = run(a, PortalSpec("O'Brien Portal"))
    assert any("O\\'Brien Portal" in q for q in org.queries)


def test_no_domain_blocks():
    a = healthy()
    a["FROM Domain"] = []
    assert by_id(run(a)[0])["domain"].status == FAIL


def test_inactive_admin_blocks():
    a = healthy()
    a["FROM User"] = [{"Username": "a@b.c", "IsActive": False, "Profile": {"PermissionsManageUsers": True}}]
    c = by_id(run(a)[0])["admin_user"]
    assert c.status == FAIL and "inactive" in c.message


def test_admin_without_manage_users_blocks():
    a = healthy()
    a["FROM User"] = [{"Username": "a@b.c", "IsActive": True, "Profile": {"PermissionsManageUsers": False}}]
    assert by_id(run(a)[0])["admin_user"].status == FAIL


def test_named_admin_not_found():
    a = healthy()
    a["FROM User"] = []
    assert by_id(run(a, PortalSpec("P", admin_username="nobody@x.y"))[0])["admin_user"].status == FAIL


def test_miaw_requested_but_missing_blocks():
    a = healthy()
    a["FROM ServiceChannel"] = []
    c = by_id(run(a, PortalSpec("P", want_miaw=True))[0])["miaw"]
    assert c.status == FAIL


def test_miaw_requested_and_present_passes():
    assert by_id(run(healthy(), PortalSpec("P", want_miaw=True))[0])["miaw"].status == PASS


def test_person_accounts_enabled_is_only_a_warning():
    a = healthy()
    a["IsPersonAccount"] = []
    report, _ = run(a)
    c = by_id(report)["person_accounts"]
    assert c.status == FAIL and c.severity == WARNING and report.can_proceed


def test_private_sharing_warns_for_customer_community():
    a = healthy()
    a["FROM Organization"] = [{"Name": "Dev", "IsSandbox": True, "OrganizationType": "Developer Edition",
                               "DefaultAccountAccess": "None", "DefaultContactAccess": "ControlledByParent"}]
    report, _ = run(a)
    c = by_id(report)["sharing_model"]
    assert c.status == FAIL and c.severity == WARNING and "Sharing Set" in c.fix_hint


def test_production_org_warns():
    a = healthy()
    a["FROM Organization"] = [{"Name": "Acme", "IsSandbox": False, "OrganizationType": "Enterprise Edition",
                               "DefaultAccountAccess": "Read", "DefaultContactAccess": "Read"}]
    c = by_id(run(a)[0])["org"]
    assert c.status == FAIL and c.severity == WARNING


def test_required_fields_missing_then_planned():
    spec = PortalSpec("P", required_fields=["Contact.Phone", "Contact.Loyalty__c"])
    describes = {"Contact": ["Id", "Phone"]}
    assert by_id(run(healthy(), spec, describes)[0])["fields"].status == FAIL
    spec.planned_fields = ["Contact.Loyalty__c"]
    assert by_id(run(healthy(), spec, describes)[0])["fields"].status == PASS


def test_refused_query_is_an_error_not_a_pass_and_does_not_hide_other_checks():
    a = healthy()
    a["FROM Domain"] = OrgQueryError("insufficient access", "INSUFFICIENT_ACCESS")
    report, _ = run(a)
    ids = by_id(report)
    assert ids["domain"].status == ERROR
    assert not report.can_proceed
    assert ids["licenses"].status == PASS and ids["admin_user"].status == PASS


def test_format_report_lists_fix_for_failures():
    a = healthy()
    a["FROM Domain"] = []
    text = format_report(run(a)[0])
    assert "BLOCKED" in text and "fix:" in text
