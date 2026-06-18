"""Instantly.ai — STUB. Fill in once API docs + custom-field schema are provided.

Planned:
    create_campaign(cfg) -> campaign_id
    upload_leads(campaign_id, leads, field_map)  # maps our variables -> Instantly custom fields
"""
import os

INSTANTLY_API_KEY = os.getenv("INSTANTLY_API_KEY", "")


def create_campaign(cfg):
    raise NotImplementedError("Instantly integration pending API docs.")


def upload_leads(campaign_id, leads, field_map):
    raise NotImplementedError("Instantly integration pending API docs.")
