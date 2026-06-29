# Intentionally empty.
# An earlier draft put a standalone execution engine here. Per the decision to
# NOT build a parallel executor, that code was removed. The existing enrichment
# pipeline remains the only engine. New workspace configuration is stored and
# edited via the Workspace Builder (see workspace_config endpoints) and will be
# consumed by the EXISTING pipeline later — not by any new engine.
