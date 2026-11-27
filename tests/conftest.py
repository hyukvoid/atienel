import base64
import json
import os
import sys

import pytest
from phase2_verify import kube


class Tokens(dict):
    def __repr__(self):
        return "<local API credentials>"


@pytest.fixture
def cluster(monkeypatch):
    if os.environ.get("ATIENEL_E2E") != "1":
        pytest.skip("local k3d integration requires make phase5-verify")
    context = os.environ.get("ATIENEL_CONTEXT", "k3d-atienel")
    assert context.startswith("k3d-")
    monkeypatch.setattr(sys, "argv", [sys.argv[0], os.environ.get("KUBECTL", "kubectl"), context])
    mode = kube(
        "aiops-core",
        "get",
        "deployment/aiops-worker",
        "-o",
        'jsonpath={.spec.template.spec.containers[0].env[?(@.name=="DIAGNOSIS_MODE")].value}',
    )
    assert mode.strip() == "verification"
    data = json.loads(kube("aiops-core", "get", "secret", "api-tokens", "-o", "json"))["data"]
    return Tokens({name: base64.b64decode(encoded).decode() for name, encoded in data.items()})
