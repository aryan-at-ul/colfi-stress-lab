"""Append-only SQLite persistence for the version-5 experiment contract."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    EvidenceSnapshotV5,
    ExperimentPreregistration,
    ExposureDescriptor,
    HoldingPosition,
    InstitutionDecisionV5,
    InstitutionProfileV5,
    ModelRegistration,
    canonical_json,
    content_sha256,
)


MIGRATION_VERSIONS = (
    "005_experiment_foundation",
    "006_governance_and_initial_runner",
    "007_feedback_worlds_and_transitions",
    "008_closed_loop_feedback_and_trajectories",
)


class ImmutableConflict(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def migrate_v5_schema(db: sqlite3.Connection) -> None:
    """Create the normalized schema without changing existing v4 records."""

    db.executescript("""
    CREATE TABLE IF NOT EXISTS experiment_schema_migrations (
        version TEXT PRIMARY KEY,
        applied_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS experiment_profile_versions (
        id TEXT PRIMARY KEY,
        institution_id TEXT NOT NULL,
        catalog_version TEXT NOT NULL,
        created_at TEXT NOT NULL,
        content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256) = 64),
        content_json TEXT NOT NULL CHECK(json_valid(content_json)),
        holdings_status TEXT NOT NULL CHECK(holdings_status IN ('unmapped','approved')),
        UNIQUE(catalog_version, institution_id)
    );

    CREATE TABLE IF NOT EXISTS experiment_holdings (
        profile_version_id TEXT NOT NULL,
        position_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        mapping_status TEXT NOT NULL CHECK(mapping_status IN ('unmapped','approved')),
        instrument_id TEXT,
        side TEXT CHECK(side IS NULL OR side IN ('long','short')),
        notional_pct REAL CHECK(notional_pct IS NULL OR (notional_pct > 0 AND notional_pct <= 1000)),
        size_basis TEXT,
        currency TEXT,
        content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
        content_json TEXT NOT NULL CHECK(json_valid(content_json)),
        PRIMARY KEY(profile_version_id, position_id),
        UNIQUE(profile_version_id, ordinal),
        FOREIGN KEY(profile_version_id) REFERENCES experiment_profile_versions(id)
    );

    CREATE TABLE IF NOT EXISTS experiment_model_specs (
        id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        exact_model TEXT NOT NULL,
        created_at TEXT NOT NULL,
        content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256) = 64),
        content_json TEXT NOT NULL CHECK(json_valid(content_json)),
        UNIQUE(provider, exact_model)
    );

    CREATE TABLE IF NOT EXISTS experiment_preregistrations (
        id TEXT PRIMARY KEY,
        schema_version INTEGER NOT NULL CHECK(schema_version = 5),
        created_at TEXT NOT NULL,
        content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256) = 64),
        content_json TEXT NOT NULL CHECK(json_valid(content_json))
    );

    CREATE TABLE IF NOT EXISTS experiment_preregistration_profiles (
        preregistration_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        profile_version_id TEXT NOT NULL,
        PRIMARY KEY(preregistration_id, profile_version_id),
        UNIQUE(preregistration_id, ordinal),
        FOREIGN KEY(preregistration_id) REFERENCES experiment_preregistrations(id),
        FOREIGN KEY(profile_version_id) REFERENCES experiment_profile_versions(id)
    );

    CREATE TABLE IF NOT EXISTS experiment_preregistration_models (
        preregistration_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        model_spec_id TEXT NOT NULL,
        PRIMARY KEY(preregistration_id, model_spec_id),
        UNIQUE(preregistration_id, ordinal),
        FOREIGN KEY(preregistration_id) REFERENCES experiment_preregistrations(id),
        FOREIGN KEY(model_spec_id) REFERENCES experiment_model_specs(id)
    );

    CREATE TABLE IF NOT EXISTS experiment_preregistration_approvals (
        id TEXT PRIMARY KEY,
        preregistration_id TEXT NOT NULL,
        approved_at TEXT NOT NULL,
        actor TEXT NOT NULL,
        approved INTEGER NOT NULL CHECK(approved IN (0,1)),
        note TEXT NOT NULL,
        approved_sha256 TEXT NOT NULL CHECK(length(approved_sha256) = 64),
        FOREIGN KEY(preregistration_id) REFERENCES experiment_preregistrations(id)
    );

    CREATE TABLE IF NOT EXISTS experiment_evidence_snapshots (
        id TEXT PRIMARY KEY,
        event_id TEXT NOT NULL,
        decision_cutoff TEXT NOT NULL,
        created_at TEXT NOT NULL,
        created_by TEXT NOT NULL,
        content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256) = 64),
        content_json TEXT NOT NULL CHECK(json_valid(content_json))
    );

    CREATE TABLE IF NOT EXISTS experiment_evidence_items (
        snapshot_id TEXT NOT NULL,
        evidence_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        evidence_class TEXT NOT NULL CHECK(evidence_class IN ('public_fact','exercise_assumption')),
        instrument_id TEXT,
        observed_at TEXT NOT NULL,
        available_at TEXT NOT NULL,
        raw_sha256 TEXT NOT NULL CHECK(length(raw_sha256) = 64),
        content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
        content_json TEXT NOT NULL CHECK(json_valid(content_json)),
        PRIMARY KEY(snapshot_id, evidence_id),
        UNIQUE(snapshot_id, ordinal),
        FOREIGN KEY(snapshot_id) REFERENCES experiment_evidence_snapshots(id)
    );

    CREATE TABLE IF NOT EXISTS experiment_evidence_approvals (
        id TEXT PRIMARY KEY,
        snapshot_id TEXT NOT NULL,
        approved_at TEXT NOT NULL,
        actor TEXT NOT NULL,
        approved INTEGER NOT NULL CHECK(approved IN (0,1)),
        note TEXT NOT NULL,
        approved_sha256 TEXT NOT NULL CHECK(length(approved_sha256) = 64),
        FOREIGN KEY(snapshot_id) REFERENCES experiment_evidence_snapshots(id)
    );

    CREATE TABLE IF NOT EXISTS experiment_worlds (
        id TEXT PRIMARY KEY,
        preregistration_id TEXT NOT NULL,
        evidence_snapshot_id TEXT NOT NULL,
        assignment_kind TEXT NOT NULL CHECK(assignment_kind IN ('shared','heterogeneous')),
        assignment_key TEXT NOT NULL,
        replicate_index INTEGER NOT NULL CHECK(replicate_index >= 1),
        assignment_sha256 TEXT NOT NULL CHECK(length(assignment_sha256) = 64),
        stage1_action_set_sha256 TEXT NOT NULL CHECK(length(stage1_action_set_sha256) = 64),
        created_at TEXT NOT NULL,
        UNIQUE(preregistration_id, assignment_key, replicate_index),
        FOREIGN KEY(preregistration_id) REFERENCES experiment_preregistrations(id),
        FOREIGN KEY(evidence_snapshot_id) REFERENCES experiment_evidence_snapshots(id)
    );

    CREATE TABLE IF NOT EXISTS experiment_snapshots (
        id TEXT PRIMARY KEY,
        world_id TEXT NOT NULL,
        snapshot_kind TEXT NOT NULL CHECK(snapshot_kind IN ('common','private')),
        institution_profile_version_id TEXT,
        observed_at TEXT NOT NULL,
        content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
        content_json TEXT NOT NULL CHECK(json_valid(content_json)),
        CHECK(
            (snapshot_kind = 'common' AND institution_profile_version_id IS NULL)
            OR
            (snapshot_kind = 'private' AND institution_profile_version_id IS NOT NULL)
        ),
        UNIQUE(world_id, snapshot_kind, institution_profile_version_id),
        FOREIGN KEY(world_id) REFERENCES experiment_worlds(id),
        FOREIGN KEY(institution_profile_version_id) REFERENCES experiment_profile_versions(id)
    );

    CREATE TABLE IF NOT EXISTS experiment_run_cells (
        id TEXT PRIMARY KEY,
        preregistration_id TEXT NOT NULL,
        evidence_snapshot_id TEXT NOT NULL,
        stage TEXT NOT NULL CHECK(stage IN ('initial','feedback')),
        world_id TEXT,
        institution_profile_version_id TEXT NOT NULL,
        model_spec_id TEXT NOT NULL,
        replicate_index INTEGER NOT NULL CHECK(replicate_index >= 1),
        execution_order INTEGER NOT NULL CHECK(execution_order >= 0),
        case_input_sha256 TEXT CHECK(case_input_sha256 IS NULL OR length(case_input_sha256) = 64),
        case_input_json TEXT CHECK(case_input_json IS NULL OR json_valid(case_input_json)),
        created_at TEXT NOT NULL,
        CHECK(
            (stage = 'initial' AND world_id IS NULL)
            OR
            (stage = 'feedback' AND world_id IS NOT NULL)
        ),
        FOREIGN KEY(preregistration_id) REFERENCES experiment_preregistrations(id),
        FOREIGN KEY(evidence_snapshot_id) REFERENCES experiment_evidence_snapshots(id),
        FOREIGN KEY(world_id) REFERENCES experiment_worlds(id),
        FOREIGN KEY(institution_profile_version_id) REFERENCES experiment_profile_versions(id),
        FOREIGN KEY(model_spec_id) REFERENCES experiment_model_specs(id)
    );
    CREATE UNIQUE INDEX IF NOT EXISTS experiment_initial_cell_identity
        ON experiment_run_cells(
            preregistration_id, institution_profile_version_id,
            model_spec_id, replicate_index
        ) WHERE stage = 'initial';
    CREATE UNIQUE INDEX IF NOT EXISTS experiment_initial_execution_order
        ON experiment_run_cells(preregistration_id, execution_order)
        WHERE stage = 'initial';
    CREATE UNIQUE INDEX IF NOT EXISTS experiment_feedback_cell_identity
        ON experiment_run_cells(
            preregistration_id, world_id, institution_profile_version_id
        ) WHERE stage = 'feedback';
    CREATE UNIQUE INDEX IF NOT EXISTS experiment_feedback_execution_order
        ON experiment_run_cells(preregistration_id, execution_order)
        WHERE stage = 'feedback';

    CREATE TABLE IF NOT EXISTS experiment_world_members (
        world_id TEXT NOT NULL,
        institution_profile_version_id TEXT NOT NULL,
        model_spec_id TEXT NOT NULL,
        initial_run_cell_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        PRIMARY KEY(world_id, institution_profile_version_id),
        UNIQUE(world_id, ordinal),
        FOREIGN KEY(world_id) REFERENCES experiment_worlds(id),
        FOREIGN KEY(institution_profile_version_id) REFERENCES experiment_profile_versions(id),
        FOREIGN KEY(model_spec_id) REFERENCES experiment_model_specs(id),
        FOREIGN KEY(initial_run_cell_id) REFERENCES experiment_run_cells(id)
    );

    CREATE TABLE IF NOT EXISTS experiment_run_attempts (
        id TEXT PRIMARY KEY,
        run_cell_id TEXT NOT NULL,
        attempt_index INTEGER NOT NULL CHECK(attempt_index >= 1),
        started_at TEXT NOT NULL,
        completed_at TEXT,
        outcome TEXT NOT NULL CHECK(outcome IN ('accepted','rejected','transport_error')),
        provider_request_sha256 TEXT CHECK(
            provider_request_sha256 IS NULL OR length(provider_request_sha256) = 64
        ),
        raw_response_sha256 TEXT CHECK(
            raw_response_sha256 IS NULL OR length(raw_response_sha256) = 64
        ),
        latency_ms REAL CHECK(latency_ms IS NULL OR latency_ms >= 0),
        finish_reason TEXT,
        usage_json TEXT CHECK(usage_json IS NULL OR json_valid(usage_json)),
        provider_request_json TEXT CHECK(
            provider_request_json IS NULL OR json_valid(provider_request_json)
        ),
        raw_response_text TEXT,
        error TEXT,
        UNIQUE(run_cell_id, attempt_index),
        FOREIGN KEY(run_cell_id) REFERENCES experiment_run_cells(id)
    );

    CREATE TABLE IF NOT EXISTS experiment_decisions (
        id TEXT PRIMARY KEY,
        run_attempt_id TEXT NOT NULL UNIQUE,
        institution_id TEXT NOT NULL,
        stage TEXT NOT NULL CHECK(stage IN ('initial','feedback')),
        stance TEXT NOT NULL CHECK(stance IN ('risk_reduce','risk_add','mixed','hold')),
        content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256) = 64),
        content_json TEXT NOT NULL CHECK(json_valid(content_json)),
        accepted_at TEXT NOT NULL,
        FOREIGN KEY(run_attempt_id) REFERENCES experiment_run_attempts(id)
    );

    CREATE TABLE IF NOT EXISTS experiment_actions (
        decision_id TEXT NOT NULL,
        action_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        action_type TEXT NOT NULL CHECK(action_type IN (
            'sell','hedge','deleverage','withdraw_liquidity','buy_support'
        )),
        target_instrument_id TEXT,
        size_value REAL NOT NULL CHECK(size_value >= 0),
        size_basis TEXT NOT NULL,
        content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
        content_json TEXT NOT NULL CHECK(json_valid(content_json)),
        PRIMARY KEY(decision_id, action_id),
        UNIQUE(decision_id, ordinal),
        FOREIGN KEY(decision_id) REFERENCES experiment_decisions(id)
    );

    CREATE TABLE IF NOT EXISTS experiment_audit_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        actor TEXT NOT NULL,
        event_type TEXT NOT NULL,
        object_type TEXT NOT NULL,
        object_id TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
        previous_event_sha256 TEXT,
        event_sha256 TEXT NOT NULL UNIQUE CHECK(length(event_sha256) = 64),
        CHECK(previous_event_sha256 IS NULL OR length(previous_event_sha256) = 64)
    );

    CREATE TABLE IF NOT EXISTS experiment_metric_results (
        id TEXT PRIMARY KEY,
        preregistration_id TEXT NOT NULL,
        metric_version TEXT NOT NULL,
        accepted_decision_set_sha256 TEXT NOT NULL CHECK(
            length(accepted_decision_set_sha256) = 64
        ),
        permutations INTEGER NOT NULL CHECK(permutations >= 0),
        bootstrap_samples INTEGER NOT NULL CHECK(bootstrap_samples >= 0),
        created_at TEXT NOT NULL,
        actor TEXT NOT NULL,
        content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
        content_json TEXT NOT NULL CHECK(json_valid(content_json)),
        UNIQUE(
            preregistration_id, metric_version, accepted_decision_set_sha256,
            permutations, bootstrap_samples
        ),
        FOREIGN KEY(preregistration_id) REFERENCES experiment_preregistrations(id)
    );

    CREATE TABLE IF NOT EXISTS experiment_world_transitions (
        id TEXT PRIMARY KEY,
        world_id TEXT NOT NULL,
        round_index INTEGER NOT NULL CHECK(round_index IN (1,2)),
        input_stage TEXT NOT NULL CHECK(input_stage IN ('initial','feedback')),
        input_action_set_sha256 TEXT NOT NULL CHECK(length(input_action_set_sha256) = 64),
        transition_policy_sha256 TEXT NOT NULL CHECK(length(transition_policy_sha256) = 64),
        created_at TEXT NOT NULL,
        content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
        content_json TEXT NOT NULL CHECK(json_valid(content_json)),
        UNIQUE(world_id, round_index),
        FOREIGN KEY(world_id) REFERENCES experiment_worlds(id)
    );

    CREATE TABLE IF NOT EXISTS experiment_world_snapshots_v5 (
        id TEXT PRIMARY KEY,
        transition_id TEXT NOT NULL,
        world_id TEXT NOT NULL,
        round_index INTEGER NOT NULL CHECK(round_index IN (1,2)),
        snapshot_kind TEXT NOT NULL CHECK(snapshot_kind IN ('common','private')),
        institution_profile_version_id TEXT,
        content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
        content_json TEXT NOT NULL CHECK(json_valid(content_json)),
        created_at TEXT NOT NULL,
        CHECK(
            (snapshot_kind='common' AND institution_profile_version_id IS NULL)
            OR
            (snapshot_kind='private' AND institution_profile_version_id IS NOT NULL)
        ),
        FOREIGN KEY(transition_id) REFERENCES experiment_world_transitions(id),
        FOREIGN KEY(world_id) REFERENCES experiment_worlds(id),
        FOREIGN KEY(institution_profile_version_id) REFERENCES experiment_profile_versions(id)
    );
    CREATE UNIQUE INDEX IF NOT EXISTS experiment_world_common_snapshot_identity
        ON experiment_world_snapshots_v5(world_id,round_index)
        WHERE snapshot_kind='common';
    CREATE UNIQUE INDEX IF NOT EXISTS experiment_world_private_snapshot_identity
        ON experiment_world_snapshots_v5(
            world_id,round_index,institution_profile_version_id
        ) WHERE snapshot_kind='private';

    CREATE TABLE IF NOT EXISTS experiment_engine_orders (
        id TEXT PRIMARY KEY,
        transition_id TEXT NOT NULL,
        institution_profile_version_id TEXT NOT NULL,
        source_decision_id TEXT NOT NULL,
        source_action_id TEXT NOT NULL,
        order_kind TEXT NOT NULL,
        instrument_id TEXT NOT NULL,
        side TEXT NOT NULL CHECK(side IN ('buy','sell','none')),
        requested_system_notional_pct REAL NOT NULL CHECK(requested_system_notional_pct >= 0),
        executed_system_notional_pct REAL NOT NULL CHECK(executed_system_notional_pct >= 0),
        curtailed_system_notional_pct REAL NOT NULL CHECK(curtailed_system_notional_pct >= 0),
        content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
        content_json TEXT NOT NULL CHECK(json_valid(content_json)),
        FOREIGN KEY(transition_id) REFERENCES experiment_world_transitions(id),
        FOREIGN KEY(institution_profile_version_id) REFERENCES experiment_profile_versions(id),
        FOREIGN KEY(source_decision_id) REFERENCES experiment_decisions(id)
    );

    CREATE TRIGGER IF NOT EXISTS experiment_approval_hash_matches
    BEFORE INSERT ON experiment_preregistration_approvals
    FOR EACH ROW
    WHEN NEW.approved_sha256 != (
        SELECT content_sha256 FROM experiment_preregistrations
        WHERE id = NEW.preregistration_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'approved preregistration hash does not match');
    END;

    CREATE TRIGGER IF NOT EXISTS experiment_evidence_approval_hash_matches
    BEFORE INSERT ON experiment_evidence_approvals
    FOR EACH ROW
    WHEN NEW.approved_sha256 != (
        SELECT content_sha256 FROM experiment_evidence_snapshots
        WHERE id = NEW.snapshot_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'approved evidence hash does not match');
    END;

    DROP TRIGGER IF EXISTS experiment_world_member_matches_initial_cell;
    CREATE TRIGGER experiment_world_member_matches_initial_cell
    BEFORE INSERT ON experiment_world_members
    FOR EACH ROW
    WHEN NOT EXISTS (
        SELECT 1
        FROM experiment_run_cells AS cell
        JOIN experiment_worlds AS world ON world.id = NEW.world_id
        WHERE cell.id = NEW.initial_run_cell_id
          AND cell.stage = 'initial'
          AND cell.preregistration_id = world.preregistration_id
          AND cell.institution_profile_version_id = NEW.institution_profile_version_id
          AND cell.model_spec_id = NEW.model_spec_id
          AND cell.replicate_index = world.replicate_index
          AND EXISTS (
              SELECT 1 FROM experiment_run_attempts AS attempt
              WHERE attempt.run_cell_id = cell.id AND attempt.outcome = 'accepted'
          )
    )
    BEGIN
        SELECT RAISE(ABORT, 'world member does not match its initial run cell');
    END;

    CREATE TRIGGER IF NOT EXISTS experiment_feedback_cell_matches_world_member
    BEFORE INSERT ON experiment_run_cells
    FOR EACH ROW
    WHEN NEW.stage = 'feedback' AND NOT EXISTS (
        SELECT 1
        FROM experiment_world_members AS member
        JOIN experiment_worlds AS world ON world.id = member.world_id
        WHERE member.world_id = NEW.world_id
          AND member.institution_profile_version_id = NEW.institution_profile_version_id
          AND member.model_spec_id = NEW.model_spec_id
          AND world.preregistration_id = NEW.preregistration_id
          AND world.replicate_index = NEW.replicate_index
    )
    BEGIN
        SELECT RAISE(ABORT, 'feedback cell does not match its world membership');
    END;

    """)

    immutable_tables = (
        "experiment_profile_versions",
        "experiment_holdings",
        "experiment_model_specs",
        "experiment_preregistrations",
        "experiment_preregistration_profiles",
        "experiment_preregistration_models",
        "experiment_preregistration_approvals",
        "experiment_evidence_snapshots",
        "experiment_evidence_items",
        "experiment_evidence_approvals",
        "experiment_worlds",
        "experiment_snapshots",
        "experiment_run_cells",
        "experiment_world_members",
        "experiment_run_attempts",
        "experiment_decisions",
        "experiment_actions",
        "experiment_audit_events",
        "experiment_metric_results",
        "experiment_world_transitions",
        "experiment_world_snapshots_v5",
        "experiment_engine_orders",
    )
    for table in immutable_tables:
        db.executescript(f"""
        CREATE TRIGGER IF NOT EXISTS {table}_no_update
        BEFORE UPDATE ON {table}
        BEGIN
            SELECT RAISE(ABORT, '{table} is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS {table}_no_delete
        BEFORE DELETE ON {table}
        BEGIN
            SELECT RAISE(ABORT, '{table} is append-only');
        END;
        """)
    columns = {
        row[1] for row in db.execute("PRAGMA table_info(experiment_run_cells)")
    }
    if "evidence_snapshot_id" not in columns:
        db.execute(
            "ALTER TABLE experiment_run_cells ADD COLUMN evidence_snapshot_id TEXT"
        )
    if "case_input_json" not in columns:
        db.execute(
            "ALTER TABLE experiment_run_cells ADD COLUMN case_input_json TEXT"
        )
    attempt_columns = {
        row[1] for row in db.execute("PRAGMA table_info(experiment_run_attempts)")
    }
    if "provider_request_json" not in attempt_columns:
        db.execute(
            "ALTER TABLE experiment_run_attempts ADD COLUMN provider_request_json TEXT"
        )
    if "raw_response_text" not in attempt_columns:
        db.execute(
            "ALTER TABLE experiment_run_attempts ADD COLUMN raw_response_text TEXT"
        )
    world_columns = {
        row[1] for row in db.execute("PRAGMA table_info(experiment_worlds)")
    }
    if "evidence_snapshot_id" not in world_columns:
        db.execute(
            "ALTER TABLE experiment_worlds ADD COLUMN evidence_snapshot_id TEXT"
        )
    if "stage1_action_set_sha256" not in world_columns:
        db.execute(
            "ALTER TABLE experiment_worlds ADD COLUMN stage1_action_set_sha256 TEXT"
        )
    # Existing v5 development databases predate the evidence column. Create this
    # trigger only after the additive migration so SQLite can resolve NEW.column.
    db.executescript("""
    DROP TRIGGER IF EXISTS experiment_run_cell_has_frozen_evidence;
    CREATE TRIGGER experiment_run_cell_has_frozen_evidence
    BEFORE INSERT ON experiment_run_cells
    FOR EACH ROW
    WHEN NEW.evidence_snapshot_id IS NULL OR NEW.case_input_sha256 IS NULL
      OR NEW.case_input_json IS NULL OR NOT EXISTS (
        SELECT 1 FROM experiment_evidence_snapshots
        WHERE id = NEW.evidence_snapshot_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'run cell requires frozen evidence and exact case input');
    END;
    """)
    for version in MIGRATION_VERSIONS:
        db.execute(
            "INSERT OR IGNORE INTO experiment_schema_migrations(version,applied_at) "
            "VALUES (?,?)",
            (version, _now()),
        )


class ExperimentStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        with self._connect() as db:
            migrate_v5_schema(db)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _append_audit(db: sqlite3.Connection, *, actor: str, event_type: str,
                      object_type: str, object_id: str, payload: dict) -> str:
        previous = db.execute(
            "SELECT event_sha256 FROM experiment_audit_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        previous_sha = previous["event_sha256"] if previous else None
        at = _now()
        payload_json = canonical_json(payload)
        payload_sha = content_sha256(payload)
        event = {
            "at": at,
            "actor": actor,
            "event_type": event_type,
            "object_type": object_type,
            "object_id": object_id,
            "payload_sha256": payload_sha,
            "previous_event_sha256": previous_sha,
        }
        event_sha = content_sha256(event)
        db.execute(
            "INSERT INTO experiment_audit_events "
            "(at,actor,event_type,object_type,object_id,payload_sha256,payload_json,"
            "previous_event_sha256,event_sha256) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                at,
                actor,
                event_type,
                object_type,
                object_id,
                payload_sha,
                payload_json,
                previous_sha,
                event_sha,
            ),
        )
        return event_sha

    @staticmethod
    def _same_or_conflict(db: sqlite3.Connection, table: str, row_id: str,
                          digest: str) -> bool:
        row = db.execute(
            f"SELECT content_sha256 FROM {table} WHERE id=?", (row_id,)
        ).fetchone()
        if row is None:
            return False
        if row["content_sha256"] != digest:
            raise ImmutableConflict(f"{table} {row_id} already has different content")
        return True

    def register_profile(self, profile: InstitutionProfileV5) -> str:
        payload = canonical_json(profile)
        digest = content_sha256(profile)
        with self._connect() as db:
            if self._same_or_conflict(
                db, "experiment_profile_versions", profile.profile_version_id, digest
            ):
                return digest
            db.execute(
                "INSERT INTO experiment_profile_versions "
                "(id,institution_id,catalog_version,created_at,content_sha256,"
                "content_json,holdings_status) VALUES (?,?,?,?,?,?,?)",
                (
                    profile.profile_version_id,
                    profile.institution_id,
                    profile.catalog_version,
                    _now(),
                    digest,
                    payload,
                    profile.holdings_status,
                ),
            )
            for ordinal, holding in enumerate(profile.holdings):
                holding_payload = canonical_json(holding)
                holding_digest = content_sha256(holding)
                if isinstance(holding, HoldingPosition):
                    position_id = holding.position_id
                    values = (
                        holding.mapping_status,
                        holding.instrument_id,
                        holding.side,
                        holding.notional_pct,
                        holding.size_basis,
                        holding.currency,
                    )
                elif isinstance(holding, ExposureDescriptor):
                    position_id = holding.exposure_id
                    values = (holding.mapping_status, None, None, None, None, None)
                else:  # pragma: no cover - the Pydantic union prevents this
                    raise TypeError(type(holding).__name__)
                db.execute(
                    "INSERT INTO experiment_holdings "
                    "(profile_version_id,position_id,ordinal,mapping_status,"
                    "instrument_id,side,notional_pct,size_basis,currency,"
                    "content_sha256,content_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        profile.profile_version_id,
                        position_id,
                        ordinal,
                        *values,
                        holding_digest,
                        holding_payload,
                    ),
                )
            self._append_audit(
                db,
                actor="system:migration",
                event_type="profile_version_registered",
                object_type="institution_profile",
                object_id=profile.profile_version_id,
                payload={"content_sha256": digest},
            )
        return digest

    def register_model(self, model: ModelRegistration) -> str:
        payload = canonical_json(model)
        digest = content_sha256(model)
        with self._connect() as db:
            if self._same_or_conflict(db, "experiment_model_specs", model.model_id, digest):
                return digest
            db.execute(
                "INSERT INTO experiment_model_specs "
                "(id,provider,exact_model,created_at,content_sha256,content_json) "
                "VALUES (?,?,?,?,?,?)",
                (
                    model.model_id,
                    model.provider,
                    model.exact_model,
                    _now(),
                    digest,
                    payload,
                ),
            )
            self._append_audit(
                db,
                actor="system:configuration",
                event_type="model_spec_registered",
                object_type="model_spec",
                object_id=model.model_id,
                payload={"content_sha256": digest},
            )
        return digest

    def create_preregistration(self, plan: ExperimentPreregistration) -> str:
        payload = canonical_json(plan)
        digest = content_sha256(plan)
        with self._connect() as db:
            if self._same_or_conflict(
                db, "experiment_preregistrations", plan.preregistration_id, digest
            ):
                return digest
            db.execute(
                "INSERT INTO experiment_preregistrations "
                "(id,schema_version,created_at,content_sha256,content_json) "
                "VALUES (?,?,?,?,?)",
                (plan.preregistration_id, 5, _now(), digest, payload),
            )
            for ordinal, profile_id in enumerate(plan.institution_profile_version_ids):
                db.execute(
                    "INSERT INTO experiment_preregistration_profiles "
                    "(preregistration_id,ordinal,profile_version_id) VALUES (?,?,?)",
                    (plan.preregistration_id, ordinal, profile_id),
                )
            for ordinal, model in enumerate(plan.models):
                db.execute(
                    "INSERT INTO experiment_preregistration_models "
                    "(preregistration_id,ordinal,model_spec_id) VALUES (?,?,?)",
                    (plan.preregistration_id, ordinal, model.model_id),
                )
            self._append_audit(
                db,
                actor="system:configuration",
                event_type="preregistration_created",
                object_type="preregistration",
                object_id=plan.preregistration_id,
                payload={"content_sha256": digest},
            )
        return digest

    def approve_preregistration(self, preregistration_id: str, approval_id: str,
                                actor: str, note: str, approved_sha256: str,
                                approved: bool = True) -> None:
        if not actor.strip():
            raise ValueError("approval actor is required")
        with self._connect() as db:
            expected = db.execute(
                "SELECT content_sha256 FROM experiment_preregistrations WHERE id=?",
                (preregistration_id,),
            ).fetchone()
            if expected is None:
                raise KeyError(preregistration_id)
            if approved_sha256 != expected["content_sha256"]:
                raise ValueError("approved preregistration hash does not match")
            existing = db.execute(
                "SELECT preregistration_id,approved_sha256 FROM "
                "experiment_preregistration_approvals WHERE id=?",
                (approval_id,),
            ).fetchone()
            if existing:
                if (
                    existing["preregistration_id"] == preregistration_id
                    and existing["approved_sha256"] == approved_sha256
                ):
                    return
                raise ImmutableConflict(f"approval {approval_id} already exists")
            db.execute(
                "INSERT INTO experiment_preregistration_approvals "
                "(id,preregistration_id,approved_at,actor,approved,note,approved_sha256) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    approval_id,
                    preregistration_id,
                    _now(),
                    actor.strip(),
                    int(approved),
                    note.strip(),
                    approved_sha256,
                ),
            )
            self._append_audit(
                db,
                actor=actor.strip(),
                event_type=(
                    "preregistration_approved" if approved
                    else "preregistration_rejected"
                ),
                object_type="preregistration",
                object_id=preregistration_id,
                payload={
                    "approval_id": approval_id,
                    "approved": approved,
                    "approved_sha256": approved_sha256,
                    "note": note.strip(),
                },
            )

    def register_evidence(self, snapshot: EvidenceSnapshotV5) -> str:
        payload = canonical_json(snapshot)
        digest = content_sha256(snapshot)
        with self._connect() as db:
            if self._same_or_conflict(
                db, "experiment_evidence_snapshots", snapshot.snapshot_id, digest
            ):
                return digest
            db.execute(
                "INSERT INTO experiment_evidence_snapshots "
                "(id,event_id,decision_cutoff,created_at,created_by,content_sha256,"
                "content_json) VALUES (?,?,?,?,?,?,?)",
                (
                    snapshot.snapshot_id,
                    snapshot.event_id,
                    snapshot.decision_cutoff.isoformat(),
                    _now(),
                    snapshot.created_by,
                    digest,
                    payload,
                ),
            )
            for ordinal, item in enumerate(snapshot.items):
                item_payload = canonical_json(item)
                db.execute(
                    "INSERT INTO experiment_evidence_items "
                    "(snapshot_id,evidence_id,ordinal,evidence_class,instrument_id,"
                    "observed_at,available_at,raw_sha256,content_sha256,content_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        snapshot.snapshot_id,
                        item.evidence_id,
                        ordinal,
                        item.evidence_class,
                        item.instrument_id,
                        item.observed_at.isoformat(),
                        item.available_at.isoformat(),
                        item.raw_sha256,
                        content_sha256(item),
                        item_payload,
                    ),
                )
            self._append_audit(
                db,
                actor=snapshot.created_by,
                event_type="evidence_snapshot_created",
                object_type="evidence_snapshot",
                object_id=snapshot.snapshot_id,
                payload={"content_sha256": digest, "items": len(snapshot.items)},
            )
        return digest

    def approve_evidence(self, snapshot_id: str, approval_id: str, actor: str,
                         note: str, approved_sha256: str,
                         approved: bool = True) -> None:
        if not actor.strip():
            raise ValueError("approval actor is required")
        with self._connect() as db:
            expected = db.execute(
                "SELECT content_sha256 FROM experiment_evidence_snapshots WHERE id=?",
                (snapshot_id,),
            ).fetchone()
            if expected is None:
                raise KeyError(snapshot_id)
            if approved_sha256 != expected["content_sha256"]:
                raise ValueError("approved evidence hash does not match")
            db.execute(
                "INSERT INTO experiment_evidence_approvals "
                "(id,snapshot_id,approved_at,actor,approved,note,approved_sha256) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    approval_id,
                    snapshot_id,
                    _now(),
                    actor.strip(),
                    int(approved),
                    note.strip(),
                    approved_sha256,
                ),
            )
            self._append_audit(
                db,
                actor=actor.strip(),
                event_type="evidence_approved" if approved else "evidence_rejected",
                object_type="evidence_snapshot",
                object_id=snapshot_id,
                payload={
                    "approval_id": approval_id,
                    "approved": approved,
                    "approved_sha256": approved_sha256,
                    "note": note.strip(),
                },
            )

    def approvals_ready(self, preregistration_id: str,
                        evidence_snapshot_id: str) -> dict:
        with self._connect() as db:
            prereg_row = db.execute(
                "SELECT approved FROM experiment_preregistration_approvals "
                "WHERE preregistration_id=? ORDER BY approved_at DESC,rowid DESC LIMIT 1",
                (preregistration_id,),
            ).fetchone()
            evidence_row = db.execute(
                "SELECT approved FROM experiment_evidence_approvals "
                "WHERE snapshot_id=? ORDER BY approved_at DESC,rowid DESC LIMIT 1",
                (evidence_snapshot_id,),
            ).fetchone()
        prereg = bool(prereg_row and prereg_row["approved"])
        evidence = bool(evidence_row and evidence_row["approved"])
        return {
            "preregistration_approved": prereg,
            "evidence_approved": evidence,
            "ready": prereg and evidence,
        }

    def materialize_initial_cells(self, cells: list[dict], actor: str) -> dict:
        """Persist an exact randomized grid only after both human approvals."""

        if not actor.strip():
            raise ValueError("planning actor is required")
        if not cells:
            raise ValueError("initial run plan contains no cells")
        preregistration_ids = {cell["preregistration_id"] for cell in cells}
        evidence_ids = {cell["evidence_snapshot_id"] for cell in cells}
        if len(preregistration_ids) != 1 or len(evidence_ids) != 1:
            raise ValueError("all initial cells must use one plan and one evidence snapshot")
        preregistration_id = next(iter(preregistration_ids))
        evidence_snapshot_id = next(iter(evidence_ids))
        if not self.approvals_ready(preregistration_id, evidence_snapshot_id)["ready"]:
            raise PermissionError(
                "exact preregistration and evidence approvals are required before planning"
            )

        comparable_keys = (
            "id",
            "preregistration_id",
            "evidence_snapshot_id",
            "stage",
            "world_id",
            "institution_profile_version_id",
            "model_spec_id",
            "replicate_index",
            "execution_order",
            "case_input_sha256",
            "case_input_json",
        )
        plan_digest = content_sha256([
            {key: cell[key] for key in comparable_keys}
            for cell in cells
        ])
        with self._connect() as db:
            existing = db.execute(
                "SELECT id,preregistration_id,evidence_snapshot_id,stage,world_id,"
                "institution_profile_version_id,model_spec_id,replicate_index,"
                "execution_order,case_input_sha256,case_input_json "
                "FROM experiment_run_cells WHERE preregistration_id=? AND stage='initial' "
                "ORDER BY execution_order",
                (preregistration_id,),
            ).fetchall()
            if existing:
                existing_values = [
                    {key: row[key] for key in comparable_keys} for row in existing
                ]
                proposed_values = [
                    {key: cell[key] for key in comparable_keys} for cell in cells
                ]
                if existing_values != proposed_values:
                    raise ImmutableConflict(
                        "a different initial run plan already exists for this preregistration"
                    )
                return self._initial_plan_summary(
                    db, preregistration_id, evidence_snapshot_id, plan_digest
                )

            for cell in cells:
                if cell["stage"] != "initial" or cell["world_id"] is not None:
                    raise ValueError("materialized cells must be initial, non-world cells")
                try:
                    case_input = json.loads(cell["case_input_json"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError("case input must be canonical JSON") from exc
                if canonical_json(case_input) != cell["case_input_json"]:
                    raise ValueError("case input JSON is not canonical")
                if content_sha256(case_input) != cell["case_input_sha256"]:
                    raise ValueError("case input hash does not match its content")
                db.execute(
                    "INSERT INTO experiment_run_cells "
                    "(id,preregistration_id,evidence_snapshot_id,stage,world_id,"
                    "institution_profile_version_id,model_spec_id,replicate_index,"
                    "execution_order,case_input_sha256,case_input_json,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        cell["id"],
                        cell["preregistration_id"],
                        cell["evidence_snapshot_id"],
                        cell["stage"],
                        cell["world_id"],
                        cell["institution_profile_version_id"],
                        cell["model_spec_id"],
                        cell["replicate_index"],
                        cell["execution_order"],
                        cell["case_input_sha256"],
                        cell["case_input_json"],
                        _now(),
                    ),
                )
            self._append_audit(
                db,
                actor=actor.strip(),
                event_type="initial_run_plan_materialized",
                object_type="preregistration",
                object_id=preregistration_id,
                payload={
                    "evidence_snapshot_id": evidence_snapshot_id,
                    "cell_count": len(cells),
                    "plan_sha256": plan_digest,
                },
            )
            return self._initial_plan_summary(
                db, preregistration_id, evidence_snapshot_id, plan_digest
            )

    @staticmethod
    def _initial_plan_summary(db: sqlite3.Connection, preregistration_id: str,
                              evidence_snapshot_id: str,
                              plan_sha256: str | None = None) -> dict:
        rows = db.execute(
            "SELECT cell.id,cell.preregistration_id,cell.evidence_snapshot_id,"
            "cell.stage,cell.world_id,cell.institution_profile_version_id,"
            "profile.institution_id,"
            "cell.model_spec_id,cell.replicate_index,cell.execution_order,"
            "cell.case_input_sha256,cell.case_input_json,cell.created_at,"
            "(SELECT count(*) FROM experiment_run_attempts AS attempt "
            " WHERE attempt.run_cell_id=cell.id) AS attempt_count,"
            "(SELECT count(*) FROM experiment_run_attempts AS accepted "
            " WHERE accepted.run_cell_id=cell.id AND accepted.outcome='accepted') "
            "AS accepted_count "
            "FROM experiment_run_cells AS cell "
            "JOIN experiment_profile_versions AS profile "
            "ON profile.id=cell.institution_profile_version_id "
            "WHERE cell.preregistration_id=? AND cell.stage='initial' "
            "ORDER BY cell.execution_order",
            (preregistration_id,),
        ).fetchall()
        if plan_sha256 is None and rows:
            plan_sha256 = content_sha256([
                {
                    "id": row["id"],
                    "preregistration_id": row["preregistration_id"],
                    "evidence_snapshot_id": row["evidence_snapshot_id"],
                    "stage": row["stage"],
                    "world_id": row["world_id"],
                    "institution_profile_version_id": row["institution_profile_version_id"],
                    "model_spec_id": row["model_spec_id"],
                    "replicate_index": row["replicate_index"],
                    "execution_order": row["execution_order"],
                    "case_input_sha256": row["case_input_sha256"],
                    "case_input_json": row["case_input_json"],
                }
                for row in rows
            ])
        return {
            "preregistration_id": preregistration_id,
            "evidence_snapshot_id": evidence_snapshot_id,
            "stage": "initial",
            "cell_count": len(rows),
            "planned_call_count": len(rows),
            "attempted_cell_count": sum(row["attempt_count"] > 0 for row in rows),
            "accepted_cell_count": sum(row["accepted_count"] > 0 for row in rows),
            "remaining_cell_count": sum(row["accepted_count"] == 0 for row in rows),
            "plan_sha256": plan_sha256,
            "cells": [{
                "id": row["id"],
                "institution_profile_version_id": row["institution_profile_version_id"],
                "institution_id": row["institution_id"],
                "model_spec_id": row["model_spec_id"],
                "replicate_index": row["replicate_index"],
                "execution_order": row["execution_order"],
                "case_input_sha256": row["case_input_sha256"],
                "created_at": row["created_at"],
                "attempt_count": row["attempt_count"],
                "accepted": bool(row["accepted_count"]),
            } for row in rows],
        }

    def get_initial_plan(self, preregistration_id: str) -> dict | None:
        with self._connect() as db:
            evidence = db.execute(
                "SELECT evidence_snapshot_id FROM experiment_run_cells "
                "WHERE preregistration_id=? AND stage='initial' "
                "ORDER BY execution_order LIMIT 1",
                (preregistration_id,),
            ).fetchone()
            if evidence is None:
                return None
            return self._initial_plan_summary(
                db, preregistration_id, evidence["evidence_snapshot_id"]
            )

    def begin_initial_execution(self, preregistration_id: str, *, actor: str,
                                confirmed_plan_sha256: str,
                                max_cells: int) -> dict:
        if not actor.strip():
            raise ValueError("execution actor is required")
        with self._connect() as db:
            evidence = db.execute(
                "SELECT evidence_snapshot_id FROM experiment_run_cells "
                "WHERE preregistration_id=? AND stage='initial' "
                "ORDER BY execution_order LIMIT 1",
                (preregistration_id,),
            ).fetchone()
            if evidence is None:
                raise KeyError(preregistration_id)
            summary = self._initial_plan_summary(
                db, preregistration_id, evidence["evidence_snapshot_id"]
            )
            if confirmed_plan_sha256 != summary["plan_sha256"]:
                raise ValueError("confirmed initial plan hash does not match")
            approval = self.approvals_ready(
                preregistration_id, evidence["evidence_snapshot_id"]
            )
            if not approval["ready"]:
                raise PermissionError(
                    "current preregistration and evidence approvals are required"
                )
            self._append_audit(
                db,
                actor=actor.strip(),
                event_type="initial_execution_authorized",
                object_type="preregistration",
                object_id=preregistration_id,
                payload={
                    "confirmed_plan_sha256": confirmed_plan_sha256,
                    "max_cells": max_cells,
                    "remaining_cells": summary["remaining_cell_count"],
                },
            )
        return summary

    def next_pending_initial_cell(self, preregistration_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT cell.id,cell.preregistration_id,cell.evidence_snapshot_id,"
                "cell.institution_profile_version_id,profile.institution_id,"
                "cell.model_spec_id,cell.replicate_index,cell.execution_order,"
                "cell.case_input_sha256,cell.case_input_json,"
                "(SELECT count(*) FROM experiment_run_attempts AS attempt "
                " WHERE attempt.run_cell_id=cell.id) AS attempt_count "
                "FROM experiment_run_cells AS cell "
                "JOIN experiment_profile_versions AS profile "
                "ON profile.id=cell.institution_profile_version_id "
                "WHERE cell.preregistration_id=? AND cell.stage='initial' "
                "AND NOT EXISTS ("
                " SELECT 1 FROM experiment_run_attempts AS accepted "
                " WHERE accepted.run_cell_id=cell.id AND accepted.outcome='accepted'"
                ") ORDER BY cell.execution_order LIMIT 1",
                (preregistration_id,),
            ).fetchone()
        return dict(row) if row else None

    def record_initial_attempt(
        self,
        *,
        run_cell_id: str,
        actor: str,
        started_at: str,
        completed_at: str,
        outcome: str,
        provider_request_sha256: str,
        provider_request_json: str,
        raw_response_text: str | None,
        latency_ms: float | None,
        finish_reason: str | None,
        usage: dict | None,
        error: str | None,
        decision: InstitutionDecisionV5 | None,
        _expected_stage: str = "initial",
    ) -> dict:
        if outcome not in {"accepted", "rejected", "transport_error"}:
            raise ValueError("invalid attempt outcome")
        if (outcome == "accepted") != (decision is not None):
            raise ValueError("accepted attempts require exactly one typed decision")
        if hashlib.sha256(provider_request_json.encode("utf-8")).hexdigest() != (
            provider_request_sha256
        ):
            raise ValueError("provider request hash does not match exact request JSON")
        raw_sha = (
            hashlib.sha256(raw_response_text.encode("utf-8")).hexdigest()
            if raw_response_text is not None else None
        )
        with self._connect() as db:
            cell = db.execute(
                "SELECT cell.stage,profile.institution_id FROM experiment_run_cells AS cell "
                "JOIN experiment_profile_versions AS profile "
                "ON profile.id=cell.institution_profile_version_id "
                "WHERE cell.id=?",
                (run_cell_id,),
            ).fetchone()
            if cell is None:
                raise KeyError(run_cell_id)
            if cell["stage"] != _expected_stage:
                raise ValueError(f"run cell is not in expected {_expected_stage} stage")
            if decision is not None and (
                decision.stage != _expected_stage
                or decision.institution_id != cell["institution_id"]
            ):
                raise ValueError("decision metadata does not match its run cell")
            existing_accepted = db.execute(
                "SELECT 1 FROM experiment_run_attempts "
                "WHERE run_cell_id=? AND outcome='accepted'",
                (run_cell_id,),
            ).fetchone()
            if existing_accepted:
                raise ImmutableConflict(f"run cell {run_cell_id} is already accepted")
            attempt_index = db.execute(
                "SELECT coalesce(max(attempt_index),0)+1 FROM experiment_run_attempts "
                "WHERE run_cell_id=?",
                (run_cell_id,),
            ).fetchone()[0]
            attempt_id = "ATT-" + hashlib.sha256(
                f"{run_cell_id}|{attempt_index}".encode("utf-8")
            ).hexdigest()[:20].upper()
            db.execute(
                "INSERT INTO experiment_run_attempts "
                "(id,run_cell_id,attempt_index,started_at,completed_at,outcome,"
                "provider_request_sha256,raw_response_sha256,latency_ms,finish_reason,"
                "usage_json,provider_request_json,raw_response_text,error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt_id,
                    run_cell_id,
                    attempt_index,
                    started_at,
                    completed_at,
                    outcome,
                    provider_request_sha256,
                    raw_sha,
                    latency_ms,
                    finish_reason,
                    canonical_json(usage) if usage is not None else None,
                    provider_request_json,
                    raw_response_text,
                    error,
                ),
            )
            decision_sha = None
            if decision is not None:
                decision_payload = canonical_json(decision)
                decision_sha = content_sha256(decision)
                db.execute(
                    "INSERT INTO experiment_decisions "
                    "(id,run_attempt_id,institution_id,stage,stance,content_sha256,"
                    "content_json,accepted_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        decision.decision_id,
                        attempt_id,
                        decision.institution_id,
                        decision.stage,
                        decision.stance,
                        decision_sha,
                        decision_payload,
                        completed_at,
                    ),
                )
                for ordinal, action in enumerate(decision.actions):
                    target = getattr(
                        action,
                        "instrument_id",
                        getattr(action, "hedge_instrument_id", None),
                    ) or getattr(action, "market_instrument_id", None)
                    db.execute(
                        "INSERT INTO experiment_actions "
                        "(decision_id,action_id,ordinal,action_type,target_instrument_id,"
                        "size_value,size_basis,content_sha256,content_json) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            decision.decision_id,
                            action.action_id,
                            ordinal,
                            action.action_type,
                            target,
                            action.size_value,
                            action.size_basis,
                            content_sha256(action),
                            canonical_json(action),
                        ),
                    )
            self._append_audit(
                db,
                actor=actor.strip(),
                event_type=f"{_expected_stage}_attempt_{outcome}",
                object_type="run_cell",
                object_id=run_cell_id,
                payload={
                    "attempt_id": attempt_id,
                    "attempt_index": attempt_index,
                    "provider_request_sha256": provider_request_sha256,
                    "raw_response_sha256": raw_sha,
                    "decision_sha256": decision_sha,
                    "error": error,
                },
            )
        return {
            "attempt_id": attempt_id,
            "attempt_index": attempt_index,
            "outcome": outcome,
            "decision_id": decision.decision_id if decision else None,
        }

    def record_feedback_attempt(self, **kwargs) -> dict:
        return self.record_initial_attempt(**kwargs, _expected_stage="feedback")

    def accepted_initial_decisions(self, preregistration_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT cell.id AS run_cell_id,cell.evidence_snapshot_id,"
                "cell.institution_profile_version_id,"
                "profile.institution_id,cell.model_spec_id,cell.replicate_index,"
                "cell.execution_order,decision.content_json,decision.content_sha256 "
                "FROM experiment_run_cells AS cell "
                "JOIN experiment_profile_versions AS profile "
                "ON profile.id=cell.institution_profile_version_id "
                "JOIN experiment_run_attempts AS attempt ON attempt.run_cell_id=cell.id "
                "AND attempt.outcome='accepted' "
                "JOIN experiment_decisions AS decision ON decision.run_attempt_id=attempt.id "
                "WHERE cell.preregistration_id=? AND cell.stage='initial' "
                "ORDER BY cell.execution_order",
                (preregistration_id,),
            ).fetchall()
        return [{
            **{key: row[key] for key in (
                "run_cell_id",
                "evidence_snapshot_id",
                "institution_profile_version_id",
                "institution_id",
                "model_spec_id",
                "replicate_index",
                "execution_order",
                "content_sha256",
            )},
            "decision": json.loads(row["content_json"]),
        } for row in rows]

    def materialize_feedback_worlds(self, worlds: list[dict], *, actor: str) -> dict:
        if not actor.strip():
            raise ValueError("world planning actor is required")
        if len(worlds) != 60:
            raise ValueError("the confirmatory design requires exactly 60 worlds")
        preregistration_ids = {world["preregistration_id"] for world in worlds}
        evidence_ids = {world["evidence_snapshot_id"] for world in worlds}
        if len(preregistration_ids) != 1 or len(evidence_ids) != 1:
            raise ValueError("all worlds must share one preregistration and evidence snapshot")
        preregistration_id = next(iter(preregistration_ids))
        evidence_snapshot_id = next(iter(evidence_ids))
        if not self.approvals_ready(preregistration_id, evidence_snapshot_id)["ready"]:
            raise PermissionError("current plan and evidence approvals are required")
        if any(len(world["members"]) != 7 for world in worlds):
            raise ValueError("every feedback world requires exactly seven members")
        if sum(world["assignment_kind"] == "shared" for world in worlds) != 30:
            raise ValueError("the confirmatory design requires 30 shared worlds")
        if sum(world["assignment_kind"] == "heterogeneous" for world in worlds) != 30:
            raise ValueError("the confirmatory design requires 30 heterogeneous worlds")

        comparable = (
            "id",
            "preregistration_id",
            "evidence_snapshot_id",
            "assignment_kind",
            "assignment_key",
            "replicate_index",
            "assignment_sha256",
            "stage1_action_set_sha256",
        )
        world_plan_sha = content_sha256([
            {
                **{key: world[key] for key in comparable},
                "members": [{
                    key: member[key] for key in (
                        "ordinal",
                        "institution_profile_version_id",
                        "model_spec_id",
                        "initial_run_cell_id",
                        "initial_decision_sha256",
                    )
                } for member in world["members"]],
            }
            for world in worlds
        ])
        with self._connect() as db:
            existing = db.execute(
                "SELECT count(*) FROM experiment_worlds WHERE preregistration_id=?",
                (preregistration_id,),
            ).fetchone()[0]
            if existing:
                summary = self._feedback_world_summary(db, preregistration_id)
                if existing != 60 or summary["world_plan_sha256"] != world_plan_sha:
                    raise ImmutableConflict(
                        "a different feedback world plan already exists"
                    )
                return summary

            for world in worlds:
                for member in world["members"]:
                    accepted = db.execute(
                        "SELECT decision.content_sha256 FROM experiment_run_cells AS cell "
                        "JOIN experiment_run_attempts AS attempt "
                        "ON attempt.run_cell_id=cell.id AND attempt.outcome='accepted' "
                        "JOIN experiment_decisions AS decision "
                        "ON decision.run_attempt_id=attempt.id "
                        "WHERE cell.id=?",
                        (member["initial_run_cell_id"],),
                    ).fetchone()
                    if accepted is None or accepted["content_sha256"] != member[
                        "initial_decision_sha256"
                    ]:
                        raise ValueError(
                            "world member does not resolve to its exact accepted decision"
                        )
                db.execute(
                    "INSERT INTO experiment_worlds "
                    "(id,preregistration_id,evidence_snapshot_id,assignment_kind,"
                    "assignment_key,replicate_index,assignment_sha256,"
                    "stage1_action_set_sha256,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        world["id"],
                        world["preregistration_id"],
                        world["evidence_snapshot_id"],
                        world["assignment_kind"],
                        world["assignment_key"],
                        world["replicate_index"],
                        world["assignment_sha256"],
                        world["stage1_action_set_sha256"],
                        _now(),
                    ),
                )
                for member in world["members"]:
                    db.execute(
                        "INSERT INTO experiment_world_members "
                        "(world_id,institution_profile_version_id,model_spec_id,"
                        "initial_run_cell_id,ordinal) VALUES (?,?,?,?,?)",
                        (
                            world["id"],
                            member["institution_profile_version_id"],
                            member["model_spec_id"],
                            member["initial_run_cell_id"],
                            member["ordinal"],
                        ),
                    )
            self._append_audit(
                db,
                actor=actor.strip(),
                event_type="feedback_world_plan_materialized",
                object_type="preregistration",
                object_id=preregistration_id,
                payload={
                    "evidence_snapshot_id": evidence_snapshot_id,
                    "world_count": len(worlds),
                    "member_count": sum(len(world["members"]) for world in worlds),
                    "world_plan_sha256": world_plan_sha,
                },
            )
            return self._feedback_world_summary(db, preregistration_id)

    @staticmethod
    def _feedback_world_summary(db: sqlite3.Connection,
                                preregistration_id: str) -> dict:
        rows = db.execute(
            "SELECT id,preregistration_id,evidence_snapshot_id,assignment_kind,"
            "assignment_key,replicate_index,assignment_sha256,"
            "stage1_action_set_sha256,created_at FROM experiment_worlds "
            "WHERE preregistration_id=? ORDER BY assignment_kind,assignment_key,"
            "replicate_index",
            (preregistration_id,),
        ).fetchall()
        worlds = []
        digest_rows = []
        for row in rows:
            members = db.execute(
                "SELECT member.ordinal,member.institution_profile_version_id,"
                "member.model_spec_id,member.initial_run_cell_id,"
                "decision.content_sha256 AS initial_decision_sha256 "
                "FROM experiment_world_members AS member "
                "JOIN experiment_run_attempts AS attempt "
                "ON attempt.run_cell_id=member.initial_run_cell_id "
                "AND attempt.outcome='accepted' "
                "JOIN experiment_decisions AS decision "
                "ON decision.run_attempt_id=attempt.id "
                "WHERE member.world_id=? ORDER BY member.ordinal",
                (row["id"],),
            ).fetchall()
            world_fields = {key: row[key] for key in (
                "id",
                "preregistration_id",
                "evidence_snapshot_id",
                "assignment_kind",
                "assignment_key",
                "replicate_index",
                "assignment_sha256",
                "stage1_action_set_sha256",
            )}
            member_fields = [dict(member) for member in members]
            digest_rows.append({**world_fields, "members": member_fields})
            worlds.append({
                **world_fields,
                "created_at": row["created_at"],
                "member_count": len(members),
            })
        return {
            "preregistration_id": preregistration_id,
            "world_count": len(rows),
            "shared_world_count": sum(
                row["assignment_kind"] == "shared" for row in rows
            ),
            "heterogeneous_world_count": sum(
                row["assignment_kind"] == "heterogeneous" for row in rows
            ),
            "member_count": sum(world["member_count"] for world in worlds),
            "world_plan_sha256": content_sha256(digest_rows) if rows else None,
            "worlds": worlds,
        }

    def get_feedback_world_plan(self, preregistration_id: str) -> dict | None:
        with self._connect() as db:
            exists = db.execute(
                "SELECT 1 FROM experiment_worlds WHERE preregistration_id=? LIMIT 1",
                (preregistration_id,),
            ).fetchone()
            return (
                self._feedback_world_summary(db, preregistration_id)
                if exists else None
            )

    def world_transition_input(self, world_id: str) -> dict | None:
        with self._connect() as db:
            world = db.execute(
                "SELECT id,preregistration_id,evidence_snapshot_id,assignment_kind,"
                "assignment_key,replicate_index,assignment_sha256,"
                "stage1_action_set_sha256 FROM experiment_worlds WHERE id=?",
                (world_id,),
            ).fetchone()
            if world is None:
                return None
            members = db.execute(
                "SELECT member.ordinal,member.institution_profile_version_id,"
                "profile.institution_id,profile.content_json AS profile_json,"
                "member.model_spec_id,member.initial_run_cell_id,"
                "decision.id AS decision_id,decision.content_sha256 AS "
                "initial_decision_sha256,decision.content_json AS decision_json "
                "FROM experiment_world_members AS member "
                "JOIN experiment_profile_versions AS profile "
                "ON profile.id=member.institution_profile_version_id "
                "JOIN experiment_run_attempts AS attempt "
                "ON attempt.run_cell_id=member.initial_run_cell_id "
                "AND attempt.outcome='accepted' "
                "JOIN experiment_decisions AS decision "
                "ON decision.run_attempt_id=attempt.id "
                "WHERE member.world_id=? ORDER BY member.ordinal",
                (world_id,),
            ).fetchall()
        return {
            "world": dict(world),
            "members": [{
                "ordinal": row["ordinal"],
                "institution_profile_version_id": row[
                    "institution_profile_version_id"
                ],
                "institution_id": row["institution_id"],
                "model_spec_id": row["model_spec_id"],
                "initial_run_cell_id": row["initial_run_cell_id"],
                "initial_decision_sha256": row["initial_decision_sha256"],
                "decision": json.loads(row["decision_json"]),
                "profile": json.loads(row["profile_json"]),
            } for row in members],
        }

    def untransitioned_world_ids(self, preregistration_id: str) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT world.id FROM experiment_worlds AS world "
                "WHERE world.preregistration_id=? AND NOT EXISTS ("
                " SELECT 1 FROM experiment_world_transitions AS transition "
                " WHERE transition.world_id=world.id AND transition.round_index=1"
                ") ORDER BY world.assignment_kind,world.assignment_key,"
                "world.replicate_index",
                (preregistration_id,),
            ).fetchall()
        return [row["id"] for row in rows]

    def persist_world_transition(self, result: dict, *, actor: str) -> dict:
        if not actor.strip():
            raise ValueError("transition actor is required")
        transition = result["transition"]
        transition_payload = canonical_json(transition)
        transition_sha = content_sha256(transition)
        common = result["common_snapshot"]
        private = result["private_snapshots"]
        if content_sha256(common["content"]) != common["sha256"]:
            raise ValueError("common snapshot hash does not match content")
        if any(content_sha256(item["content"]) != item["sha256"] for item in private):
            raise ValueError("private snapshot hash does not match content")
        if len(private) != 7:
            raise ValueError("transition requires exactly seven private snapshots")
        if transition["common_snapshot_sha256"] != common["sha256"]:
            raise ValueError("transition common snapshot link does not match")
        linked_private = {
            item["content"]["institution_profile_version_id"]: item["sha256"]
            for item in private
        }
        if transition["private_snapshot_sha256"] != linked_private:
            raise ValueError("transition private snapshot links do not match")

        with self._connect() as db:
            world = db.execute(
                "SELECT preregistration_id,evidence_snapshot_id,"
                "stage1_action_set_sha256 FROM experiment_worlds WHERE id=?",
                (transition["world_id"],),
            ).fetchone()
            if world is None:
                raise KeyError(transition["world_id"])
            if not self.approvals_ready(
                world["preregistration_id"], world["evidence_snapshot_id"]
            )["ready"]:
                raise PermissionError(
                    "current plan and evidence approvals are required for transition"
                )
            plan_row = db.execute(
                "SELECT content_json FROM experiment_preregistrations WHERE id=?",
                (world["preregistration_id"],),
            ).fetchone()
            plan = ExperimentPreregistration.model_validate(
                json.loads(plan_row["content_json"])
            )
            if transition["transition_policy_sha256"] != plan.transition_policy.sha256:
                raise ValueError("transition policy does not match the preregistration")
            if transition["round_index"] == 1 and transition["input_stage"] == "initial":
                expected_action_set_sha = world["stage1_action_set_sha256"]
            elif transition["round_index"] == 2 and transition["input_stage"] == "feedback":
                feedback_rows = db.execute(
                    "SELECT member.institution_profile_version_id,cell.id AS run_cell_id,"
                    "decision.content_sha256 FROM experiment_world_members AS member "
                    "JOIN experiment_run_cells AS cell ON cell.world_id=member.world_id "
                    "AND cell.institution_profile_version_id="
                    "member.institution_profile_version_id AND cell.stage='feedback' "
                    "JOIN experiment_run_attempts AS attempt ON attempt.run_cell_id=cell.id "
                    "AND attempt.outcome='accepted' "
                    "JOIN experiment_decisions AS decision "
                    "ON decision.run_attempt_id=attempt.id "
                    "WHERE member.world_id=? ORDER BY member.ordinal",
                    (transition["world_id"],),
                ).fetchall()
                if len(feedback_rows) != 7:
                    raise ValueError(
                        "round two requires seven accepted feedback decisions"
                    )
                expected_action_set_sha = content_sha256([{
                    "institution_profile_version_id": row[
                        "institution_profile_version_id"
                    ],
                    "initial_run_cell_id": row["run_cell_id"],
                    "initial_decision_sha256": row["content_sha256"],
                } for row in feedback_rows])
            else:
                raise ValueError("transition round and input stage do not match")
            if expected_action_set_sha != transition["input_action_set_sha256"]:
                raise ValueError("transition input does not match accepted decisions")
            existing = db.execute(
                "SELECT content_sha256 FROM experiment_world_transitions WHERE id=?",
                (transition["transition_id"],),
            ).fetchone()
            if existing:
                if existing["content_sha256"] != transition_sha:
                    raise ImmutableConflict(
                        f"transition {transition['transition_id']} has different content"
                    )
                return self._world_transition_record(db, transition["transition_id"])

            db.execute(
                "INSERT INTO experiment_world_transitions "
                "(id,world_id,round_index,input_stage,input_action_set_sha256,"
                "transition_policy_sha256,created_at,content_sha256,content_json) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    transition["transition_id"],
                    transition["world_id"],
                    transition["round_index"],
                    transition["input_stage"],
                    transition["input_action_set_sha256"],
                    transition["transition_policy_sha256"],
                    _now(),
                    transition_sha,
                    transition_payload,
                ),
            )
            for snapshot, kind in [(common, "common"), *(
                (item, "private") for item in private
            )]:
                content = snapshot["content"]
                db.execute(
                    "INSERT INTO experiment_world_snapshots_v5 "
                    "(id,transition_id,world_id,round_index,snapshot_kind,"
                    "institution_profile_version_id,content_sha256,content_json,"
                    "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        snapshot["id"],
                        transition["transition_id"],
                        transition["world_id"],
                        transition["round_index"],
                        kind,
                        content.get("institution_profile_version_id"),
                        snapshot["sha256"],
                        canonical_json(content),
                        _now(),
                    ),
                )
            for order in result["orders"]:
                db.execute(
                    "INSERT INTO experiment_engine_orders "
                    "(id,transition_id,institution_profile_version_id,"
                    "source_decision_id,source_action_id,order_kind,instrument_id,side,"
                    "requested_system_notional_pct,executed_system_notional_pct,"
                    "curtailed_system_notional_pct,content_sha256,content_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        order["order_id"],
                        transition["transition_id"],
                        order["institution_profile_version_id"],
                        order["source_decision_id"],
                        order["source_action_id"],
                        order["order_kind"],
                        order["instrument_id"],
                        order["side"],
                        order["requested_system_notional_pct"],
                        order["executed_system_notional_pct"],
                        order["curtailed_system_notional_pct"],
                        content_sha256(order),
                        canonical_json(order),
                    ),
                )
            self._append_audit(
                db,
                actor=actor.strip(),
                event_type="world_transition_materialized",
                object_type="world_transition",
                object_id=transition["transition_id"],
                payload={
                    "world_id": transition["world_id"],
                    "round_index": transition["round_index"],
                    "transition_sha256": transition_sha,
                    "common_snapshot_sha256": common["sha256"],
                    "private_snapshot_sha256": linked_private,
                    "order_count": len(result["orders"]),
                },
            )
            return self._world_transition_record(db, transition["transition_id"])

    @staticmethod
    def _world_transition_record(db: sqlite3.Connection,
                                 transition_id: str) -> dict:
        transition = db.execute(
            "SELECT id,world_id,round_index,input_stage,input_action_set_sha256,"
            "transition_policy_sha256,created_at,content_sha256,content_json "
            "FROM experiment_world_transitions WHERE id=?",
            (transition_id,),
        ).fetchone()
        snapshots = db.execute(
            "SELECT id,snapshot_kind,institution_profile_version_id,content_sha256 "
            "FROM experiment_world_snapshots_v5 WHERE transition_id=? "
            "ORDER BY snapshot_kind,institution_profile_version_id",
            (transition_id,),
        ).fetchall()
        order_count = db.execute(
            "SELECT count(*) FROM experiment_engine_orders WHERE transition_id=?",
            (transition_id,),
        ).fetchone()[0]
        return {
            "id": transition["id"],
            "world_id": transition["world_id"],
            "round_index": transition["round_index"],
            "input_stage": transition["input_stage"],
            "input_action_set_sha256": transition["input_action_set_sha256"],
            "transition_policy_sha256": transition["transition_policy_sha256"],
            "created_at": transition["created_at"],
            "sha256": transition["content_sha256"],
            "content": json.loads(transition["content_json"]),
            "snapshots": [dict(row) for row in snapshots],
            "order_count": order_count,
        }

    def get_world_transition(self, world_id: str, round_index: int = 1) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id FROM experiment_world_transitions "
                "WHERE world_id=? AND round_index=?",
                (world_id, round_index),
            ).fetchone()
            return self._world_transition_record(db, row["id"]) if row else None

    def feedback_world_inputs(self, preregistration_id: str) -> list[dict]:
        with self._connect() as db:
            worlds = db.execute(
                "SELECT world.id,world.preregistration_id,world.evidence_snapshot_id,"
                "world.assignment_kind,world.assignment_key,world.replicate_index,"
                "world.stage1_action_set_sha256,transition.id AS transition_id "
                "FROM experiment_worlds AS world "
                "JOIN experiment_world_transitions AS transition "
                "ON transition.world_id=world.id AND transition.round_index=1 "
                "WHERE world.preregistration_id=? "
                "ORDER BY world.assignment_kind,world.assignment_key,"
                "world.replicate_index",
                (preregistration_id,),
            ).fetchall()
            output = []
            for world in worlds:
                common = db.execute(
                    "SELECT content_json FROM experiment_world_snapshots_v5 "
                    "WHERE transition_id=? AND snapshot_kind='common'",
                    (world["transition_id"],),
                ).fetchone()
                members = db.execute(
                    "SELECT member.ordinal,member.institution_profile_version_id,"
                    "profile.institution_id,member.model_spec_id,"
                    "member.initial_run_cell_id,cell.case_input_json,"
                    "private.content_json AS private_json "
                    "FROM experiment_world_members AS member "
                    "JOIN experiment_profile_versions AS profile "
                    "ON profile.id=member.institution_profile_version_id "
                    "JOIN experiment_run_cells AS cell "
                    "ON cell.id=member.initial_run_cell_id "
                    "JOIN experiment_world_snapshots_v5 AS private "
                    "ON private.transition_id=? AND private.snapshot_kind='private' "
                    "AND private.institution_profile_version_id="
                    "member.institution_profile_version_id "
                    "WHERE member.world_id=? ORDER BY member.ordinal",
                    (world["transition_id"], world["id"]),
                ).fetchall()
                output.append({
                    "world": {
                        key: world[key] for key in (
                            "id", "preregistration_id", "evidence_snapshot_id",
                            "assignment_kind", "assignment_key", "replicate_index",
                            "stage1_action_set_sha256",
                        )
                    },
                    "common_snapshot": json.loads(common["content_json"]),
                    "members": [{
                        "ordinal": member["ordinal"],
                        "institution_profile_version_id": member[
                            "institution_profile_version_id"
                        ],
                        "institution_id": member["institution_id"],
                        "model_spec_id": member["model_spec_id"],
                        "initial_run_cell_id": member["initial_run_cell_id"],
                        "initial_case_input_json": member["case_input_json"],
                        "private_snapshot": json.loads(member["private_json"]),
                    } for member in members],
                })
        return output

    def materialize_feedback_cells(self, cells: list[dict], *, actor: str) -> dict:
        if not actor.strip():
            raise ValueError("feedback planning actor is required")
        if len(cells) != 420:
            raise ValueError("the confirmatory feedback grid requires 420 cells")
        preregistration_ids = {cell["preregistration_id"] for cell in cells}
        evidence_ids = {cell["evidence_snapshot_id"] for cell in cells}
        world_ids = {cell["world_id"] for cell in cells}
        if len(preregistration_ids) != 1 or len(evidence_ids) != 1:
            raise ValueError("feedback cells must share one plan and evidence snapshot")
        if len(world_ids) != 60:
            raise ValueError("feedback cells must cover exactly 60 worlds")
        preregistration_id = next(iter(preregistration_ids))
        evidence_snapshot_id = next(iter(evidence_ids))
        if not self.approvals_ready(preregistration_id, evidence_snapshot_id)["ready"]:
            raise PermissionError("current plan and evidence approvals are required")
        comparable = (
            "id", "preregistration_id", "evidence_snapshot_id", "stage", "world_id",
            "institution_profile_version_id", "model_spec_id", "replicate_index",
            "execution_order", "case_input_sha256", "case_input_json",
        )
        proposed = [{key: cell[key] for key in comparable} for cell in cells]
        plan_sha = content_sha256(proposed)
        with self._connect() as db:
            existing = db.execute(
                "SELECT id,preregistration_id,evidence_snapshot_id,stage,world_id,"
                "institution_profile_version_id,model_spec_id,replicate_index,"
                "execution_order,case_input_sha256,case_input_json "
                "FROM experiment_run_cells WHERE preregistration_id=? "
                "AND stage='feedback' ORDER BY execution_order",
                (preregistration_id,),
            ).fetchall()
            if existing:
                values = [{key: row[key] for key in comparable} for row in existing]
                if values != proposed:
                    raise ImmutableConflict("a different feedback plan already exists")
                return self._feedback_cell_summary(
                    db, preregistration_id, evidence_snapshot_id, plan_sha
                )
            for cell in cells:
                if cell["stage"] != "feedback" or not cell["world_id"]:
                    raise ValueError("every feedback cell requires an immutable world")
                case = json.loads(cell["case_input_json"])
                if canonical_json(case) != cell["case_input_json"]:
                    raise ValueError("feedback case input is not canonical JSON")
                if content_sha256(case) != cell["case_input_sha256"]:
                    raise ValueError("feedback case input hash does not match")
                db.execute(
                    "INSERT INTO experiment_run_cells "
                    "(id,preregistration_id,evidence_snapshot_id,stage,world_id,"
                    "institution_profile_version_id,model_spec_id,replicate_index,"
                    "execution_order,case_input_sha256,case_input_json,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        cell["id"], cell["preregistration_id"],
                        cell["evidence_snapshot_id"], cell["stage"], cell["world_id"],
                        cell["institution_profile_version_id"], cell["model_spec_id"],
                        cell["replicate_index"], cell["execution_order"],
                        cell["case_input_sha256"], cell["case_input_json"], _now(),
                    ),
                )
            self._append_audit(
                db,
                actor=actor.strip(),
                event_type="feedback_run_plan_materialized",
                object_type="preregistration",
                object_id=preregistration_id,
                payload={
                    "evidence_snapshot_id": evidence_snapshot_id,
                    "cell_count": len(cells),
                    "world_count": len(world_ids),
                    "feedback_plan_sha256": plan_sha,
                },
            )
            return self._feedback_cell_summary(
                db, preregistration_id, evidence_snapshot_id, plan_sha
            )

    @staticmethod
    def _feedback_cell_summary(db: sqlite3.Connection, preregistration_id: str,
                               evidence_snapshot_id: str,
                               plan_sha256: str | None = None) -> dict:
        rows = db.execute(
            "SELECT cell.id,cell.preregistration_id,cell.evidence_snapshot_id,"
            "cell.stage,cell.world_id,cell.institution_profile_version_id,"
            "profile.institution_id,cell.model_spec_id,cell.replicate_index,"
            "cell.execution_order,cell.case_input_sha256,cell.case_input_json,"
            "(SELECT count(*) FROM experiment_run_attempts AS attempt "
            " WHERE attempt.run_cell_id=cell.id) AS attempt_count,"
            "(SELECT count(*) FROM experiment_run_attempts AS accepted "
            " WHERE accepted.run_cell_id=cell.id AND accepted.outcome='accepted') "
            "AS accepted_count FROM experiment_run_cells AS cell "
            "JOIN experiment_profile_versions AS profile "
            "ON profile.id=cell.institution_profile_version_id "
            "WHERE cell.preregistration_id=? AND cell.stage='feedback' "
            "ORDER BY cell.execution_order",
            (preregistration_id,),
        ).fetchall()
        if plan_sha256 is None and rows:
            plan_sha256 = content_sha256([{
                key: row[key] for key in (
                    "id", "preregistration_id", "evidence_snapshot_id", "stage",
                    "world_id", "institution_profile_version_id", "model_spec_id",
                    "replicate_index", "execution_order", "case_input_sha256",
                    "case_input_json",
                )
            } for row in rows])
        return {
            "preregistration_id": preregistration_id,
            "evidence_snapshot_id": evidence_snapshot_id,
            "stage": "feedback",
            "cell_count": len(rows),
            "world_count": len({row["world_id"] for row in rows}),
            "attempted_cell_count": sum(row["attempt_count"] > 0 for row in rows),
            "accepted_cell_count": sum(row["accepted_count"] > 0 for row in rows),
            "remaining_cell_count": sum(row["accepted_count"] == 0 for row in rows),
            "feedback_plan_sha256": plan_sha256,
            "cells": [{
                **{key: row[key] for key in (
                    "id", "world_id", "institution_profile_version_id",
                    "institution_id", "model_spec_id", "replicate_index",
                    "execution_order", "case_input_sha256", "attempt_count",
                )},
                "accepted": bool(row["accepted_count"]),
            } for row in rows],
        }

    def next_pending_feedback_cell(self, preregistration_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT cell.id,cell.preregistration_id,cell.evidence_snapshot_id,"
                "cell.world_id,cell.institution_profile_version_id,"
                "profile.institution_id,cell.model_spec_id,cell.replicate_index,"
                "cell.execution_order,cell.case_input_sha256,cell.case_input_json "
                "FROM experiment_run_cells AS cell "
                "JOIN experiment_profile_versions AS profile "
                "ON profile.id=cell.institution_profile_version_id "
                "WHERE cell.preregistration_id=? AND cell.stage='feedback' "
                "AND NOT EXISTS (SELECT 1 FROM experiment_run_attempts AS accepted "
                "WHERE accepted.run_cell_id=cell.id AND accepted.outcome='accepted') "
                "ORDER BY cell.execution_order LIMIT 1",
                (preregistration_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_feedback_plan(self, preregistration_id: str) -> dict | None:
        with self._connect() as db:
            evidence = db.execute(
                "SELECT evidence_snapshot_id FROM experiment_run_cells "
                "WHERE preregistration_id=? AND stage='feedback' LIMIT 1",
                (preregistration_id,),
            ).fetchone()
            return (
                self._feedback_cell_summary(
                    db, preregistration_id, evidence["evidence_snapshot_id"]
                ) if evidence else None
            )

    def begin_feedback_execution(self, preregistration_id: str, *, actor: str,
                                 confirmed_plan_sha256: str,
                                 max_cells: int) -> dict:
        if not actor.strip():
            raise ValueError("feedback execution actor is required")
        with self._connect() as db:
            evidence = db.execute(
                "SELECT evidence_snapshot_id FROM experiment_run_cells "
                "WHERE preregistration_id=? AND stage='feedback' LIMIT 1",
                (preregistration_id,),
            ).fetchone()
            if evidence is None:
                raise KeyError(preregistration_id)
            summary = self._feedback_cell_summary(
                db, preregistration_id, evidence["evidence_snapshot_id"]
            )
            if summary["feedback_plan_sha256"] != confirmed_plan_sha256:
                raise ValueError("confirmed feedback plan hash does not match")
            if not self.approvals_ready(
                preregistration_id, evidence["evidence_snapshot_id"]
            )["ready"]:
                raise PermissionError("current plan and evidence approvals are required")
            self._append_audit(
                db,
                actor=actor.strip(),
                event_type="feedback_execution_authorized",
                object_type="preregistration",
                object_id=preregistration_id,
                payload={
                    "confirmed_plan_sha256": confirmed_plan_sha256,
                    "max_cells": max_cells,
                    "remaining_cells": summary["remaining_cell_count"],
                },
            )
        return summary

    def finish_feedback_execution(self, preregistration_id: str, *, actor: str,
                                  processed_cells: int,
                                  error: str | None = None) -> None:
        with self._connect() as db:
            self._append_audit(
                db,
                actor=actor.strip(),
                event_type=(
                    "feedback_execution_failed" if error
                    else "feedback_execution_batch_finished"
                ),
                object_type="preregistration",
                object_id=preregistration_id,
                payload={"processed_cells": processed_cells, "error": error},
            )

    def accepted_feedback_decisions(self, preregistration_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT cell.id AS run_cell_id,cell.world_id,"
                "cell.evidence_snapshot_id,cell.institution_profile_version_id,"
                "profile.institution_id,cell.model_spec_id,cell.replicate_index,"
                "cell.execution_order,decision.content_json,decision.content_sha256 "
                "FROM experiment_run_cells AS cell "
                "JOIN experiment_profile_versions AS profile "
                "ON profile.id=cell.institution_profile_version_id "
                "JOIN experiment_run_attempts AS attempt ON attempt.run_cell_id=cell.id "
                "AND attempt.outcome='accepted' "
                "JOIN experiment_decisions AS decision ON decision.run_attempt_id=attempt.id "
                "WHERE cell.preregistration_id=? AND cell.stage='feedback' "
                "ORDER BY cell.execution_order",
                (preregistration_id,),
            ).fetchall()
        return [{
            **{key: row[key] for key in (
                "run_cell_id", "world_id", "evidence_snapshot_id",
                "institution_profile_version_id", "institution_id", "model_spec_id",
                "replicate_index", "execution_order", "content_sha256",
            )},
            "decision": json.loads(row["content_json"]),
        } for row in rows]

    def second_transition_input(self, world_id: str) -> dict | None:
        with self._connect() as db:
            world = db.execute(
                "SELECT id,preregistration_id,evidence_snapshot_id,assignment_kind,"
                "assignment_key,replicate_index,assignment_sha256,"
                "stage1_action_set_sha256 FROM experiment_worlds WHERE id=?",
                (world_id,),
            ).fetchone()
            if world is None:
                return None
            prior = db.execute(
                "SELECT id FROM experiment_world_transitions "
                "WHERE world_id=? AND round_index=1",
                (world_id,),
            ).fetchone()
            if prior is None:
                return None
            common = db.execute(
                "SELECT content_json FROM experiment_world_snapshots_v5 "
                "WHERE transition_id=? AND snapshot_kind='common'",
                (prior["id"],),
            ).fetchone()
            private = db.execute(
                "SELECT content_json FROM experiment_world_snapshots_v5 "
                "WHERE transition_id=? AND snapshot_kind='private' "
                "ORDER BY institution_profile_version_id",
                (prior["id"],),
            ).fetchall()
            decisions = db.execute(
                "SELECT member.ordinal,cell.id AS run_cell_id,cell.world_id,"
                "cell.institution_profile_version_id,profile.institution_id,"
                "profile.content_json AS profile_json,cell.model_spec_id,"
                "cell.replicate_index,decision.content_sha256,decision.content_json "
                "FROM experiment_world_members AS member "
                "JOIN experiment_run_cells AS cell ON cell.world_id=member.world_id "
                "AND cell.institution_profile_version_id="
                "member.institution_profile_version_id AND cell.stage='feedback' "
                "JOIN experiment_profile_versions AS profile "
                "ON profile.id=cell.institution_profile_version_id "
                "JOIN experiment_run_attempts AS attempt ON attempt.run_cell_id=cell.id "
                "AND attempt.outcome='accepted' "
                "JOIN experiment_decisions AS decision "
                "ON decision.run_attempt_id=attempt.id "
                "WHERE member.world_id=? ORDER BY member.ordinal",
                (world_id,),
            ).fetchall()
        return {
            "world": dict(world),
            "previous_common_snapshot": json.loads(common["content_json"]),
            "previous_private_snapshots": [
                json.loads(row["content_json"]) for row in private
            ],
            "feedback_decisions": [{
                **{key: row[key] for key in (
                    "run_cell_id", "world_id", "institution_profile_version_id",
                    "institution_id", "model_spec_id", "replicate_index",
                    "content_sha256",
                )},
                "decision": json.loads(row["content_json"]),
                "profile": json.loads(row["profile_json"]),
            } for row in decisions],
        }

    def worlds_pending_second_transition(self, preregistration_id: str) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT world.id FROM experiment_worlds AS world "
                "WHERE world.preregistration_id=? "
                "AND (SELECT count(*) FROM experiment_run_cells AS cell "
                " JOIN experiment_run_attempts AS attempt ON attempt.run_cell_id=cell.id "
                " AND attempt.outcome='accepted' WHERE cell.world_id=world.id "
                " AND cell.stage='feedback')=7 "
                "AND NOT EXISTS (SELECT 1 FROM experiment_world_transitions AS t "
                " WHERE t.world_id=world.id AND t.round_index=2) "
                "ORDER BY world.assignment_kind,world.assignment_key,"
                "world.replicate_index",
                (preregistration_id,),
            ).fetchall()
        return [row["id"] for row in rows]

    def world_trajectory_inputs(self, preregistration_id: str) -> list[dict]:
        with self._connect() as db:
            worlds = db.execute(
                "SELECT id,assignment_kind,assignment_key,replicate_index,"
                "stage1_action_set_sha256 FROM experiment_worlds "
                "WHERE preregistration_id=? ORDER BY assignment_kind,assignment_key,"
                "replicate_index",
                (preregistration_id,),
            ).fetchall()
            output = []
            for world in worlds:
                common = db.execute(
                    "SELECT snapshot.round_index,snapshot.content_sha256,"
                    "snapshot.content_json FROM experiment_world_snapshots_v5 AS snapshot "
                    "WHERE snapshot.world_id=? AND snapshot.snapshot_kind='common' "
                    "ORDER BY snapshot.round_index",
                    (world["id"],),
                ).fetchall()
                initial = db.execute(
                    "SELECT member.ordinal,decision.content_sha256,decision.content_json "
                    "FROM experiment_world_members AS member "
                    "JOIN experiment_run_attempts AS attempt "
                    "ON attempt.run_cell_id=member.initial_run_cell_id "
                    "AND attempt.outcome='accepted' "
                    "JOIN experiment_decisions AS decision "
                    "ON decision.run_attempt_id=attempt.id "
                    "WHERE member.world_id=? ORDER BY member.ordinal",
                    (world["id"],),
                ).fetchall()
                feedback = db.execute(
                    "SELECT member.ordinal,decision.content_sha256,decision.content_json "
                    "FROM experiment_world_members AS member "
                    "JOIN experiment_run_cells AS cell ON cell.world_id=member.world_id "
                    "AND cell.institution_profile_version_id="
                    "member.institution_profile_version_id AND cell.stage='feedback' "
                    "JOIN experiment_run_attempts AS attempt ON attempt.run_cell_id=cell.id "
                    "AND attempt.outcome='accepted' "
                    "JOIN experiment_decisions AS decision "
                    "ON decision.run_attempt_id=attempt.id "
                    "WHERE member.world_id=? ORDER BY member.ordinal",
                    (world["id"],),
                ).fetchall()
                if len(common) != 2 or len(initial) != 7 or len(feedback) != 7:
                    continue
                output.append({
                    "world_id": world["id"],
                    "assignment_kind": world["assignment_kind"],
                    "assignment_key": world["assignment_key"],
                    "replicate_index": world["replicate_index"],
                    "stage1_action_set_sha256": world["stage1_action_set_sha256"],
                    "common_snapshots": [{
                        "round_index": row["round_index"],
                        "sha256": row["content_sha256"],
                        "content": json.loads(row["content_json"]),
                    } for row in common],
                    "initial_decisions": [{
                        "sha256": row["content_sha256"],
                        "decision": json.loads(row["content_json"]),
                    } for row in initial],
                    "feedback_decisions": [{
                        "sha256": row["content_sha256"],
                        "decision": json.loads(row["content_json"]),
                    } for row in feedback],
                })
        return output

    def finish_initial_execution(self, preregistration_id: str, *, actor: str,
                                 processed_cells: int,
                                 error: str | None = None) -> None:
        with self._connect() as db:
            self._append_audit(
                db,
                actor=actor.strip(),
                event_type=(
                    "initial_execution_failed" if error
                    else "initial_execution_batch_finished"
                ),
                object_type="preregistration",
                object_id=preregistration_id,
                payload={
                    "processed_cells": processed_cells,
                    "error": error,
                },
            )

    def register_metric_result(self, result: dict, *, actor: str,
                               permutations: int,
                               bootstrap_samples: int) -> dict:
        if not actor.strip():
            raise ValueError("metric computation actor is required")
        payload = canonical_json(result)
        digest = content_sha256(result)
        result_id = "METRIC-" + hashlib.sha256(
            "|".join((
                result["preregistration_id"],
                result["metric_version"],
                result["accepted_decision_set_sha256"],
                str(permutations),
                str(bootstrap_samples),
            )).encode("utf-8")
        ).hexdigest()[:20].upper()
        with self._connect() as db:
            existing = db.execute(
                "SELECT content_sha256,content_json,created_at,actor "
                "FROM experiment_metric_results WHERE id=?",
                (result_id,),
            ).fetchone()
            if existing:
                if existing["content_sha256"] != digest:
                    raise ImmutableConflict(
                        f"metric result {result_id} already has different content"
                    )
                return {
                    "id": result_id,
                    "sha256": digest,
                    "created_at": existing["created_at"],
                    "actor": existing["actor"],
                    "content": json.loads(existing["content_json"]),
                }
            created_at = _now()
            db.execute(
                "INSERT INTO experiment_metric_results "
                "(id,preregistration_id,metric_version,accepted_decision_set_sha256,"
                "permutations,bootstrap_samples,created_at,actor,content_sha256,"
                "content_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    result_id,
                    result["preregistration_id"],
                    result["metric_version"],
                    result["accepted_decision_set_sha256"],
                    permutations,
                    bootstrap_samples,
                    created_at,
                    actor.strip(),
                    digest,
                    payload,
                ),
            )
            self._append_audit(
                db,
                actor=actor.strip(),
                event_type="initial_metrics_computed",
                object_type="metric_result",
                object_id=result_id,
                payload={
                    "content_sha256": digest,
                    "accepted_decision_set_sha256": result[
                        "accepted_decision_set_sha256"
                    ],
                    "permutations": permutations,
                    "bootstrap_samples": bootstrap_samples,
                },
            )
        return {
            "id": result_id,
            "sha256": digest,
            "created_at": created_at,
            "actor": actor.strip(),
            "content": result,
        }

    def get_profile(self, profile_version_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id,institution_id,catalog_version,created_at,content_sha256,"
                "content_json,holdings_status FROM experiment_profile_versions WHERE id=?",
                (profile_version_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            **dict(row),
            "sha256": row["content_sha256"],
            "content": json.loads(row["content_json"]),
        }

    def list_profiles(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id FROM experiment_profile_versions "
                "ORDER BY institution_id,catalog_version,created_at"
            ).fetchall()
        return [self.get_profile(row["id"]) for row in rows]

    def get_evidence(self, snapshot_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id,event_id,decision_cutoff,created_at,created_by,"
                "content_sha256,content_json FROM experiment_evidence_snapshots "
                "WHERE id=?",
                (snapshot_id,),
            ).fetchone()
            if row is None:
                return None
            approvals = db.execute(
                "SELECT id,approved_at,actor,approved,note,approved_sha256 "
                "FROM experiment_evidence_approvals WHERE snapshot_id=? "
                "ORDER BY approved_at,id",
                (snapshot_id,),
            ).fetchall()
        return {
            "id": row["id"],
            "event_id": row["event_id"],
            "decision_cutoff": row["decision_cutoff"],
            "created_at": row["created_at"],
            "created_by": row["created_by"],
            "sha256": row["content_sha256"],
            "content": json.loads(row["content_json"]),
            "approvals": [
                {**dict(item), "approved": bool(item["approved"])}
                for item in approvals
            ],
        }

    def audit_events(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT seq,at,actor,event_type,object_type,object_id,payload_sha256,"
                "payload_json,previous_event_sha256,event_sha256 "
                "FROM experiment_audit_events ORDER BY seq"
            ).fetchall()
        return [
            {**dict(row), "payload": json.loads(row["payload_json"])}
            for row in rows
        ]

    def verify_audit_chain(self) -> bool:
        previous = None
        for event in self.audit_events():
            payload = event["payload"]
            if content_sha256(payload) != event["payload_sha256"]:
                return False
            expected = content_sha256({
                "at": event["at"],
                "actor": event["actor"],
                "event_type": event["event_type"],
                "object_type": event["object_type"],
                "object_id": event["object_id"],
                "payload_sha256": event["payload_sha256"],
                "previous_event_sha256": previous,
            })
            if event["previous_event_sha256"] != previous:
                return False
            if event["event_sha256"] != expected:
                return False
            previous = event["event_sha256"]
        return True

    def get_preregistration(self, preregistration_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id,schema_version,created_at,content_sha256,content_json "
                "FROM experiment_preregistrations WHERE id=?",
                (preregistration_id,),
            ).fetchone()
            if row is None:
                return None
            approvals = db.execute(
                "SELECT id,approved_at,actor,approved,note,approved_sha256 "
                "FROM experiment_preregistration_approvals "
                "WHERE preregistration_id=? ORDER BY approved_at,id",
                (preregistration_id,),
            ).fetchall()
        return {
            "id": row["id"],
            "schema_version": row["schema_version"],
            "created_at": row["created_at"],
            "sha256": row["content_sha256"],
            "content": json.loads(row["content_json"]),
            "approvals": [
                {**dict(item), "approved": bool(item["approved"])}
                for item in approvals
            ],
        }
