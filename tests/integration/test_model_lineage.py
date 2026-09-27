"""ADR-019: a converted fine-tune is registered with lineage and gated into sweeps."""

from __future__ import annotations

import hashlib

import pytest
from fake_crispasr import fake_catalog
from pydantic import ValidationError

from aerochorus.sweep_contracts import (
    CatalogModel,
    CatalogSync,
    ModelUpdate,
    QualificationGate,
    QualificationRecord,
)
from aerochorus.worker.client import ApiError

LINEAGE = {
    "base_model": "alpha-model",
    "fine_tune_dataset": "bwi-atc v3",
    "fine_tune_dataset_manifest_sha256": "a" * 64,
    "training_run": "runs/2026-10-02-alpha-ft",
    "native_checkpoint_uri": "file:///lab/checkpoints/alpha-ft.nemo",
    "native_checkpoint_sha256": "b" * 64,
    "converter": "CrispASR models/convert-alpha-to-gguf.py",
    "converter_version": "0.8.37@abc123",
    "crispasr_version": "0.8.37",
}


def custom_model(tmp_path, **pedigree) -> CatalogModel:
    blob = b"converted fine-tuned gguf"
    (tmp_path / "alpha-ft.gguf").write_bytes(blob)
    return CatalogModel(
        logical_name="alpha-ft-bwi-v1",
        architecture_family="family-alpha",
        crisp_backend="alpha",
        upstream_model="lab/alpha-ft-bwi",
        artifact_url="http://lab.local/models/alpha-ft.gguf",
        upstream_revision="local",
        model_filename="alpha-ft.gguf",
        model_sha256=hashlib.sha256(blob).hexdigest(),
        artifact_size_bytes=len(blob),
        quantization="q8_0",
        pedigree=pedigree,
    )


def test_lineage_is_validated(tmp_path):
    with pytest.raises(ValidationError):
        custom_model(tmp_path, lineage=LINEAGE | {"native_checkpoint_sha256": "nope"})
    with pytest.raises(ValidationError):
        CatalogModel.model_validate(
            custom_model(tmp_path).model_dump() | {"artifact_url": None, "artifact_repo": None}
        )


def test_fine_tune_needs_every_gate_before_sweeps(api, tmp_path):
    base = fake_catalog(tmp_path / "models")
    model = custom_model(tmp_path, lineage=LINEAGE, contamination="none (BWI corpus only)")
    api.sync_models(CatalogSync(families=base.families, models=[*base.models, model]))
    stored = api.get_model("alpha-ft-bwi-v1")
    assert stored.artifact_uri == "http://lab.local/models/alpha-ft.gguf"
    assert stored.pedigree["lineage"]["fine_tune_dataset"] == "bwi-atc v3"
    # Same family as its base model: agreement never counts them as independent (ADR-009).
    assert stored.architecture_family == "family-alpha"

    with pytest.raises(ApiError) as excinfo:
        api.update_model("alpha-ft-bwi-v1", ModelUpdate(sweep_eligible=True))
    assert excinfo.value.status_code == 409 and "conversion" in str(excinfo.value.detail)

    gates = list(QualificationGate)
    for gate in gates[:-1]:
        api.record_qualification(
            "alpha-ft-bwi-v1", QualificationRecord(gate=gate, passed=True, evidence={"ok": 1})
        )
    with pytest.raises(ApiError):
        api.update_model("alpha-ft-bwi-v1", ModelUpdate(sweep_eligible=True))
    api.record_qualification("alpha-ft-bwi-v1", QualificationRecord(gate=gates[-1], passed=True))
    assert api.update_model("alpha-ft-bwi-v1", ModelUpdate(sweep_eligible=True)).sweep_eligible

    # A later regression failure revokes eligibility.
    revoked = api.record_qualification(
        "alpha-ft-bwi-v1",
        QualificationRecord(gate="regression", passed=False, evidence={"ter": 0.61}),
    )
    assert revoked.sweep_eligible is False
    assert revoked.qualification["regression"]["passed"] is False

    # Upstream models without lineage keep the existing rule.
    assert api.update_model("alpha-model", ModelUpdate(sweep_eligible=True)).sweep_eligible
