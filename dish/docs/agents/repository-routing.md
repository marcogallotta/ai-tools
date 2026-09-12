# Repository routing

## Unqualified GitHub references

In governed Dish work, unqualified PR/issue numbers mean `marcogallotta/ai-tools`; do not ask Marco
for owner/repo while the repository is configured.

## GitHub connector routing

The active ChatGPT Project selects exactly one external-tool transport family for the entire chat.
Never mix Connector tools and MCP apps, and never infer integration identity from a generic tool
namespace such as `api_tool`; the selected installed integration is the identity.

- **Recurring repository-role Projects — Connector-only.** The generated Projects under
  [`../chatgpt-projects/`](../chatgpt-projects/README.md) select the installed GitHub Connector in
  `api_tool` and use Connector tools only. Never select or invoke the separate GitHub MCP app or any
  other MCP app. If the Connector and MCP app cannot be distinguished, or the required Connector is
  unavailable, tell Marco and stop rather than substituting an MCP app.
- **General Dish and Cooking Projects — MCP-app-only.** The Projects installed from
  [`../../deploy/mcp-app.md`](../../deploy/mcp-app.md) select the installed GitHub MCP app and Dish
  MCP app and use MCP apps only. Never select or invoke the GitHub Connector or any other Connector.
  If a required MCP app is unavailable or the two GitHub integrations cannot be distinguished, tell
  Marco and stop rather than substituting a Connector.

Claude Code and Codex use their local checkout and native Git; this ChatGPT transport-family choice
does not apply to them.
