# Split portable project state from private operational state

Katsi separates owner-approved intent, invariants, verified decisions, and relevant project metadata that may travel with a workspace from machine-local embeddings, caches, identities, capabilities, leases, detailed activity, and recovery data. This makes long-lived projects portable without committing private agent activity or machine-specific authority into the project, at the cost of maintaining an explicit boundary between the two stores.
