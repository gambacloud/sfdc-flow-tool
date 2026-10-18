"""Agent IR -> the files of an AiAuthoringBundle (an Agent Script plus its meta file)."""

from __future__ import annotations

from typing import Dict

from .ir_agent import Agent
from .portal_agent import (
    AgentScriptSpec, Param, ScriptAction, ScriptTopic, authoring_bundle_files,
)

PLACEHOLDER_USER = "NEW AGENT USER"


class MissingAgentUser(ValueError):
    """Raised when a bundle is asked for without a real agent user. The org
    accepts the placeholder at deploy time and only rejects it when the agent
    is published, so it is refused here instead."""


def to_script_spec(agent: Agent, agent_user: str = "") -> AgentScriptSpec:
    return AgentScriptSpec(
        api_name=agent.api_name,
        label=agent.label,
        description=agent.description,
        agent_user=agent_user,
        agent_type=agent.agent_type,
        instructions=agent.instructions,
        welcome=agent.welcome_message,
        error=agent.error_message,
        topics=[
            ScriptTopic(
                api_name=t.api_name,
                label=t.label,
                description=t.description,
                instructions=t.instructions,
                actions=[
                    ScriptAction(
                        api_name=a.api_name,
                        description=a.description,
                        target=a.target,
                        target_type=a.target_type,
                        inputs=[Param(p.name, p.description, p.type) for p in a.inputs],
                        outputs=[Param(p.name, p.description, p.type) for p in a.outputs],
                    )
                    for a in t.actions
                ],
            )
            for t in agent.topics
        ],
    )


def generate_agent_files(agent: Agent, agent_user: str | None = None) -> Dict[str, str]:
    """Zip-path -> content for `AiAuthoringBundle` member `agent.api_name`."""
    user = (agent_user or agent.agent_user or "").strip()
    if agent.agent_type == "employee":
        user = ""  # an employee agent runs as whoever calls it
    elif not user or user == PLACEHOLDER_USER:
        raise MissingAgentUser(
            f"Agent {agent.api_name!r} needs an agent user: the username of a user with the "
            "'Einstein Agent User' profile. Set it in the plan options and build again."
        )
    return authoring_bundle_files(to_script_spec(agent, user))
