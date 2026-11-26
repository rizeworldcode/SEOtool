# LINKPILOT AI — SYSTEM ARCHITECTURE, WORKFLOW & AGENT HANDOVER SPECIFICATION

**Document Version:** 1.0.0-PROD  
**Target Audience:** Antigravity AI Agents & Senior Software Engineers  
**System Name:** LinkPilot AI  
**Repository Working Directory:** `/Volumes/HIKVISION/vikas/seo-automation`  
**Execution Environment:** macOS darwin, Python 3.13.5 (Virtualenv: `./.venv`)  
**Active Project State:** Phase 1A Complete (100%), Phase 1B Milestones 1.1–4 Accepted (149/149 Tests Passing), Milestone 5 (Domain Authority) Fully Specified and Awaiting Implementation Authorization.

---

## 1. MISSION & SYSTEM PURPOSE

**LinkPilot AI** is an enterprise-grade autonomous search engine telemetry, backlink acquisition, graph extraction, and link equity computation engine. 

Unlike third-party SEO wrappers that query external APIs (e.g., Moz, Ahrefs, Semrush), LinkPilot AI performs **mathematically rigorous, zero-dependency PageRank and Authority Scoring directly over its own ingested web graph**.

The system computes two foundational authority metrics:
1. **Page Authority ($LP\text{-}PA \in [0.0, 1.0]$):** Granular, component-scaled link equity of individual URLs derived from stationary PageRank distributions over directed web graphs.
2. **Domain Authority ($LP\text{-}DA \in [0.0, 1.0]$ / Display: $0\text{--}100$):** Multi-component aggregate authority of root domains, combining anchor pillar strength, regularized harmonic top-$k$ depth, and logarithmic breadth-scaled volume.

---

## 2. STRICT OPERATING RESTRICTIONS & AGENT GOVERNANCE RULES

Any AI agent operating in this repository **must strictly observe the following ironclad rules**:

### Rule 1: Production Database Protection
- The production database is located at `./data/linkpilot.db`.
- **NEVER** run destructive database commands (`DROP`, `VACUUM`, unisolated `create_all()`).
- **NEVER** mutate `./data/linkpilot.db` during test runs. All unit and integration tests **must** execute against in-memory SQLite (`sqlite:///:memory:`) or isolated temporary SQLite files.
- The active authorized database hash is:
  ```
  SHA-256: 0ecedfc4cf0abe213dcc0a4f96585b3d2a722b066ced1c1c6877ef20452644c9
  File Size: 819,200 bytes (exactly 200 SQLite pages * 4,096 bytes)
  Table Count: Exactly 17 tables (16 Phase 1A tables + table 17: page_authority_scores)
  Journal Mode: DELETE (zero -wal or -shm files permitted)
  ```
- Any unauthorized change to `./data/linkpilot.db` will break `tests/governance/test_database_integrity.py` and fail the build.

### Rule 2: Strict Implementation Phasing
- **Phase 1: Read-Only Investigation & Specification First.** Always inspect the actual source code and produce an architectural specification and risk assessment before editing files.
- **Phase 2: Explicit Authorization Required.** **NEVER** write implementation code or modify database schemas without explicit user prompt authorization (e.g., "IMPLEMENT MILESTONE X").
- **Phase 3: Zero Regression Tolerance.** The existing **149 / 149 test suite must remain 100% passing at all times**. Never alter historical test assertions or remove tests to make a suite pass.

### Rule 3: Session & Transaction Ownership
- In repository operations, **never** call `session.commit()` or `session.close()` on caller-supplied SQLAlchemy sessions.
- Always isolate internal persistence failures using SQLAlchemy `with session.begin_nested():` (SAVEPOINT). Releasing a SAVEPOINT does **not** commit outer transactions.

---

## 3. ARCHITECTURE & CURRENT PROGRESS OVERVIEW

