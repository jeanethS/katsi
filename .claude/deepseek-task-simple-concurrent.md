# Task: Concurrent Change Tests

Create a pytest test file `tests/test_concurrent_changes.py` with tests for:

1. Agent B proposal, Agent C modifies same file → proposal blocked
2. Agent B proposal, Agent C modifies different file → proposal valid
3. Exact invalidation evidence returned for conflicts
4. Independent proposals can proceed in parallel

Focus on realistic concurrent work scenarios. Keep it practical.