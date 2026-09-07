# Repository routing

## Unqualified GitHub references

In governed Dish work, unqualified PR/issue numbers mean `marcogallotta/ai-tools`; do not ask Marco
for owner/repo while the repository is configured.

## GitHub integration identity

For every Dish GitHub authority read or write, ChatGPT must use only the installed GitHub connector:
tool integration identifier `plugin_connector_*` (its display name may be `GitHub`). The GitHub
MCP/ASDK app, identified by `plugin_asdk_app_*`, is prohibited. Do not use
`api_tool.list_resources(["Github"])` or a generic `Github` MCP namespace for Dish repository
authority. A provider/display name or GitHub capability is not proof of connector identity.

Before the first repository-authority operation in a session, distinguish the integration by type,
not by provider/display name. If connector identity is unavailable or unclear, or the connector
cannot perform the required operation, stop and tell Marco that GitHub connector access is
unavailable. Do not substitute the MCP/ASDK app, web search, a local clone, cached files, memory, or
another GitHub integration.