```
┌────────────────────────────────────────────────────────────────────────┐
│                        PHASE 1A: DATA ACQUISITION                       │
│                           STATUS: 100% ACCEPTED                        │
│                                                                        │
│ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌────────────────┐ │
│ │ Common Crawl │ │  ICANN RDAP  │ │   Wayback    │ │   Bing / GSC   │ │
│ │ WARC Parser  │ │  Registrar   │ │  CDX Archive │ │ API Ingestion  │ │
│ └──────┬───────┘ └──────┬───────┘ └──────┬───────┘ └───────┬────────┘ │
│        └────────────────┼────────────────┼─────────────────┘          │
│                         ▼                                              │
│             SQLite DB: data/linkpilot.db (16 tables)                   │
│             Pages (170), PageLinks (165), Occurrences (1,675)         │
└─────────────────────────────────┬──────────────────────────────────────┘
                                  │
                                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   PHASE 1B: AUTHORITY SCORING ENGINE                   │
│                                                                        │
│ ┌────────────────────────────────────────────────────────────────────┐ │
│ │ M1.1: URL Normalization & Link Eligibility Engine (40/40 Tests)    │ │
│ │ app/authority/normalizer.py, app/authority/eligibility.py          │ │
│ └──────────────────────────────────┬─────────────────────────────────┘ │
│                                    ▼                                   │
│ ┌────────────────────────────────────────────────────────────────────┐ │
│ │ M2: Directed Graph & CSR Sparse Matrix Engine (25/25 Tests)        │ │
│ │ app/authority/graph.py, app/authority/matrix.py                    │ │
│ └──────────────────────────────────┬─────────────────────────────────┘ │
│                                    ▼                                   │
│ ┌────────────────────────────────────────────────────────────────────┐ │
│ │ M3: PageRank Numerical Solver (Aitken Extrapolation) (32/32 Tests) │ │
│ │ app/authority/pagerank.py                                          │ │
│ └──────────────────────────────────┬─────────────────────────────────┘ │
│                                    ▼                                   │
│ ┌────────────────────────────────────────────────────────────────────┐ │
│ │ M4 & M4-C1: Page Authority Scoring & SAVEPOINT Persist (44 Tests)  │ │
│ │ app/authority/scorer.py, app/database/repositories.py              │ │
│ │ CLI Isolation (7 Tests), Database Integrity Governance (1 Test)    │ │
│ │ TOTAL ACCEPTED BASELINE: 149 / 149 TESTS PASSING                   │ │
│ └──────────────────────────────────┬─────────────────────────────────┘ │
│                                    ▼                                   │
│ ┌────────────────────────────────────────────────────────────────────┐ │
│ │ M5: Domain Authority (LP-DA) Engine [NEXT TO IMPLEMENT]            │ │
│ │ Specification Status: READY FOR IMPLEMENTATION AUTHORIZATION       │ │
│ └────────────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 4. MATHEMATICAL FORMULATIONS

### A. LinkPilot Page Authority ($LP\text{-}PA$) — ACCEPTED (M4)
Located in `app/authority/matrix.py` (lines 317–350) and `app/authority/graph.py`:

1. **Power Iteration PageRank:** Computes stationary distribution vector $\mathbf{p}$ over the CSR transition matrix with damping $\alpha = 0.85$ and Aitken $\Delta^2$ acceleration:
   $$\mathbf{p}^{(k+1)} = \alpha \mathbf{p}^{(k)} \mathbf{P} + \left(1 - \alpha + \alpha P_{\text{dangle}}\right) \mathbf{v}$$
2. **Component Significance Scaling:**
   $$\text{CompScale}(C_k) = \frac{\ln(1 + N_k)}{\ln(1 + N_k) + \ln(1 + N_{\text{ref}})}, \quad N_{\text{ref}} = 100.0$$
3. **Directed Equity Extraction:** Teleportation noise is subtracted so unlinked nodes receive zero score:
   $$p_{\text{base}} = \frac{1 - \alpha + \alpha P_{\text{dangle}}}{N}, \quad p_{\text{directed}} = \max\left(0.0, \; \frac{p_i - p_{\text{base}}}{\alpha}\right)$$
4. **Logarithmic Saturation:**
   $$LP\text{-}PA(p_i) = \min\left(1.0, \; \max\left(0.0, \; \frac{\log_{10}(1.0 + p_{\text{directed}} \cdot \text{CompScale}(C_k) \cdot 100.0)}{\log_{10}(51.0)}\right)\right)$$

---

### B. LinkPilot Domain Authority ($LP\text{-}DA$) — SPECIFIED (M5)
Official Model: **`LP-DA Weighted Authority Aggregation Model v1`**

For a target root domain $D$ with $N$ eligible internal scored pages with scores $S_D = \{s_1, \dots, s_N\} \subset [0.0, 1.0]$:

$$LP\text{-}DA(D) = \min\left(1.0, \; \max\left(0.0, \; \alpha \cdot A(D) + \beta \cdot T_k(D) + \gamma \cdot B(D) \cdot M(D)\right)\right)$$
$$LP\text{-}DA_{\text{display}}(D) = \text{round}(LP\text{-}DA(D) \times 100.0, \; 2) \in [0.0, 100.0]$$

#### Component Formulations:
1. **Anchor Authority ($A(D)$):** Maximum page score:
   $$A(D) = \max(S_D) \quad (\text{or } 0.0 \text{ if } N=0)$$
2. **Regularized Harmonic Mean Top-$k$ Depth ($T_k(D)$):** Evaluated over top $m = \min(k, N)$ scores sorted in descending order:
   $$T_k(D) = \begin{cases} 0.0, & \text{if } N = 0 \text{ or } \sum_{i=1}^m s_{(i)} = 0.0 \\ \max\left(0.0, \; \frac{m}{\sum_{i=1}^m \frac{1}{s_{(i)} + \epsilon}} - \epsilon\right), & \text{where } \epsilon = 1.0 \times 10^{-4} \end{cases}$$
3. **Domain Breadth Saturation ($B(D)$):**
   $$B(D) = \min\left(1.0, \; \frac{\log_{10}(1.0 + N)}{\log_{10}(1.0 + N_{\text{ref}})}\right), \quad N_{\text{ref}} = 100.0$$
4. **Volume-Weighted Mean ($M(D)$):**
   $$M(D) = \frac{1}{N} \sum_{i=1}^N s_i \quad (\text{or } 0.0 \text{ if } N=0)$$
5. **Approved Baseline Parameters:**
   $$\alpha = 0.40, \quad \beta = 0.35, \quad \gamma = 0.25 \quad (\alpha + \beta + \gamma = 1.0), \quad k = 5, \quad N_{\text{ref}} = 100.0$$

---

## 5. DATABASE & TRANSACTION ARCHITECTURE

### Current Active Schema (17 Tables in `data/linkpilot.db`)
1. `data_provenance`
2. `data_sources`
3. `crawl_collections`
4. `crawl_runs`
5. `download_jobs`
6. `download_files`
7. `domains` (Canonical domain registry)
8. `pages` (Canonical crawled URLs)
9. `page_links` (Directed link pairs)
10. `link_occurrences` (DOM occurrences & rel attributes)
11. `webpage_metadata`
12. `processing_errors`
13. `domain_registration_data`
14. `historical_snapshots`
15. `source_credentials`
16. `source_sync_history`
17. `page_authority_scores` (Persisted LP-PA scores)

### Milestone 5 Table Specification (Table 18 — Pending Implementation)
```python
class DomainAuthorityScore(Base):
    __tablename__ = "domain_authority_scores"

    id = Column(Integer, primary_key=True, autoincrement=True)
    domain_id = Column(Integer, ForeignKey("domains.id", ondelete="SET NULL"), nullable=True, index=True)
    domain_name = Column(String(255), nullable=False, unique=True)
    lp_da_score = Column(Float, nullable=False)
    lp_da_display = Column(Float, nullable=False)
    anchor_page_pa = Column(Float, nullable=False)
    top_k_harmonic_pa = Column(Float, nullable=False)
    mean_page_pa = Column(Float, nullable=False)
    breadth_score = Column(Float, nullable=False)
    scored_pages_count = Column(Integer, nullable=False)
    configured_top_k = Column(Integer, nullable=False, default=5)
    harmonic_epsilon = Column(Float, nullable=False, default=1e-4)
    reference_page_count = Column(Float, nullable=False, default=100.0)
    alpha = Column(Float, nullable=False, default=0.40)
    beta = Column(Float, nullable=False, default=0.35)
    gamma = Column(Float, nullable=False, default=0.25)
    algorithm_version = Column(String(50), nullable=False, default="LP-DA-v1.0.0")
    computed_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    domain_rel = relationship("Domain", foreign_keys=[domain_id])

    __table_args__ = (
        Index("ix_das_score_desc", "lp_da_score"),
    )
