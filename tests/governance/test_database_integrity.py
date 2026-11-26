"""
LinkPilot AI — Phase 1B M4-C1: Dedicated Governance & Storage Integrity Test Suite.
Tracks cryptographic file lineage across project milestones, preserving historical
evidence while auditing active database integrity, foreign keys, and row counts.
"""

import hashlib
import os
import sqlite3
import pytest


# Cryptographic lineage across project milestones
DATABASE_LINEAGE = {
    "pre_m4_baseline": "a0cceefe1e874205a4af40296b77a287c18380990a4f77f90fc9168b96a04711",
    "post_m4_incident_baseline": "0ecedfc4cf0abe213dcc0a4f96585b3d2a722b066ced1c1c6877ef20452644c9",
    "post_m5_baseline": "469910fd5b3d2821090f3de1f7ec15bace02dae1e9130beb6cb6e062074ce234",
}


def test_historical_database_checksum_governance():
    """GOV-01: Verify physical database hash matches authorized baseline and passes integrity checks."""
    db_path = "./data/linkpilot.db"
    backup_path = "./data/backups/linkpilot_pre_m5.db"
    assert os.path.exists(db_path), f"Production database {db_path} does not exist."
    assert os.path.exists(backup_path), f"Pre-M5 database backup {backup_path} does not exist."

    # Verify pre-M5 backup matches post-M4 baseline
    backup_sha256 = hashlib.sha256()
    with open(backup_path, "rb") as bf:
        while chunk := bf.read(65536):
            backup_sha256.update(chunk)
    assert backup_sha256.hexdigest() == DATABASE_LINEAGE["post_m4_incident_baseline"], (
        f"Pre-M5 backup hash {backup_sha256.hexdigest()} does not match post-M4 baseline "
        f"{DATABASE_LINEAGE['post_m4_incident_baseline']}."
    )

    # Compute current active SHA-256
    sha256 = hashlib.sha256()
    with open(db_path, "rb") as f:
        while chunk := f.read(65536):
            sha256.update(chunk)
    current_hash = sha256.hexdigest()

    # Verify current hash matches authorized post-M5 baseline
    assert current_hash == DATABASE_LINEAGE["post_m5_baseline"], (
        f"Active database hash {current_hash} does not match authorized baseline "
        f"{DATABASE_LINEAGE['post_m5_baseline']}."
    )

    # Read-only verification of structural and referential integrity
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    cur = conn.cursor()

    # 1. B-tree structural integrity
    cur.execute("PRAGMA integrity_check;")
    integrity = cur.fetchall()
    assert integrity == [("ok",)], f"Integrity check failed: {integrity}"

    # 2. Foreign key referential integrity
    cur.execute("PRAGMA foreign_key_check;")
    fk_violations = cur.fetchall()
    assert fk_violations == [], f"Foreign key violations found: {fk_violations}"

    # 3. Table count verification: exactly 18 tables present
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
    tables = [r[0] for r in cur.fetchall()]
    assert len(tables) == 18, f"Expected 18 tables, got {len(tables)}: {tables}"
    assert "domain_authority_scores" in tables

    # 4. Critical table row count verification
    expected_counts = {
        "pages": 170,
        "page_links": 165,
        "link_occurrences": 1675,
        "domains": 30,
        "domain_registration_data": 2,
        "historical_snapshots": 2,
        "page_authority_scores": 0,
        "domain_authority_scores": 0,
    }
    for table_name, expected_count in expected_counts.items():
        cur.execute(f"SELECT COUNT(*) FROM \"{table_name}\";")
        actual_count = cur.fetchone()[0]
        assert actual_count == expected_count, (
            f"Table {table_name} expected {expected_count} rows, found {actual_count}."
        )

    conn.close()
