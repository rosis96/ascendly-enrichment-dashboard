"""Provider integrations. All stubbed until API docs land.

Planned interface (provider-agnostic):
  verify(emails)            -> Reoon email verification (valid/invalid/catch-all)
  create_campaign(cfg)      -> Instantly OR Bison
  upload_leads(campaign, leads, field_map)
Each provider implements these so the dashboard stays provider-agnostic.
"""
