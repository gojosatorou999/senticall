"""surge_mode.py CLI — dry-run + apply behaviours.

The script is an ops utility; tests pin:
- CLI accepts on/off and rejects bogus modes
- dry-run makes zero AWS calls
- --apply invokes autoscaling, SSM, and CloudWatch with the right
  arguments (via mocked boto clients)
- Attribution label is stamped onto the CloudWatch metric so a
  post-mortem can distinguish IMD-triggered from manual toggles

boto3 is imported lazily inside `_boto_clients`; we patch that
function to return a MagicMock dict, so tests don't need real AWS
credentials or the boto3 SDK actually resolving anything.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _fake_clients() -> dict[str, MagicMock]:
    ecs = MagicMock()
    aas = MagicMock()
    ssm = MagicMock()
    ssm.exceptions.ParameterNotFound = type("PNF", (Exception,), {})
    cw = MagicMock()
    return {"ecs": ecs, "aas": aas, "ssm": ssm, "cw": cw}


# ─── CLI parsing ───────────────────────────────────────────────────


def test_cli_rejects_bogus_mode(capsys):
    from scripts.surge_mode import main

    old_argv = sys.argv[:]
    sys.argv = ["surge_mode.py", "yeah-ok"]
    try:
        with pytest.raises(SystemExit):
            main()
    finally:
        sys.argv = old_argv
    err = capsys.readouterr().err
    assert "invalid choice" in err.lower()


# ─── Dry run ────────────────────────────────────────────────────────


def test_dry_run_makes_no_aws_calls(capsys):
    from scripts import surge_mode as sm

    fake = _fake_clients()
    with patch.object(sm, "_boto_clients", return_value=fake):
        rc = sm._toggle(
            "on",
            cluster="fg-voice",
            service="fg-voice-agent",
            surge_min_tasks=10,
            region="ap-south-1",
            trigger_source="manual",
            apply=False,
        )
    assert rc == 0
    fake["aas"].register_scalable_target.assert_not_called()
    fake["ssm"].put_parameter.assert_not_called()
    fake["cw"].put_metric_data.assert_not_called()
    out = capsys.readouterr().out
    assert "dry-run" in out


# ─── Apply on ──────────────────────────────────────────────────────


def test_apply_on_bumps_min_capacity_and_sets_ssm():
    from scripts import surge_mode as sm

    fake = _fake_clients()
    with patch.object(sm, "_boto_clients", return_value=fake):
        rc = sm._toggle(
            "on",
            cluster="fg-voice",
            service="fg-voice-agent",
            surge_min_tasks=15,
            region="ap-south-1",
            trigger_source="cloudwatch",
            apply=True,
        )
    assert rc == 0

    fake["aas"].register_scalable_target.assert_called_once()
    aas_kwargs = fake["aas"].register_scalable_target.call_args.kwargs
    assert aas_kwargs["MinCapacity"] == 15
    assert aas_kwargs["ResourceId"] == "service/fg-voice/fg-voice-agent"
    assert aas_kwargs["ScalableDimension"] == "ecs:service:DesiredCount"

    fake["ssm"].put_parameter.assert_called_once()
    ssm_kwargs = fake["ssm"].put_parameter.call_args.kwargs
    assert ssm_kwargs["Name"] == "/fg-voice/surge_mode"
    assert ssm_kwargs["Value"] == "true"
    assert ssm_kwargs["Overwrite"] is True

    fake["cw"].put_metric_data.assert_called_once()
    cw_kwargs = fake["cw"].put_metric_data.call_args.kwargs
    assert cw_kwargs["Namespace"] == "SentinelVoice"
    metric = cw_kwargs["MetricData"][0]
    assert metric["MetricName"] == "surge_mode_toggle"
    assert metric["Value"] == 1.0
    dims = {d["Name"]: d["Value"] for d in metric["Dimensions"]}
    assert dims["trigger_source"] == "cloudwatch"


# ─── Apply off ─────────────────────────────────────────────────────


def test_apply_off_restores_normal_min_and_deletes_ssm():
    from scripts import surge_mode as sm

    fake = _fake_clients()
    with patch.object(sm, "_boto_clients", return_value=fake):
        rc = sm._toggle(
            "off",
            cluster="fg-voice",
            service="fg-voice-agent",
            surge_min_tasks=15,
            region="ap-south-1",
            trigger_source="manual",
            apply=True,
        )
    assert rc == 0

    aas_kwargs = fake["aas"].register_scalable_target.call_args.kwargs
    assert aas_kwargs["MinCapacity"] == sm.NORMAL_MIN_TASKS

    fake["ssm"].delete_parameter.assert_called_once_with(Name="/fg-voice/surge_mode")

    metric = fake["cw"].put_metric_data.call_args.kwargs["MetricData"][0]
    assert metric["Value"] == 0.0


def test_apply_off_tolerates_already_absent_ssm():
    from scripts import surge_mode as sm

    fake = _fake_clients()
    fake["ssm"].delete_parameter.side_effect = fake["ssm"].exceptions.ParameterNotFound
    with patch.object(sm, "_boto_clients", return_value=fake):
        rc = sm._toggle(
            "off",
            cluster="fg-voice",
            service="fg-voice-agent",
            surge_min_tasks=15,
            region="ap-south-1",
            trigger_source="manual",
            apply=True,
        )
    assert rc == 0  # PNF is swallowed; process exits clean