```

### Transaction Lifecycle & SAVEPOINT Contracts
- **Unified Preparation:** When `compute_da=True`, PA and DA records are both calculated and validated in memory (Stages 1–7) *before* acquiring a database transaction.
- **Atomic Persistence:**
  ```python
  if session is not None:
      with session.begin_nested():  # SAVEPOINT
          repo = CrawlRepository(session)
          repo.upsert_page_authority_scores(pa_records, target_domain=target_domain)
          if self.config.compute_da:
              repo.upsert_domain_authority_scores(da_records, target_domain=target_domain)
          session.flush()
  ```
- **Atomicity Guarantee:** If DA persistence fails, the SAVEPOINT rolls back the preceding PA persistence. Unmigrated callers running with `compute_da=False` (default) never touch `domain_authority_scores`.

---

## 6. CLI REFERENCE & ISOLATION GUARDS

LinkPilot uses a Typer-powered CLI entrypoint (`app/commands/cli.py` or `./dataengine`).

### Existing CLI Commands:
```bash
# Verify database schema without side effects
python -m app.commands.cli db check

# Initialize database schema explicitly (User action only)
python -m app.commands.cli db init

# Compute Page Authority across all pages (M4)
python -m app.commands.cli authority compute --scope all --damping 0.85

# Export datasets to Parquet / CSV
python -m app.commands.cli export --format parquet
```

### CLI Isolation Architecture:
- Root CLI callback (`@app.callback()`) is a strict no-op (`pass`). It **never** calls `init_db()`.
- Pre-execution validation gates in `authority compute` verify required tables exist before opening database connections.
- Lookup commands use read-only SQLite URIs: `sqlite:///file:<abs_path>?mode=ro&uri=true`, which physically blocks any write attempt.

