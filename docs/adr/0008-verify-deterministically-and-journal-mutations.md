# Verify deterministically and journal governed mutations

A governed action is verified only when configured deterministic local checks and Executable Invariants pass or the Workspace Owner explicitly verifies it; agent reports and model confidence cannot establish success. Before mutation, Katsi records affected hashes and recoverable preimages in an append-only Action Journal, and failed postconditions trigger rollback. When no applicable verifier exists, the result remains Applied Unverified. This adds storage and execution overhead in exchange for honest outcomes and recoverability.
