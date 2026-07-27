"""Trimmed OpenAPI document for the Custom GPT Action surface."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

ACTION_COMMANDS = (
    "create",
    "sections",
    "read",
    "inspect",
    "start",
    "prepare",
    "approve",
    "reject",
    "submit",
)

_ARGUMENT_SCHEMAS: dict[str, dict[str, Any]] = {
    "create": {
        "required": ["agent", "title"],
        "properties": {"agent": {"type": "string", "enum": ["claude", "gpt", "codex"]}, "title": {"type": "string"}},
    },
    "sections": {
        "required": ["agent"],
        "properties": {"agent": {"type": "string", "enum": ["claude", "gpt", "codex"]}},
    },
    "read": {
        "required": ["task_gid", "agent"],
        "properties": {"task_gid": {"type": "string"}, "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]}},
    },
    "inspect": {
        "required": ["submission_id", "agent"],
        "properties": {"submission_id": {"type": "string"}, "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]}},
    },
    "start": {
        "required": ["task_gid", "agent", "kind"],
        "properties": {
            "task_gid": {"type": "string"},
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
            "kind": {"type": "string", "enum": ["planning", "initial", "change", "verification"]},
            "run_id": {"type": "string"},
            "independence_attestation": {"type": "string"},
            "change_level": {"type": "string", "enum": ["small", "large"]},
            "change_reason": {"type": "string"},
        },
    },
    "prepare": {
        "required": ["submission_id", "agent", "model", "file_text"],
        "properties": {
            "submission_id": {"type": "string"},
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
            "model": {"type": "string"},
            "file_text": {"type": "string"},
            "material_classification": {"type": "string", "enum": ["material", "non-material"]},
            "exemption_revision": {"type": "string"},
            "dish_name": {"type": "string"},
            "recognition": {"type": "string"},
            "roles": {"type": "array", "items": {"type": "string"}},
            "no_role_tags": {"type": "boolean"},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "no_blockers": {"type": "boolean"},
        },
    },
    "approve": {
        "required": ["submission_id", "agent", "model", "correction", "reviewed_identity", "semantic_review_complete", "provenance_complete"],
        "anyOf": [
            {"required": ["run_id"]},
            {"required": ["independence_attestation"]},
        ],
        "properties": {
            "submission_id": {"type": "string"},
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
            "model": {"type": "string"},
            "correction": {"type": "string", "enum": ["none", "small"]},
            "file_text": {"type": "string"},
            "reviewed_identity": {"type": "string"},
            "semantic_review_complete": {"type": "boolean"},
            "provenance_complete": {"type": "boolean"},
            "run_id": {"type": "string"},
            "independence_attestation": {"type": "string"},
        },
    },
    "reject": {
        "required": ["submission_id", "agent", "reason", "route"],
        "anyOf": [
            {"required": ["run_id"]},
            {"required": ["independence_attestation"]},
        ],
        "properties": {
            "submission_id": {"type": "string"},
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
            "model": {"type": "string"},
            "reason": {"type": "string"},
            "route": {"type": "string", "enum": ["large", "evidence", "human-review"]},
            "file_text": {"type": "string"},
            "resume_status": {"type": "string", "enum": ["pending-research", "pending-verification"]},
            "run_id": {"type": "string"},
            "independence_attestation": {"type": "string"},
        },
    },
    "submit": {
        "required": ["submission_id"],
        "properties": {"submission_id": {"type": "string"}},
    },
}


def action_openapi(*, server_url: str = "https://dish.example.invalid") -> dict[str, Any]:
    envelope = {
        "type": "object",
        "required": ["ok", "command", "code", "retryable", "allowed_actions", "data", "errors"],
        "properties": {
            "ok": {"type": "boolean"},
            "command": {"type": "string"},
            "code": {"type": "string"},
            "task_gid": {"type": ["string", "null"]},
            "submission_id": {"type": ["string", "null"]},
            "state": {"type": ["string", "null"]},
            "retryable": {"type": "boolean"},
            "allowed_actions": {"type": "array", "items": {"type": "string"}},
            "data": {"type": "object", "additionalProperties": True},
            "errors": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        },
    }
    paths: dict[str, Any] = {}
    for command in ACTION_COMMANDS:
        argument_schema = deepcopy(_ARGUMENT_SCHEMAS[command])
        argument_schema.update({"type": "object", "additionalProperties": False})
        paths[f"/v1/action/{command}"] = {
            "post": {
                "operationId": f"dish_{command.replace('-', '_')}",
                "summary": f"Run dish {command}",
                "security": [{"actionBearer": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["client", "arguments"],
                                "additionalProperties": False,
                                "properties": {
                                    "client": {
                                        "type": "object",
                                        "required": ["run_id"],
                                        "additionalProperties": False,
                                        "properties": {"run_id": {"type": "string"}},
                                    },
                                    "arguments": argument_schema,
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Canonical dish workflow result",
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ResultEnvelope"}}},
                    }
                },
            }
        }
    paths["/v1/action/leases/{operation_id}/renew"] = {
        "post": {
            "operationId": "dish_renew_lease",
            "summary": "Renew the current GPT Action operation lease",
            "security": [{"actionBearer": []}],
            "parameters": [{"name": "operation_id", "in": "path", "required": True, "schema": {"type": "string"}}],
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": {
                    "type": "object", "required": ["client"], "additionalProperties": False,
                    "properties": {"client": {
                        "type": "object", "required": ["run_id"], "additionalProperties": False,
                        "properties": {"run_id": {"type": "string"}},
                    }},
                }}},
            },
            "responses": {"200": {"description": "Canonical lease result", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ResultEnvelope"}}}}},
        }
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": "Dish GPT Action", "version": "1.0.0"},
        "servers": [{"url": server_url}],
        "paths": paths,
        "components": {
            "securitySchemes": {"actionBearer": {"type": "http", "scheme": "bearer"}},
            "schemas": {"ResultEnvelope": envelope},
        },
    }
