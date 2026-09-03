"""Surge-mode toggle (spec §14.4).

Cyclone landfall traffic can spike 50-100x baseline within minutes;
target-tracking autoscaling reacts in tens of seconds, which is too
slow. This script bumps the ECS service's `desiredCount` +
`minCapacity` to a pre-computed surge floor, triggerable from three
paths per spec:

1. **IMD/INCOIS warning** — the main Sentinel alerting pipeline
   invokes this script when a red-alert cyclone bulletin lands
2. **Manual ops toggle** — on-call runs `surge_mode.py on` when they
   see a spike and want to pre-warm capacity
3. **Automatic** — a CloudWatch alarm on
   `fg_voice_concurrent_calls_per_task > 0.70 * ceiling` fires a
   Lambda that shells out to this script

Effects when `on`:
- Bumps `min_capacity` on the target-tracking scaling policy from
  the normal baseline (2 tasks) to a pre-computed surge floor
  (default 10 tasks — set in `SURGE_MIN_TASKS`)
- Sets an SSM parameter `/fg-voice/surge_mode = "true"` that the
  running voice-agent processes read and use to switch the
  overflow behaviour: when all workers are saturated, `<Say>` a
  pre-recorded overflow message with an SMS link to the web form
  rather than dropping the call
- Emits a CloudWatch custom metric so the surge is visible on
  dashboards and post-mortems can correlate events

Effects when `off`:
- Restores `min_capacity` to `NORMAL_MIN_TASKS`
- Deletes the SSM parameter (agent processes revert on next check)

Not idempotent-guarded here — the CLI is a one-shot; the underlying
AWS APIs are idempotent (setting `MinCapacity=10` twice is a no-op).

Dry-run: default. Pass `--apply` to actually mutate AWS state.
Callers that shell out from CloudWatch Lambda pass `--apply
--from=cloudwatch`; the `from` label is stamped on the CloudWatch
custom metric so a post-mortem can attribute the trigger.

Requires `boto3`. Kept as a script (not a runtime module) because
it's ops-facing; the runtime side reads the SSM parameter via
`aioboto3` in `main.py` when `SURGE_ENABLED=true` (P8 wire-in).
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Literal

_SSM_PARAM_NAME = "/fg-voice/surge_mode"
_METRIC_NAMESPACE = "SentinelVoice"
_METRIC_NAME = "surge_mode_toggle"

NORMAL_MIN_TASKS = 2
SURGE_MIN_TASKS_DEFAULT = 10


def _boto_clients(region: str):  # type: ignore[no-untyped-def]
    """Lazy imports so `--help` works without boto installed."""
    import boto3

    session = boto3.session.Session(region_name=region)
    return {
        "ecs": session.client("ecs"),
        "aas": session.client("application-autoscaling"),
        "ssm": session.client("ssm"),
        "cw": session.client("cloudwatch"),
    }


def _toggle(
    mode: Literal["on", "off"],
    *,
    cluster: str,
    service: str,
    surge_min_tasks: int,
    region: str,
    trigger_source: str,
    apply: bool,
) -> int:
    """Body of the toggle. Prints what it would/did do; returns
    process exit code."""
    normal_str = f"min_capacity={NORMAL_MIN_TASKS}"
    surge_str = f"min_capacity={surge_min_tasks}"
    target_str = surge_str if mode == "on" else normal_str
    ssm_value = "true" if mode == "on" else None

    print(f"surge_mode: mode={mode} cluster={cluster} service={service} target={target_str}")
    print(f"           ssm_param={_SSM_PARAM_NAME}={ssm_value} region={region}")

    if not apply:
        print("surge_mode: dry-run — no AWS calls made. Pass --apply to commit.")
        return 0

    clients = _boto_clients(region)

    # 1. Autoscaling min-capacity.
    resource_id = f"service/{cluster}/{service}"
    min_capacity = surge_min_tasks if mode == "on" else NORMAL_MIN_TASKS
    clients["aas"].register_scalable_target(
        ServiceNamespace="ecs",
        ResourceId=resource_id,
        ScalableDimension="ecs:service:DesiredCount",
        MinCapacity=min_capacity,
    )
    print(f"surge_mode: set MinCapacity={min_capacity} on {resource_id}")

    # 2. SSM parameter — runtime processes read this.
    if mode == "on":
        clients["ssm"].put_parameter(
            Name=_SSM_PARAM_NAME,
            Value="true",
            Type="String",
            Overwrite=True,
        )
        print(f"surge_mode: set SSM {_SSM_PARAM_NAME}=true")
    else:
        try:
            clients["ssm"].delete_parameter(Name=_SSM_PARAM_NAME)
            print(f"surge_mode: deleted SSM {_SSM_PARAM_NAME}")
        except clients["ssm"].exceptions.ParameterNotFound:
            print(f"surge_mode: SSM {_SSM_PARAM_NAME} already absent")

    # 3. CloudWatch metric — attribution for post-mortems.
    clients["cw"].put_metric_data(
        Namespace=_METRIC_NAMESPACE,
        MetricData=[
            {
                "MetricName": _METRIC_NAME,
                "Value": 1.0 if mode == "on" else 0.0,
                "Unit": "None",
                "Dimensions": [
                    {"Name": "cluster", "Value": cluster},
                    {"Name": "service", "Value": service},
                    {"Name": "trigger_source", "Value": trigger_source},
                ],
            }
        ],
    )
    print(f"surge_mode: emitted CloudWatch metric {_METRIC_NAMESPACE}/{_METRIC_NAME}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["on", "off"], help="Surge on or off")
    parser.add_argument(
        "--cluster",
        default=os.environ.get("FG_ECS_CLUSTER", "fg-voice"),
        help="ECS cluster name (default from FG_ECS_CLUSTER env)",
    )
    parser.add_argument(
        "--service",
        default=os.environ.get("FG_ECS_SERVICE", "fg-voice-agent"),
        help="ECS service name (default from FG_ECS_SERVICE env)",
    )
    parser.add_argument(
        "--surge-min-tasks",
        type=int,
        default=int(os.environ.get("SURGE_MIN_TASKS", SURGE_MIN_TASKS_DEFAULT)),
        help=f"Min tasks in surge mode (default {SURGE_MIN_TASKS_DEFAULT})",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION", "ap-south-1"),
    )
    parser.add_argument(
        "--from",
        dest="trigger_source",
        default="manual",
        help='Attribution label ("manual", "cloudwatch", "imd-warning")',
    )
    parser.add_argument("--apply", action="store_true", help="Commit the changes")
    args = parser.parse_args()

    return _toggle(
        args.mode,
        cluster=args.cluster,
        service=args.service,
        surge_min_tasks=args.surge_min_tasks,
        region=args.region,
        trigger_source=args.trigger_source,
        apply=args.apply,
    )


if __name__ == "__main__":
    sys.exit(main())
