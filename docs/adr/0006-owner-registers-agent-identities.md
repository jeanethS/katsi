# The Workspace Owner registers agent identities

An MCP client cannot grant itself authority by declaring a name. The Workspace Owner registers durable Agent Identities and assigns revocable, workspace-scoped Capability Grants; application and model names remain descriptive metadata only. YOLO Mode is likewise enabled for a specific identity and workspace rather than globally. This adds an explicit onboarding step in exchange for preventing any local client from silently acquiring coordination or mutation authority.
