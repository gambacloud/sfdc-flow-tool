"""portal_miaw generators, and the pipeline's Messaging step."""

import asyncio
import io
import re
import zipfile

import pytest

from flowtool.portal_miaw import (
    MiawSpec, chat_widget_component, embedded_service_config_xml, messaging_channel_xml,
    miaw_files, queue_routing_config_xml, queue_xml,
)
from flowtool.portal_site import SiteSpec, build_site
from tests.test_portal_site import FakeMeta, FakeWriter, answers


def test_names_are_developer_name_safe():
    spec = MiawSpec(channel_label="Portal Chat!", queue_label="3rd line / Support")
    assert spec.channel == "Portal_Chat"
    assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", spec.queue)
    assert spec.routing.endswith("_Routing") and spec.deployment == "Portal_Chat_Deployment"


def test_org_enum_spellings():
    spec = MiawSpec()
    assert "<routingModel>LEAST_ACTIVE</routingModel>" in queue_routing_config_xml(spec)
    ch = messaging_channel_xml(spec)
    assert "<messagingChannelType>EmbeddedMessaging</messagingChannelType>" in ch
    assert "<sessionHandlerType>Queue</sessionHandlerType>" in ch


def test_queue_accepts_messaging_sessions_and_points_at_the_routing_config():
    xml = queue_xml(MiawSpec())
    assert "<sobjectType>MessagingSession</sobjectType>" in xml
    assert "<queueRoutingConfig>Portal_Support_Routing</queueRoutingConfig>" in xml


def test_auth_mode_follows_the_spec():
    assert "<authMode>UnAuth</authMode>" in messaging_channel_xml(MiawSpec())
    assert "<authMode>Auth</authMode>" in messaging_channel_xml(MiawSpec(authenticated=True))


def test_deployment_is_tied_to_the_picasso_site_not_the_custom_site():
    xml = embedded_service_config_xml(MiawSpec(), "Member_Hub1")
    assert "<site>Member_Hub1</site>" in xml
    assert "<messagingChannel>Portal_Chat</messagingChannel>" in xml


def test_labels_are_xml_escaped():
    assert "&lt;b&gt;" in messaging_channel_xml(MiawSpec(channel_label="<b>x</b>"))


def test_miaw_files_cover_every_type_in_the_package():
    files, types = miaw_files(MiawSpec(), "Member_Hub1")
    assert set(types) == {"QueueRoutingConfig", "Queue", "MessagingChannel", "EmbeddedServiceConfig"}
    assert len(files) == 4


def test_chat_widget_component_carries_the_props_the_runtime_reads():
    comp = chat_widget_component(MiawSpec(), "https://x.my.salesforce-scrt.com", "https://x.my.site.com/ESWPortal", lambda: "id1")
    assert comp["definition"] == "experience_messaging:embeddedMessaging"
    assert comp["attributes"] == {
        "deploymentName": "Portal_Chat_Deployment",
        "scrtUrl": "https://x.my.salesforce-scrt.com",
        "siteEndpoint": "https://x.my.site.com/ESWPortal",
    }


# --- pipeline ---------------------------------------------------------------

def messaging_answers():
    a = {"SELECT IsActive FROM MessagingChannel": [{"IsActive": True}]}
    a.update(answers())
    a["FROM MessagingChannel WHERE DeveloperName"] = [{"Id": "0Mj1"}]
    a["SELECT DeveloperName FROM EmbeddedServiceConfig"] = []      # preflight: name is free
    a["SELECT Id FROM EmbeddedServiceConfig"] = [{"Id": "04I1"}]   # validation: it now exists
    return a


def run(org, meta, messaging, pages=False):
    from flowtool.portal_pages import PortalContent
    spec = SiteSpec("Customer Portal", poll_seconds=0, messaging=messaging, want_miaw=messaging is not None,
                    content=PortalContent(site_name="Customer Portal", headline="Hi") if pages else None)
    return asyncio.run(build_site(org, meta, spec, apply=True))


def test_pipeline_deploys_the_chain_and_asks_for_a_publish_if_the_chat_does_not_appear():
    meta = FakeMeta()
    result = run(FakeWriter(messaging_answers()), meta, MiawSpec())
    assert result.ok, [(s.name, s.status, s.detail) for s in result.steps]
    z = zipfile.ZipFile(io.BytesIO(meta.deployed[-1][0]))
    assert "EmbeddedServiceConfig/Portal_Chat_Deployment.EmbeddedServiceConfig" in z.namelist()
    assert b"<site>Customer_Portal1</site>" in z.read("EmbeddedServiceConfig/Portal_Chat_Deployment.EmbeddedServiceConfig")
    assert any("Publish" in m and "10 minutes" in m for m in result.manual_steps)
    assert {c.id for c in result.validation} >= {"messaging_channel", "messaging_active", "messaging_deployment"}


def test_no_messaging_means_no_messaging_step():
    result = run(FakeWriter(answers()), FakeMeta(), None)
    assert "Messaging" not in [s.name for s in result.steps]


