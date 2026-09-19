"""Opt-in acceptance against real Identity/Platform services, never default CI fixtures."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.environ.get("DEIXIC_REAL_JOURNEY") != "1",
                    reason="requires an explicitly configured dedicated test tenant")
def test_real_identity_platform_result_survives_client_process_restart(tmp_path):
    checkpoint = tmp_path / "real-journey.json"
    command = [sys.executable, "-m", "deixic.examples.verify_test_journey"]
    accepted = subprocess.run(command + ["submit", str(checkpoint)],
                              capture_output=True, text=True, timeout=60)
    assert accepted.returncode == 0, "Real Identity/Platform submission did not pass"
    acceptance = json.loads(accepted.stdout)
    assert acceptance["status"] == "accepted"
    recovered = subprocess.run(command + ["resume", str(checkpoint)],
                               capture_output=True, text=True, timeout=120)
    assert recovered.returncode == 0, "Real Identity/Platform restarted result verification did not pass"
    result = json.loads(recovered.stdout)
    assert result["status"] == "completed"
    assert result["turn_id"] == acceptance["turn_id"]
    assert result["restarted_client"] is True
    assert result["message_id"]
    assert result["identity_key_id"]
    token = os.environ["DEIXIC_API_KEY"]
    assert token not in accepted.stdout + recovered.stdout + checkpoint.read_text()
