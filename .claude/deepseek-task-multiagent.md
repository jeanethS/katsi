# Task: Multi-Agent Continuity Fixture (Task 19.2)

## Repository purpose

katsi is a local-first relational file-context engine with comprehensive workspace coordination,
multi-agent support, and Claims/work state persistence. The system supports multiple agents
working collaboratively with durable state.

## The unit of work

Build the Agent A → Agent B continuity fixture using separate MCP client processes and durable
Claims/work state. This is Task 19.2 from the workspace coordination implementation plan.

## Context to understand

Multi-agent continuity involves:

1. **Separate MCP client processes**:
   - Each agent has its own MCP client connection
   - Separate authentication and authorization contexts
   - Independent but coordinated work

2. **Durable Claims/work state**:
   - Claims persist across agent sessions
   - Work items maintain state between agents
   - Workspace briefs provide continuity
   - Agent identities are preserved

3. **Agent A → Agent B handoff**:
   - Agent A publishes Claims and creates work items
   - Agent A completes its portion and exits
   - Agent B resumes using persisted state
   - Agent B sees Agent A's Claims and work
   - Continuity of workspace intent and goals

4. **Workspace state as coordination**:
   - Goals and intent persist
   - Claims provide provenance
   - Work items show progress
   - Briefs provide context

## Required behavior

You are to implement multi-agent continuity fixtures that:

1. **Create separate MCP client processes**:
   - Agent A with distinct identity and authentication
   - Agent B with distinct identity and authentication
   - Proper isolation between processes
   - Independent authorization contexts

2. **Implement durable Claims/work state**:
   - Agent A creates Claims that persist
   - Agent A creates work items that persist
   - Agent B can read Agent A's Claims
   - Agent B can see Agent A's work items
   - State survives Agent A's exit

3. **Test Agent A → Agent B continuity**:
   - Agent A establishes workspace goals/intent
   - Agent A creates Claims and work items
   - Agent A exits gracefully
   - Agent B starts fresh
   - Agent B can resume from Agent A's state
   - Agent B understands workspace context from briefs

4. **Verify Claims and work persistence**:
   - Claims survive process boundaries
   - Work items maintain state
   - Workspace intent is preserved
   - Agent identities persist correctly
   - Attribution is maintained across handoffs

## Contracts to preserve — do not change these

- MCP client interface and authentication
- Claims publishing and retrieval
- Work item creation and querying
- Workspace brief generation
- Agent identity management

## Allowed paths

- `tests/test_multi_agent_continuity.py` (create new test file)
- `tests/fixtures/multi_agent_fixtures.py` (extend if exists)
- You may read but NOT modify:
  - `packages/core/katsi_core/workspace/claims.py` (if exists)
  - `packages/core/katsi_core/workspace/work_items.py` (if exists)
  - `packages/core/katsi_core/workspace/identity.py`

## Exclusions — do not touch

- Any production code in `packages/core/katsi_core/workspace/`
- MCP server implementation
- Claims/work item persistence mechanisms
- Database schemas or migrations

## Acceptance checks

Write these as pytest tests in `tests/test_multi_agent_continuity.py`:

1. **Separate MCP client tests**:
   - Create Agent A with distinct identity
   - Create Agent B with distinct identity
   - Verify proper process isolation
   - Verify independent authorization contexts

2. **Durable state tests**:
   - Agent A creates Claims, verify they persist
   - Agent A creates work items, verify they persist
   - Agent B can read Agent A's Claims
   - Agent B can see Agent A's work items
   - State survives Agent A's process exit

3. **Continuity handoff tests**:
   - Agent A establishes workspace intent
   - Agent A creates Claims and work
   - Agent A exits
   - Agent B starts and can resume
   - Agent B understands workspace context
   - Agent B can continue from Agent A's work

4. **Attribution and provenance tests**:
   - Claims correctly attributed to Agent A
   - Work items show Agent A's provenance
   - Agent B can see Agent A's contributions
   - No attribution confusion between agents

5. **Workspace brief continuity tests**:
   - Briefs survive agent handoffs
   - Agent B gets proper context from briefs
   - Goals and intent persist across agents
   - Work status is accurately reflected

Run: `uv run pytest tests/test_multi_agent_continuity.py -v`

## Response format

Return a complete pytest test file with multi-agent continuity fixtures and tests.
Include test fixtures for creating separate MCP client processes, managing agent lifecycles,
and verifying durable state persistence. Focus on realistic agent handoff scenarios.