def test_plan_lists_the_messaging_step_before_publish():
    spec = SiteSpec("Customer Portal", messaging=MiawSpec(), want_miaw=True)
    result = asyncio.run(build_site(FakeWriter(messaging_answers()), FakeMeta(), spec, apply=False))
    names = [s.name for s in result.steps]
    assert names.index("Messaging") == names.index("Publish") - 1


def test_failed_messaging_deploy_stops_before_publish():
    class Meta(FakeMeta):
        async def deploy_and_wait(self, zip_bytes, check_only=True):
            from flowtool.sfdc import DeployResult
            self.deployed.append((zip_bytes, check_only))
            ok = len(self.deployed) == 1
            return DeployResult(id="0Af", status="Succeeded" if ok else "Failed", success=ok)

    org = FakeWriter(messaging_answers())
    result = run(org, Meta(), MiawSpec())
    assert not result.ok
    assert not any(p[0].endswith("/publish") for p in org.posts)


def test_preflight_blocks_a_deployment_name_already_bound_to_another_site():
    a = messaging_answers()
    a["SELECT DeveloperName FROM EmbeddedServiceConfig"] = [{"DeveloperName": "Portal_Chat_Deployment"}]
    org = FakeWriter(a)
    spec = SiteSpec("Customer Portal", poll_seconds=0, messaging=MiawSpec(), want_miaw=True)
    result = asyncio.run(build_site(org, FakeMeta(), spec, apply=True))
    bad = [c for c in result.preflight.checks if c.id == "miaw"][0]
    assert bad.status == "fail" and "cannot be moved" in bad.message
    assert not result.steps and not org.posts


def test_reuse_allows_the_existing_deployment():
    a = messaging_answers()
    a["SELECT DeveloperName FROM EmbeddedServiceConfig"] = [{"DeveloperName": "Portal_Chat_Deployment"}]
    a["WHERE Name = 'Customer Portal' OR UrlPathPrefix"] = [{"Name": "Customer Portal", "UrlPathPrefix": "p", "Status": "Live"}]
    spec = SiteSpec("Customer Portal", poll_seconds=0, messaging=MiawSpec(), want_miaw=True, allow_existing_site=True)
    org = FakeWriter(a, sites=[{"id": "0DB1", "name": "Customer Portal", "status": "Live", "siteUrl": "https://x.my.site.com/portal"}])
    result = asyncio.run(build_site(org, FakeMeta(), spec, apply=True))
    assert [c for c in result.preflight.checks if c.id == "miaw"][0].status == "pass"


# --- the chat button -----------------------------------------------------------

def test_scrt_url_rewrites_the_my_domain_host():
    from flowtool.portal_miaw import scrt_url
    assert scrt_url("https://drive-app-3557-dev-ed.scratch.my.salesforce.com") ==         "https://drive-app-3557-dev-ed.scratch.my.salesforce-scrt.com"
    assert scrt_url("https://acme.my.salesforce.com/") == "https://acme.my.salesforce-scrt.com"
    assert scrt_url("acme--dev.sandbox.my.salesforce.com") == "https://acme--dev.sandbox.my.salesforce-scrt.com"
    with pytest.raises(ValueError):
        scrt_url("https://example.com")


def test_the_pipeline_activates_the_channel_it_made():
    org = FakeWriter(messaging_answers())
    run(org, FakeMeta(), MiawSpec())
    assert org.updated == [("MessagingChannel", "0Mj1", {"IsActive": True})]


def test_both_pages_carry_the_chat_button_with_derived_values():
    import json as _json
    meta = FakeMeta()
    run(FakeWriter(messaging_answers()), meta, MiawSpec(), pages=True)
    z = zipfile.ZipFile(io.BytesIO(meta.deployed[1][0]))
    root = "digitalExperiences/site/Customer_Portal1/sfdc_cms__view"
    for view in ("home", "my_account"):
        text = z.read(f"{root}/{view}/content.json").decode()
        assert "experience_messaging:embeddedMessaging" in text, view
        data = _json.loads(text)

        def components(node):
            if isinstance(node, dict):
                if node.get("definition") == "experience_messaging:embeddedMessaging":
                    yield node
                for v in node.values():
                    yield from components(v)
            elif isinstance(node, list):
                for v in node:
                    yield from components(v)

        (chat,) = list(components(data))
        assert chat["attributes"] == {
            "deploymentName": "Portal_Chat_Deployment",
            "scrtUrl": "https://x.my.salesforce-scrt.com",
            "siteEndpoint": "https://x.my.site.com/portal",
        }


def test_no_chat_button_without_messaging():
    meta = FakeMeta()
    run(FakeWriter(answers()), meta, None, pages=True)
    z = zipfile.ZipFile(io.BytesIO(meta.deployed[1][0]))
    assert b"experience_messaging" not in z.read("digitalExperiences/site/Customer_Portal1/sfdc_cms__view/home/content.json")
