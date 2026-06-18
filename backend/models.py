"""ORM models. Results are stored on the Lead as JSON so the schema stays simple
while we iterate on which enrichment variables exist."""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, JSON, Float
from sqlalchemy.orm import relationship
from db import Base


class LeadList(Base):
    __tablename__ = "lead_lists"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    variable_set = Column(String, default="ascendly_lean")
    created_at = Column(DateTime, default=datetime.utcnow)
    leads = relationship("Lead", back_populates="list", cascade="all, delete-orphan")


class Lead(Base):
    __tablename__ = "leads"
    id = Column(Integer, primary_key=True)
    list_id = Column(Integer, ForeignKey("lead_lists.id"))
    first_name = Column(String, default="")
    last_name = Column(String, default="")
    title = Column(String, default="")
    company = Column(String, default="")
    website = Column(String, default="")
    email = Column(String, default="")
    data = Column(JSON, default=dict)        # raw imported row
    result = Column(JSON, default=dict)      # {ICPReview, ICP_reason, _title_gate, <vars>, _status}
    verify = Column(JSON, default=dict)      # raw Reoon result
    email_status = Column(String, default="")  # reoon status label (safe/catch_all/invalid/...)
    status = Column(String, default="pending")  # pending | running | done | skipped
    list = relationship("LeadList", back_populates="leads")


class CustomVariable(Base):
    __tablename__ = "custom_variables"
    id = Column(Integer, primary_key=True)
    variable_set = Column(String, index=True)
    name = Column(String)     # slug / engine output key
    label = Column(String)    # display name the user typed
    spec = Column(JSON, default=dict)   # full engine-format variable spec
    created_at = Column(DateTime, default=datetime.utcnow)


class Job(Base):
    __tablename__ = "jobs"
    id = Column(Integer, primary_key=True)
    list_id = Column(Integer, ForeignKey("lead_lists.id"))
    kind = Column(String, default="enrich")     # enrich | verify
    summary = Column(JSON, default=dict)         # flexible counters (used by verify)
    status = Column(String, default="queued")   # queued | running | done | cancelled | error
    total = Column(Integer, default=0)
    done = Column(Integer, default=0)
    icp = Column(Integer, default=0)
    nonicp = Column(Integer, default=0)
    rejected = Column(Integer, default=0)
    cost = Column(Float, default=0.0)
    variable_set = Column(String, default="ascendly_lean")
    enrichments = Column(JSON, default=list)    # selected output variables
    created_at = Column(DateTime, default=datetime.utcnow)
