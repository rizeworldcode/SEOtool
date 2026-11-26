"""
LinkPilot AI — Phase 1B M4-C1: CLI Database Isolation & Lifecycle Test Suite.
Verifies that CLI commands operate with complete physical database safety,
eliminating silent schema initialization side-effects.
"""

import hashlib
import os
import sqlite3
import pytest
from typer.testing import CliRunner
from sqlalchemy import create_engine, inspect

from app.commands.cli import app as cli_app
from app.config.settings import settings
from app.database.models import Base
from app.authority.scorer import AuthorityScoringResult
from tests.governance.test_database_integrity import DATABASE_LINEAGE
from unittest.mock import patch


runner = CliRunner()


def compute_file_sha256(filepath: str) -> str:
    """Compute SHA-256 of a file on disk."""
    sha = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            sha.update(chunk)
    return sha.hexdigest()


def test_cli_help_does_not_mutate_db():
    """CLI-01: Verify that 'dataengine --help' mutates zero bytes in the physical database."""
    prod_db = "./data/linkpilot.db"
    assert os.path.exists(prod_db)

    stat_before = os.stat(prod_db)
    sha_before = compute_file_sha256(prod_db)

    result = runner.invoke(cli_app, ["--help"])
    assert result.exit_code == 0

    stat_after = os.stat(prod_db)
    sha_after = compute_file_sha256(prod_db)

    assert stat_before.st_size == stat_after.st_size
    assert stat_before.st_mtime == stat_after.st_mtime
    assert sha_before == sha_after


def test_cli_read_only_command_no_db_mutation():
    """CLI-02: Verify that read-only CLI commands do not alter or create schema."""
    prod_db = "./data/linkpilot.db"
    assert os.path.exists(prod_db)

    sha_before = compute_file_sha256(prod_db)

    res1 = runner.invoke(cli_app, ["authority", "--help"])
    assert res1.exit_code == 0

    res2 = runner.invoke(cli_app, ["db", "--help"])
    assert res2.exit_code == 0

    sha_after = compute_file_sha256(prod_db)
    assert sha_before == sha_after


def test_cli_db_init_explicit_creation(tmp_path, monkeypatch):
    """CLI-03: Verify that 'dataengine db init' explicitly creates database schemas."""
    temp_db_path = str(tmp_path / "explicit_init.db")
    temp_db_url = f"sqlite:///{temp_db_path}"

    monkeypatch.setattr(settings, "DATABASE_URL", temp_db_url)
    from app.database import connection
    monkeypatch.setattr(connection, "engine", create_engine(temp_db_url))

    # Before init: file does not exist or has 0 tables
    result = runner.invoke(cli_app, ["db", "init"])
    assert result.exit_code == 0
    assert "Database schema initialized successfully." in result.output

    # Verify tables created
    insp = inspect(create_engine(temp_db_url))
    tables = insp.get_table_names()
    assert "page_authority_scores" in tables
    assert "pages" in tables
    assert "page_links" in tables


def test_authority_compute_missing_schema_error(tmp_path, monkeypatch):
    """CLI-04: Verify that 'authority compute' aborts with exit code 1 when schema is missing."""
    temp_db_path = str(tmp_path / "empty_uninit.db")
    temp_db_url = f"sqlite:///{temp_db_path}"

    # Create empty sqlite db file with no tables
    sqlite3.connect(temp_db_path).close()

    monkeypatch.setattr(settings, "DATABASE_URL", temp_db_url)
    from app.database import connection
    monkeypatch.setattr(connection, "engine", create_engine(temp_db_url))

    result = runner.invoke(cli_app, ["authority", "compute", "--all"])
    assert result.exit_code == 1
    assert "Error: Database schema is missing or uninitialized." in result.output
    assert "dataengine db init" in result.output


def test_authority_compute_existing_schema_success(tmp_path, monkeypatch):
    """CLI-05: Verify that 'authority compute' proceeds successfully with pre-initialized schema."""
    temp_db_path = str(tmp_path / "initialized.db")
    temp_db_url = f"sqlite:///{temp_db_path}"

    eng = create_engine(temp_db_url)
    Base.metadata.create_all(bind=eng)

    monkeypatch.setattr(settings, "DATABASE_URL", temp_db_url)
    from app.database import connection
    monkeypatch.setattr(connection, "engine", eng)

    mock_res = AuthorityScoringResult(
        target_domain=None,
        total_pages_scored=0,
        total_components=0,
        converged_components=0,
        non_converged_components=0,
        skipped_singleton_components=0,
        max_lp_pa_score=0.0,
        mean_lp_pa_score=0.0,
        total_wall_time_seconds=0.005,
        computed_at=None,
        status="COMPLETED",
    )

    with patch("app.authority.scorer.PageRankOrchestrator.run", return_value=mock_res), \
         patch("app.database.repositories.CrawlRepository.get_top_authority_scores", return_value=[]):
        result = runner.invoke(cli_app, ["authority", "compute", "--all"])

    assert result.exit_code == 0
    assert "Page Authority Scoring Summary" in result.output


def test_cli_invalid_arguments_exit_code_2():
    """CLI-06: Verify that missing mandatory arguments exit with code 2."""
    res1 = runner.invoke(cli_app, ["authority", "compute"])
    assert res1.exit_code == 2

    res2 = runner.invoke(cli_app, ["authority", "compute", "--domain", "ex.com", "--all"])
    assert res2.exit_code == 2

    res3 = runner.invoke(cli_app, ["authority", "compute", "--all", "--alpha", "1.5"])
    assert res3.exit_code == 2


def test_cli_physical_db_path_invariance():
    """CLI-07: Verify that executing CLI tests leaves ./data/linkpilot.db bit-for-bit invariant."""
    prod_db = "./data/linkpilot.db"
    assert os.path.exists(prod_db)

    sha_current = compute_file_sha256(prod_db)
    # Target authorized post-M5 baseline hash from governance lineage
    expected_hash = DATABASE_LINEAGE["post_m5_baseline"]
    assert sha_current == expected_hash