---

## 7. HOW TO EXECUTE AND VERIFY THE TEST SUITE

Always set `PYTHONPATH=.` when invoking the test suite:

```bash
# Run the complete accepted 149-test regression baseline
PYTHONPATH=. ./.venv/bin/pytest \
  tests/test_authority_m1_eligibility.py \
  tests/test_authority_m2_graph_matrix.py \
  tests/test_authority_m3_pagerank.py \
  tests/test_authority_m4_scorer.py \
  tests/test_cli_isolation.py \
  tests/governance/test_database_integrity.py
```
*Expected Output:* `149 passed in ~2.0s`.

---

## 8. STEP-BY-STEP IMPLEMENTATION ROADMAP FOR MILESTONE 5

When the user explicitly issues the authorization command (**"IMPLEMENT MILESTONE 5"**), follow this exact 5-step sequence:

### Step 1: Preflight Backup
1. Verify `data/linkpilot.db` checksum matches `0ecedfc4cf0abe213dcc0a4f96585b3d2a722b066ced1c1c6877ef20452644c9`.
2. Create byte-for-byte backup:
   ```bash
   cp data/linkpilot.db data/backups/linkpilot_pre_m5.db
   ```
3. Verify backup checksum matches.

### Step 2: Database Model & Repository Methods
1. Add `DomainAuthorityScore` to `app/database/models.py`.
2. Add `upsert_domain_authority_scores()`, `get_domain_authority_score()`, and `get_top_domain_authority_scores()` to `app/database/repositories.py`.

