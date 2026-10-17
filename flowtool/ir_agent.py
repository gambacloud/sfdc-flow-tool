"""
Agentforce Service Agent IR.

The model describes the agent - who it is, its topics and the actions each topic
may take - and `xmlgen_agent` writes the Agent Script from that. The script is
never written by the model, which is what rules out the two mistakes the org
only reports at publish time (an action the topic refers to but never defined,
and a missing agent user), and what lets every action be checked against the
Apex class, Flow or Prompt Template it calls.

What was established against a real scratch org (API 68, Einstein1AIPlatform):
- an action's target is `apex://<Class>`, `flow://<Flow>` or `prompt://<Template>`
  and a deploy rejects one that does not exist ("couldn't find the flow, prompt,
  or apex class");
- the agent is deployed as an AiAuthoringBundle (a draft); publishing and
  activating it is done in Agentforce Builder.
"""

from __future__ import annotations

import re
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .ir import _check_api_name

TargetType = Literal["apex", "flow", "prompt"]
ParamType = Literal["string", "number", "boolean"]

# Names the generated script already uses for its own built-in topics.
RESERVED_TOPICS = {"agent_router", "escalation", "off_topic", "ambiguous_question"}

_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,38}[a-z0-9]$")
_PARAM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,60}$")


def _ident(value: str, what: str) -> str:
    if not _IDENT_RE.match(value) or "__" in value:
        raise ValueError(
            f"{what} {value!r} must be lowercase letters, digits and single underscores, "
            "starting with a letter (for example 'create_support_case')"
        )
    return value


class Param(BaseModel):
    name: str = Field(description="Exactly the name of the Apex @InvocableVariable, Flow variable or template input")
    description: str = Field(description="What it is, so the agent can fill it from the conversation")
    type: ParamType = "string"

    @field_validator("name")
    @classmethod
    def valid_name(cls, v: str) -> str:
        if not _PARAM_RE.match(v):
            raise ValueError(f"parameter name {v!r} must be letters, digits and underscores, starting with a letter")
        return v

    @field_validator("description")
    @classmethod
    def described(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("every parameter needs a description - the agent fills it from the conversation")
        return v


class Action(BaseModel):
    api_name: str = Field(description="Lowercase snake_case, unique within its topic")
    description: str = Field(description="When and why to use it - the agent chooses actions by this text")
    target_type: TargetType
    target: str = Field(description="The api name of the Apex class, Flow or Prompt Template this calls")
    inputs: List[Param] = Field(default_factory=list)
    outputs: List[Param] = Field(default_factory=list)

    @field_validator("api_name")
    @classmethod
    def valid_api_name(cls, v: str) -> str:
        return _ident(v, "action api_name")

    @field_validator("target")
    @classmethod
    def valid_target(cls, v: str) -> str:
        return _check_api_name(v, "action target", max_length=80)

    @field_validator("description")
    @classmethod
    def described(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("every action needs a description - the agent chooses actions by it")
        return v

    @model_validator(mode="after")
    def distinct_params(self) -> "Action":
        for label, params in (("input", self.inputs), ("output", self.outputs)):
            names = [p.name for p in params]
            dupes = sorted({n for n in names if names.count(n) > 1})
            if dupes:
                raise ValueError(f"action {self.api_name!r}: duplicate {label} name(s): {', '.join(dupes)}")
        return self


class Topic(BaseModel):
    api_name: str = Field(description="Lowercase snake_case, unique in the agent")
    label: str
    description: str = Field(description="What this topic is for - the agent routes to a topic by this text")
    instructions: str = Field(description="How the agent should behave inside this topic, in plain sentences")
    actions: List[Action] = Field(default_factory=list)

    @field_validator("api_name")
    @classmethod
    def valid_api_name(cls, v: str) -> str:
        v = _ident(v, "topic api_name")
        if v in RESERVED_TOPICS:
            raise ValueError(f"topic api_name {v!r} is reserved for a built-in topic - choose another")
        return v

    @field_validator("description", "instructions")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("topic description and instructions must not be empty")
        return v

    @model_validator(mode="after")
    def unique_actions(self) -> "Topic":
        names = [a.api_name for a in self.actions]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"topic {self.api_name!r}: duplicate action name(s): {', '.join(dupes)}")
        return self


class Agent(BaseModel):
    api_name: str
    label: str
    description: str
    # Not deployed - see CustomField.reasoning in ir_object.py.
    reasoning: Optional[str] = None
    instructions: str = Field(
        default="You are a helpful assistant for this company's customers.",
        description="Who the agent is and how it should behave overall",
    )
    welcome_message: str = "Hi, I'm an AI assistant. How can I help you?"
    error_message: str = "Sorry, it looks like something has gone wrong."
    topics: List[Topic] = Field(min_length=1)
    agent_user: Optional[str] = Field(
        default=None,
        description="Leave out - the username of the agent user is supplied when the plan is built.",
    )

    @field_validator("api_name")
    @classmethod
    def valid_api_name(cls, v: str) -> str:
        return _check_api_name(v, "agent api_name", max_length=60)

    @model_validator(mode="after")
    def unique_topics(self) -> "Agent":
        names = [t.api_name for t in self.topics]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate topic name(s): {', '.join(dupes)}")
        return self

    def action_targets(self) -> List[Action]:
        return [a for t in self.topics for a in t.actions]
