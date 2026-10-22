"""Shared dashboard/CLI runtime defaults. Explicit CLI paths still take precedence."""
import os
from pathlib import Path

from .job_store import DEFAULT_DATABASE, JobStore


def open_job_store(database=None, *, artifact_root=None, input_root=None):
    database = database or os.getenv("MARKETLENS_JOBS_DB") or DEFAULT_DATABASE
    artifact_root = artifact_root or os.getenv("MARKETLENS_JOB_ARTIFACT_ROOT") or None
    input_root = input_root or os.getenv("MARKETLENS_INPUT_ROOT") or None
    # An omitted input root uses the database's persisted root (including M3.2 DBs).
    # New explicitly configured workspaces need no manual directory creation.
    if input_root:
        Path(input_root).mkdir(parents=True, exist_ok=True)
    return JobStore(database, artifact_root=artifact_root, input_root=input_root)
