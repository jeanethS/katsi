# Keep the reconciliation control loop local

Katsi observes workspaces, compiles declared intent, plans Change Sets, evaluates policy, and verifies outcomes using local components because personal workspace authority depends on privacy and a control boundary the owner can inspect. A cloud model may advise only when the person explicitly invokes it with disclosed context; it cannot authorize or execute a Change Set. This accepts the capability limits of local models in exchange for preventing remote inference from becoming part of Katsi's authority path.