### Step 3: Domain Scorer Engine & Orchestrator Integration
1. Create `app/authority/domain_scorer.py` containing `compute_domain_authority_scores()` with the regularized harmonic mean formula.
2. In `app/authority/scorer.py`:
   - Add `compute_da: bool = False` to `AuthorityScoringConfig` (default `False` for backward compatibility).
   - In `PageRankOrchestrator.run()`: add Stage 6 DA aggregation, update Stage 7 batch validation, and update Stage 8 dual persistence inside the SAVEPOINT.

### Step 4: CLI Extensions
1. In `app/commands/cli.py`:
   - Add `--with-da / --no-da` option to `authority compute` (default `--no-da`).
   - Add `authority domain [DOMAIN_NAME]` read-only retrieval command.

### Step 5: Test Suite & Post-Migration Governance
1. Create `tests/test_authority_m5_domain.py` containing the 38 in-memory unit/integration tests.
2. Run full test suite: all 187 tests (149 baseline + 38 M5) must pass.
3. Migrate `data/linkpilot.db` via `python -m app.commands.cli db init`.
4. Run `PRAGMA integrity_check` and `PRAGMA foreign_key_check`.
5. Record the new 18-table checksum in `tests/governance/test_database_integrity.py` under `DATABASE_LINEAGE["post_m5_baseline"]`.

---

## 9. USER'S SUBSEQUENT REQUEST: WEB UI DEVELOPMENT

The user previously requested:
> *"can i test it by any ui where i past url and test da pa of that url"*

Once Milestone 5 (Domain Authority) is implemented and verified, proceed immediately to build the **LinkPilot Web UI**:
- **Framework Choice:** Streamlit (fastest, interactive) or FastAPI + Tailwind HTML.
- **Core UI Capabilities:**
  1. **URL Authority Inspector:** Input box to paste any URL.
  2. **Metrics Display:** Shows Page Authority ($PA \in [0.0, 1.0]$ and $0\text{--}100$), PageRank, component ID, and Domain Authority ($DA \in [0.0, 1.0]$ and $0\text{--}100$).
  3. **Link Equity Graph Viewer:** Displays top inbound linking pages, anchor texts, and outbound links contributing to the score.
  4. **On-Demand Scored Ingestion:** If a pasted URL does not yet exist in `data/linkpilot.db`, crawl/ingest its outbound links on the fly, link it into the local graph, and compute its local authority score.

---

## 10. ARTIFACT & SPECIFICATION CROSS-REFERENCE

All prior audits and technical decisions are permanently preserved in the conversation artifact directory (`/Users/vikasjangid/.gemini/antigravity/brain/e1041fef-49e6-4bfc-8213-556f8ae8348d/`):
- `M5_C4_FINAL_SPECIFICATION_AUDIT.md`: Final specification audit establishing the `NOT IMPLEMENTED — DESIGN CONTRACT ONLY` baseline and SAVEPOINT pre-flush semantics.
- `M5_C3_FINAL_CLOSURE_AUDIT.md`: Transaction lifecycle verification and mathematical zero-score calibration.
- `M5_C2_FINAL_RECONCILIATION.md`: Backward-compatibility policy and sensitivity analysis.
- `M4_C1_FINAL_DELIVERY_AND_ACCEPTANCE_REPORT.md`: Acceptance report for Milestone 4 (149/149 tests).
- `tests/governance/test_database_integrity.py`: Cryptographic database lineage audit.

---
*End of Handover Specification.*
