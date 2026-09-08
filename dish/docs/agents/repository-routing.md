# Repository routing

## Unqualified GitHub references

In governed Dish work, unqualified PR/issue numbers mean `marcogallotta/ai-tools`; do not ask Marco
for owner/repo while the repository is configured.

## GitHub connector routing

For every GitHub read or write, use the GitHub Connector. Do not use any GitHub tool exposed
through `api_tool` or MCP, including an `api_tool` resource named `Github`; that is the GitHub MCP
app, not the GitHub Connector. If the GitHub Connector is not available as a distinct tool, stop
and report the access blocker rather than substituting MCP.
