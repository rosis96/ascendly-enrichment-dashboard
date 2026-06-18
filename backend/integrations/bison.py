"""Bison — STUB. Fill in once API docs + custom-field schema are provided.

Planned (same interface as Instantly so the dashboard stays provider-agnostic):
    create_campaign(cfg) -> campaign_id
    upload_leads(campaign_id, leads, field_map)
"""
import os

BISON_API_KEY = os.getenv("BISON_API_KEY", "")


def create_campaign(cfg):
    raise NotImplementedError("Bison integration pending API docs.")


def upload_leads(campaign_id, leads, field_map):
    raise NotImplementedError("Bison integration pending API docs.")
