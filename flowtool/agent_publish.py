"""
Publishing and activating a deployed Agentforce employee agent, without the sf CLI.

A deployed AiAuthoringBundle is only a draft: Agentforce Builder shows it, nobody can
talk to it. `sf agent publish authoring-bundle` turns it into a BotDefinition plus a
BotVersion, and `sf agent activate` switches that version on. The server this tool runs
on has no sf CLI, so this module makes the same calls the CLI's @salesforce/agents
library makes (agents/scriptAgent.js, agents/scriptAgentPublisher.js,
agents/productionAgent.js, utils.js useNamedUserJwt):

1. GET <instance>/agentforce/bootstrap/nameduser with the session as a `sid` cookie
   answers a named-user JWT - the token api.salesforce.com accepts.
2. POST api.salesforce.com/einstein/ai-agent/v1.1/authoring/scripts compiles the
   bundle's Agent Script into the agent definition JSON.
3. POST .../authoring/agents (first publish) or .../authoring/agents/<botId>/versions
   (every later one) creates the BotDefinition / a new BotVersion.
4. The bundle is deployed again with <target>Agent.vN</target>, which links the
   draft to the version just created (the CLI does the same).
5. POST /services/data/vXX/connect/bot-versions/<id>/activation {"status": "Active"}.

Then BotVersion is queried to confirm the version really is Active.

Only employee agents: from the networks this was built on, publishing a service agent
gets 404 from api.salesforce.com and a reset connection from test.api.salesforce.com,
so it stays a manual step in Agentforce Builder. Verified for an employee agent against
a real scratch org.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import httpx

from .sfdc import MetadataClient, _normalise, build_deploy_package

SFAP_AUTHORING = "https://api.salesforce.com/einstein/ai-agent/v1.1/authoring"
_SFAP_HEADERS = {"x-client-name": "afdx", "content-type": "application/json"}
EMPLOYEE_AGENT_TYPE = "AgentforceEmployeeAgent"
_TARGET_RE = re.compile(r"\s*<target>[^<]*</target>")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class AgentPublishError(RuntimeError):
    """A step of publish/activate failed; the message says which and what the org said."""


@dataclass
class PublishResult:
    agent: str
    bot_id: str
    version_id: str
    version_number: int
    version_name: str
    status: str  # what BotVersion says after the activation, "Active" when it worked
    first_publish: bool

    def as_dict(self) -> Dict[str, Any]:
        return {
            "agent": self.agent, "bot_id": self.bot_id, "version_id": self.version_id,
            "version_number": self.version_number, "version_name": self.version_name,
            "status": self.status, "first_publish": self.first_publish,
        }


def with_target(meta_xml: str, target: str) -> str:
    """The bundle's meta file pointing at `target` (Agent.vN), replacing any older one."""
    xml = _TARGET_RE.sub("", meta_xml)
    return xml.replace("</AiAuthoringBundle>", f"    <target>{target}</target>\n</AiAuthoringBundle>", 1)


def _detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:400] or f"HTTP {resp.status_code}"
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return "; ".join(str(d.get("message") or d) for d in data)
    if isinstance(data, dict):
        return str(data.get("errorMessage") or data.get("message") or data)[:400]
    return str(data)[:400]


