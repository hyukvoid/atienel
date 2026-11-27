from uuid import uuid4

import pytest
from atienel_shared.contracts import IncidentEvent, StreamMessage
from pydantic import ValidationError
from src.agent import Diagnosis, report_from


def test_stream_rejects_coerced_version():
    with pytest.raises(ValidationError):
        StreamMessage(schema_version="1", job_id=uuid4(), incident_id=uuid4())


def test_event_hash_normalizes_timestamp_and_nullable_field():
    body = {
        "event_id": "test:normalize",
        "namespace": "target-apps",
        "pod_name": "pod",
        "pod_uid": str(uuid4()),
        "incident_type": "OOMKilled",
        "observed_at": "2026-11-27T00:00:00Z",
        "source": "pod_status",
        "summary": "Observed",
    }
    event = IncidentEvent.model_validate(body)
    assert (
        event.request_hash()
        == IncidentEvent.model_validate(
            {
                **body,
                "observed_at": "2026-11-27T09:00:00+09:00",
                "container_name": None,
            }
        ).request_hash()
    )


def test_report_rejects_fabricated_citation():
    diagnosis = Diagnosis(
        suspected_cause="OOM",
        citation_ids=[str(uuid4())],
        limitations=[],
        recommended_yaml_patch=None,
        patch_unavailable_reason="Measurements unavailable.",
    )
    with pytest.raises(ValueError, match="Unknown citation"):
        report_from({"signal": {}}, {"gaps": []}, [], diagnosis)
