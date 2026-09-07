# Repository routing

## Unqualified GitHub references

In governed Dish work, unqualified PR/issue numbers mean `marcogallotta/ai-tools`; do not ask Marco
for owner/repo while the repository is configured.

## GitHub connector routing

For every Dish GitHub authority read or write, ChatGPT must enter through the installed GitHub
Connector using the connector routing layer (`api_tool`). Actions returned from that route remain
authorized Connector operations even when exposed under a `Github.*` namespace or described as
“GitHub MCP Server.” Those namespace and protocol labels do not identify which installed
integration supplied the action.

Never independently select or invoke the separate GitHub MCP app. Before the first
repository-authority operation in a session, establish provenance from the Connector selection
route rather than inferring identity from a provider name, tool namespace, or MCP wording. If that
Connector-route provenance is unavailable or unclear, or the Connector cannot perform the required
operation, stop and tell Marco that GitHub Connector access is unavailable. Do not substitute the
separate GitHub MCP app, web search, a local clone, cached files, memory, or another integration.