class AgentPublisher:
    def __init__(
        self, instance_url: str, session_id: str, api_version: str = "65.0",
        http: Optional[httpx.AsyncClient] = None, metadata: Optional[MetadataClient] = None,
    ):
        self.instance_url = _normalise(instance_url)
        self.session_id = session_id
        self.api_version = api_version
        self._own_http = http is None
        self.http = http or httpx.AsyncClient(timeout=120.0)
        self._own_metadata = metadata is None
        self.metadata = metadata or MetadataClient(instance_url, session_id, api_version)

    async def aclose(self) -> None:
        if self._own_http:
            await self.http.aclose()
        if self._own_metadata:
            await self.metadata.aclose()

    async def __aenter__(self) -> "AgentPublisher":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    # ---- org REST ----

    def _rest(self, path: str) -> str:
        return f"{self.instance_url}/services/data/v{self.api_version}{path}"

    async def query(self, soql: str) -> list:
        resp = await self.http.get(
            self._rest("/query"), params={"q": soql},
            headers={"Authorization": f"Bearer {self.session_id}"},
        )
        if resp.status_code != 200:
            raise AgentPublishError(f"Query failed ({resp.status_code}): {_detail(resp)}")
        return resp.json().get("records", [])

    async def named_user_token(self) -> str:
        resp = await self.http.get(
            f"{self.instance_url}/agentforce/bootstrap/nameduser",
            headers={"Content-Type": "application/json", "Cookie": f"sid={self.session_id}"},
        )
        token = None
        if resp.status_code == 200:
            try:
                token = resp.json().get("access_token")
            except ValueError:
                token = None
        if not isinstance(token, str) or token.count(".") != 2:
            raise AgentPublishError(
                f"The org did not hand out an Agentforce API token ({resp.status_code}). "
                "Check that Agentforce is enabled and the user may use it."
            )
        return token

    # ---- the bundle ----

    async def read_bundle(self, name: str) -> Tuple[str, str]:
        """(Agent Script, bundle-meta.xml) of the deployed bundle, as the org has it."""
        zip_bytes = await self.metadata.wait_for_retrieve(
            await self.metadata.start_retrieve_types({"AiAuthoringBundle": [name]})
        )
        base = f"aiAuthoringBundles/{name}/{name}"
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            names = set(archive.namelist())
            if f"{base}.agent" not in names or f"{base}.bundle-meta.xml" not in names:
                raise AgentPublishError(f"Agent {name} is not deployed in this org (no AiAuthoringBundle {name}).")
            return (archive.read(f"{base}.agent").decode("utf-8"),
                    archive.read(f"{base}.bundle-meta.xml").decode("utf-8"))

    async def compile(self, jwt: str, script: str) -> Dict[str, Any]:
        resp = await self.http.post(
            f"{SFAP_AUTHORING}/scripts",
            headers={**_SFAP_HEADERS, "Authorization": f"Bearer {jwt}"},
            json={"assets": [{"type": "AFScript", "name": "AFScript", "content": script}],
                  "afScriptVersion": "2.0.0"},
        )
        if resp.status_code != 200:
            raise AgentPublishError(f"Compiling the agent script failed ({resp.status_code}): {_detail(resp)}")
        data = resp.json()
        if data.get("status") != "success" or not data.get("compiledArtifact"):
            errors = "; ".join(
                str(e.get("description") or e.get("message") or e) for e in data.get("errors") or []
            )
            raise AgentPublishError(f"The agent script does not compile: {errors or data.get('status')}")
        return data["compiledArtifact"]

    # ---- publish + activate ----

    async def publish(self, name: str, activate: bool = True) -> PublishResult:
        if not _SAFE_NAME_RE.match(name):
            raise AgentPublishError(f"{name!r} is not an agent API name.")
        script, meta = await self.read_bundle(name)
        jwt = await self.named_user_token()
        definition = await self.compile(jwt, script)
        agent_type = (definition.get("globalConfiguration") or {}).get("agentType")
        if agent_type != EMPLOYEE_AGENT_TYPE:
            raise AgentPublishError(
                f"{name} is a {agent_type or 'service'} agent. Only employee agents can be published from "
                "here; publish this one in Agentforce Builder."
            )

        bots = await self.query(f"SELECT Id FROM BotDefinition WHERE DeveloperName = '{name}'")
        bot_id = bots[0]["Id"] if bots else None
        url = f"{SFAP_AUTHORING}/agents/{bot_id}/versions" if bot_id else f"{SFAP_AUTHORING}/agents"
        resp = await self.http.post(
            url, headers={**_SFAP_HEADERS, "Authorization": f"Bearer {jwt}"},
            json={"agentDefinition": definition, "instanceConfig": {"endpoint": self.instance_url}},
        )
        if resp.status_code not in (200, 201):
            raise AgentPublishError(f"Publishing failed ({resp.status_code}): {_detail(resp)}")
        published = resp.json()
        if not published.get("botId") or not published.get("botVersionId"):
            raise AgentPublishError(f"Publishing failed: {published.get('errorMessage') or published}")
        version_id = published["botVersionId"]

        versions = await self.query(
            f"SELECT Id, DeveloperName, VersionNumber, Status FROM BotVersion WHERE Id = '{version_id}'"
        )
        if not versions:
            raise AgentPublishError(f"Published, but BotVersion {version_id} cannot be found.")
        version = versions[0]

        # Link the draft to the new version, the way the CLI does after publishing.
        files = {f"aiAuthoringBundles/{name}/{name}.agent": script,
                 f"aiAuthoringBundles/{name}/{name}.bundle-meta.xml": with_target(meta, f"{name}.{version['DeveloperName']}")}
        result = await self.metadata.deploy_and_wait(
            build_deploy_package(files, {"AiAuthoringBundle": [name]}, self.api_version), check_only=False,
        )
        if not result.success:
            detail = "; ".join(str(f) for f in result.failures) or result.error_message or result.status
            raise AgentPublishError(f"Published version {version['VersionNumber']}, but redeploying the bundle failed: {detail}")

        status = version.get("Status") or "Inactive"
        if activate:
            status = await self.activate(version_id)
        return PublishResult(
            agent=name, bot_id=published["botId"], version_id=version_id,
            version_number=int(version.get("VersionNumber") or 0), version_name=version["DeveloperName"],
            status=status, first_publish=bot_id is None,
        )

    async def activate(self, version_id: str) -> str:
        """Switch one BotVersion on and return the Status the org reports afterwards."""
        resp = await self.http.post(
            self._rest(f"/connect/bot-versions/{version_id}/activation"),
            headers={"Authorization": f"Bearer {self.session_id}", "Content-Type": "application/json"},
            json={"status": "Active"},
        )
        if resp.status_code not in (200, 201):
            raise AgentPublishError(f"Activating failed ({resp.status_code}): {_detail(resp)}")
        body = resp.json()
        if body.get("success") is False:
            raise AgentPublishError(f"Activating failed: {body.get('messages') or body}")
        rows = await self.query(f"SELECT Status FROM BotVersion WHERE Id = '{version_id}'")
        status = rows[0]["Status"] if rows else "Unknown"
        if status != "Active":
            raise AgentPublishError(f"Activation was accepted, but the version is still {status}.")
        return status


async def publish_employee_agent(
    instance_url: str, session_id: str, name: str, api_version: str = "65.0", activate: bool = True,
) -> PublishResult:
    async with AgentPublisher(instance_url, session_id, api_version) as publisher:
        return await publisher.publish(name, activate=activate)
