"""Batch prediction service — runs prediction pipeline as subprocess.

In-memory job tracking with subprocess.Popen. Results persist in silver.suggested_answers;
job metadata is ephemeral (lost on server restart).
"""

import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from uuid import uuid4

from nctqai.db import BRONZE_SCHEMA, SILVER_SCHEMA, run_sql

logger = logging.getLogger(__name__)

# Project root — 4 levels up from this file (services/mc/batches.py -> src/nctqai/services/mc/)
_PROJECT_ROOT = str(Path(__file__).resolve().parents[4])
_SRC_DIR = str(Path(__file__).resolve().parents[3])


class BatchStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


@dataclass
class BatchJob:
    job_id: str
    district_id: int
    district_name: str
    ay_id: int
    status: BatchStatus = BatchStatus.pending
    started_at: float = 0.0
    finished_at: float = 0.0
    completed: int = 0
    total: int = 0
    error: str | None = None
    log_lines: list[str] = field(default_factory=list)

    @property
    def elapsed_seconds(self) -> int:
        if self.started_at == 0:
            return 0
        end = self.finished_at if self.finished_at else time.time()
        return int(end - self.started_at)

    @property
    def progress_pct(self) -> int:
        if self.total == 0:
            return 0
        return min(int(self.completed / self.total * 100), 100)


_PROGRESS_RE = re.compile(r"\[(\d+)/(\d+)\]")
_MAX_LOG_LINES = 200


class BatchManager:
    def __init__(self):
        self._jobs: dict[str, BatchJob] = {}
        self._lock = threading.Lock()

    def start_batch(self, district_id: int, district_name: str, ay_id: int) -> BatchJob:
        job_id = str(uuid4())[:8]
        job = BatchJob(
            job_id=job_id,
            district_id=district_id,
            district_name=district_name,
            ay_id=ay_id,
        )
        with self._lock:
            self._jobs[job_id] = job

        t = threading.Thread(target=self._run_subprocess, args=(job,), daemon=True)
        t.start()
        return job

    def _run_subprocess(self, job: BatchJob):
        job.status = BatchStatus.running
        job.started_at = time.time()

        env = {**os.environ, "PYTHONPATH": _SRC_DIR, "PREDICTOR_DRY_RUN": "false"}

        cmd = [
            sys.executable, "-m", "prediction_pipeline.run",
            "--district", str(job.district_id),
            "--ay", str(job.ay_id),
            "--priority", "High",
            "--no-dry-run",
        ]

        logger.info("Starting batch %s: %s", job.job_id, " ".join(cmd))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=_PROJECT_ROOT,
                env=env,
            )

            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue

                with self._lock:
                    if len(job.log_lines) < _MAX_LOG_LINES:
                        job.log_lines.append(line)

                m = _PROGRESS_RE.search(line)
                if m:
                    job.completed = int(m.group(1))
                    job.total = int(m.group(2))

            proc.wait()
            job.finished_at = time.time()

            if proc.returncode == 0:
                job.status = BatchStatus.completed
                logger.info("Batch %s completed in %ss", job.job_id, job.elapsed_seconds)
            else:
                job.status = BatchStatus.failed
                job.error = f"Process exited with code {proc.returncode}"
                logger.error("Batch %s failed: %s", job.job_id, job.error)

        except Exception as e:
            job.finished_at = time.time()
            job.status = BatchStatus.failed
            job.error = str(e)
            logger.exception("Batch %s crashed: %s", job.job_id, e)

    def get_all_jobs(self) -> list[BatchJob]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.started_at or 0, reverse=True)

    def get_job(self, job_id: str) -> BatchJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def has_running_jobs(self) -> bool:
        with self._lock:
            return any(j.status in (BatchStatus.pending, BatchStatus.running) for j in self._jobs.values())

    def is_district_running(self, district_id: int, ay_id: int) -> bool:
        with self._lock:
            return any(
                j.district_id == district_id and j.ay_id == ay_id
                and j.status in (BatchStatus.pending, BatchStatus.running)
                for j in self._jobs.values()
            )


# Module-level singleton
_manager: BatchManager | None = None
_manager_lock = threading.Lock()


def get_batch_manager() -> BatchManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = BatchManager()
        return _manager


def get_available_districts() -> list[dict]:
    """All in-universe districts ordered by name."""
    sql = f"""
        SELECT d.district_id, d.district_name, d.district_state as state
        FROM {BRONZE_SCHEMA}.district d
        JOIN {SILVER_SCHEMA}.districts sd ON sd.district_id = d.district_id
        WHERE sd.is_in_universe = true
        ORDER BY d.district_name
    """
    return run_sql(sql)


def get_doc_count(district_id: int, ay_id: int) -> int:
    """Count of district_documents available for a district × ay_id."""
    sql = f"""
        SELECT COUNT(*) as cnt
        FROM {SILVER_SCHEMA}.district_documents
        WHERE district_id = %s AND effective_ay_ids @> ARRAY[%s]
    """
    rows = run_sql(sql, (district_id, ay_id))
    return rows[0]["cnt"] if rows else 0


def get_high_priority_count() -> int:
    """Count of active high-priority questions."""
    sql = f"""
        SELECT COUNT(*) as cnt
        FROM {SILVER_SCHEMA}.questions
        WHERE guidance_is_active = true AND q_priority = 'High'
    """
    rows = run_sql(sql)
    return rows[0]["cnt"] if rows else 0
