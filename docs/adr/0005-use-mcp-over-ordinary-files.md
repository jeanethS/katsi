# Use MCP over ordinary files

Katsi exposes its Living Model and coordination operations as an MCP-first workspace service while files remain ordinary files accessed through existing tools. The protocol grows from existing retrieval primitives into opening a workspace, obtaining a Workspace Brief, acquiring work, publishing Claims, proposing and verifying Change Sets, and releasing work. This reaches agents across clients without requiring a mounted virtual filesystem, accepting that Katsi must observe and reconcile changes made outside its protocol.
