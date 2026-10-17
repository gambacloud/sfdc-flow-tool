"""
Portal builder, stage 5: Messaging for In-App and Web (MIAW) as metadata.

Everything here deploys through the Metadata API - no clicking in Setup. The
chain, in dependency order, was established against a real scratch org
(features EmbeddedServiceMessaging + Einstein1AIPlatform):

    QueueRoutingConfig -> Queue (MessagingSession) -> MessagingChannel
        -> EmbeddedServiceConfig (the "Embedded Service Deployment", tied to the
           site's ChatterNetworkPicasso site name, e.g. Member_Hub1)

Enum spellings the org insisted on: QueueRoutingConfig.routingModel
LEAST_ACTIVE (not LeastActive), MessagingChannel needs messagingChannelType
EmbeddedMessaging and sessionHandlerType Queue, and EmbeddedServiceConfig.site
must be the Picasso site (Member_Hub1), not the CustomSite (Member_Hub).

The chat button on the pages is the LWR component
`experience_messaging:embeddedMessaging`; its public props (read from the
runtime) include deploymentName, scrtUrl and siteEndpoint. Both URLs turn out
to be derivable - the deployment's Code Snippet in Setup shows
`init('<orgId>', '<deployment>', '<the site's own URL>', {scrt2URL: 'https://<mydomain>.my.salesforce-scrt.com'})`
- so `siteEndpoint` is just the site URL and `scrt_url()` rewrites the My
Domain host. Not proven: that the page's bootstrap script
(`<site>/assets/js/bootstrap.min.js`) is actually served - it answered 404 in
the scratch org for 15+ minutes after the deployment was published and the
channel activated. Without those values the page tries
`undefined/assets/js/bootstrap.min.js`.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

METADATA_NS = "http://soap.sforce.com/2006/04/metadata"


def _slug(label: str) -> str:
    """Salesforce developer name: letters/digits/underscores, starts with a letter."""
    out = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_")
    if not out or not out[0].isalpha():
        out = "X" + out
    return out


@dataclass
class MiawSpec:
    channel_label: str = "Portal Chat"
    queue_label: str = "Portal Support"
    allow_guests: bool = True
    authenticated: bool = False  # authMode Auth (logged-in users) vs UnAuth

    @property
    def channel(self) -> str:
        return _slug(self.channel_label)

    @property
    def queue(self) -> str:
        return _slug(self.queue_label)

    @property
    def routing(self) -> str:
        return _slug(self.queue_label + " Routing")

    @property
    def deployment(self) -> str:
        return _slug(self.channel_label + " Deployment")


def queue_routing_config_xml(spec: MiawSpec) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<QueueRoutingConfig xmlns="{METADATA_NS}">
    <label>{escape(spec.queue_label)} Routing</label>
    <routingModel>LEAST_ACTIVE</routingModel>
    <routingPriority>1</routingPriority>
    <pushTimeout>60</pushTimeout>
    <capacityWeight>1</capacityWeight>
</QueueRoutingConfig>
"""


def queue_xml(spec: MiawSpec) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Queue xmlns="{METADATA_NS}">
    <doesSendEmailToMembers>false</doesSendEmailToMembers>
    <name>{escape(spec.queue_label)}</name>
    <queueRoutingConfig>{spec.routing}</queueRoutingConfig>
    <queueSobject>
        <sobjectType>MessagingSession</sobjectType>
    </queueSobject>
</Queue>
"""


def messaging_channel_xml(spec: MiawSpec) -> str:
    auth = "Auth" if spec.authenticated else "UnAuth"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<MessagingChannel xmlns="{METADATA_NS}">
    <description>{escape(spec.channel_label)}</description>
    <embeddedConfig>
        <authMode>{auth}</authMode>
        <isAttachmentUploadEnabled>false</isAttachmentUploadEnabled>
        <isSaveTranscriptEnabled>false</isSaveTranscriptEnabled>
    </embeddedConfig>
    <masterLabel>{escape(spec.channel_label)}</masterLabel>
    <messagingChannelType>EmbeddedMessaging</messagingChannelType>
    <sessionHandlerQueue>{spec.queue}</sessionHandlerQueue>
    <sessionHandlerType>Queue</sessionHandlerType>
</MessagingChannel>
"""


def embedded_service_config_xml(spec: MiawSpec, picasso_site: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<EmbeddedServiceConfig xmlns="{METADATA_NS}">
    <areGuestUsersAllowed>{str(spec.allow_guests).lower()}</areGuestUsersAllowed>
    <deploymentFeature>EmbeddedMessaging</deploymentFeature>
    <deploymentType>Web</deploymentType>
    <embeddedServiceMessagingChannel>
        <isEnabled>true</isEnabled>
        <messagingChannel>{spec.channel}</messagingChannel>
        <shouldShowDeliveryReceipts>true</shouldShowDeliveryReceipts>
        <shouldShowEmojiSelection>true</shouldShowEmojiSelection>
        <shouldShowReadReceipts>true</shouldShowReadReceipts>
        <shouldShowTypingIndicators>true</shouldShowTypingIndicators>
        <shouldStartNewLineOnEnter>false</shouldStartNewLineOnEnter>
    </embeddedServiceMessagingChannel>
    <isEnabled>true</isEnabled>
    <masterLabel>{escape(spec.channel_label)} Deployment</masterLabel>
    <site>{escape(picasso_site)}</site>
</EmbeddedServiceConfig>
"""


def miaw_files(spec: MiawSpec, picasso_site: str) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    """(zip path -> content, package.xml types) for one deploy."""
    files = {
        f"queueRoutingConfigs/{spec.routing}.queueRoutingConfig": queue_routing_config_xml(spec),
        f"queues/{spec.queue}.queue": queue_xml(spec),
        f"messagingChannels/{spec.channel}.messagingChannel": messaging_channel_xml(spec),
        f"EmbeddedServiceConfig/{spec.deployment}.EmbeddedServiceConfig": embedded_service_config_xml(spec, picasso_site),
    }
    types = {
        "QueueRoutingConfig": [spec.routing],
        "Queue": [spec.queue],
        "MessagingChannel": [spec.channel],
        "EmbeddedServiceConfig": [spec.deployment],
    }
    return files, types


def scrt_url(instance_url: str) -> str:
    """The messaging service host for an org: its My Domain host with
    `salesforce.com` swapped for `salesforce-scrt.com` (seen on a scratch org:
    drive-app-3557-dev-ed.scratch.my.salesforce.com ->
    drive-app-3557-dev-ed.scratch.my.salesforce-scrt.com)."""
    from urllib.parse import urlsplit

    host = urlsplit(instance_url if "://" in instance_url else f"https://{instance_url}").hostname or ""
    if not host.endswith("salesforce.com"):
        raise ValueError(f"not a Salesforce My Domain host: {host!r}")
    return "https://" + host[: -len("salesforce.com")] + "salesforce-scrt.com"


def chat_widget_component(
    spec: MiawSpec, scrt_url: str, site_endpoint: str, new_id: Callable[[], str] = lambda: str(uuid.uuid4())
) -> Dict[str, Any]:
    """The page component, once the deployment's Code Snippet values are known
    (Setup > Embedded Service Deployments > the deployment > Code Snippet)."""
    return {
        "attributes": {
            "deploymentName": spec.deployment,
            "scrtUrl": scrt_url,
            "siteEndpoint": site_endpoint,
        },
        "definition": "experience_messaging:embeddedMessaging",
        "id": new_id(),
        "type": "component",
    }
