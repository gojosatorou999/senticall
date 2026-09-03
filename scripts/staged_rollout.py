"""Staged rollout management (spec §P9 — "single district first").

Manages the `/fg-voice/{env}/rollout/enabled_districts` SSM parameter
that controls which Andhra Pradesh / Telangana districts can receive
voice calls from the hotline.

Rollout strategy:
  1. Start with 1 coastal district (e.g. Krishna) after 50 supervised pilot calls
  2. Expand by 2-3 districts per week, monitoring completion rate + error rate
  3. Expand state-wide once SLOs are stable for 14 consecutive days
  4. Remove the parameter (allow all) once 100% rollout is stable

Subcommands
-----------
list    Show current enabled districts and rollout coverage
enable  Add one or more districts to the rollout
disable Remove one or more districts
full    Enable all 59 TG+AP coastal districts (full rollout)
reset   Remove the SSM parameter (allow all calls — use carefully)

Usage
-----
    uv run python scripts/staged_rollout.py list
    uv run python scripts/staged_rollout.py enable Krishna --apply
    uv run python scripts/staged_rollout.py enable East_Godavari West_Godavari --apply
    uv run python scripts/staged_rollout.py full --apply
    uv run python scripts/staged_rollout.py reset --apply
"""

from __future__ import annotations

import argparse
import os
import sys

_ENV = os.environ.get("FG_ENV", "dev")
_REGION = os.environ.get("AWS_REGION", "ap-south-1")
_PARAM_NAME = f"/fg-voice/{_ENV}/rollout/enabled_districts"

# All 59 TG+AP coastal districts from the project's geographic scope.
_ALL_DISTRICTS = [
    # Andhra Pradesh (26 districts, coastal focus)
    "Srikakulam",
    "Vizianagaram",
    "Visakhapatnam",
    "Alluri_Sitharama_Raju",
    "Anakapalli",
    "Kakinada",
    "East_Godavari",
    "Konaseema",
    "West_Godavari",
    "Eluru",
    "NTR",
    "Krishna",
    "Palnadu",
    "Guntur",
    "Bapatla",
    "Prakasam",
    "SPSR_Nellore",
    "Nandyal",
    "Kurnool",
    "Anantapur",
    "Sri_Sathya_Sai",
    "YSR_Kadapa",
    "Annamayya",
    "Tirupati",
    "Chittoor",
    "Nellore",
    # Telangana (33 districts, all included for completeness)
    "Adilabad",
    "Bhadradri_Kothagudem",
    "Hanumakonda",
    "Hyderabad",
    "Jagtial",
    "Jangaon",
    "Jayashankar_Bhupalapally",
    "Jogulamba_Gadwal",
    "Kamareddy",
    "Karimnagar",
    "Khammam",
    "Komaram_Bheem_Asifabad",
    "Mahabubabad",
    "Mahabubnagar",
    "Mancherial",
    "Medak",
    "Medchal_Malkajgiri",
    "Mulugu",
    "Nagarkurnool",
    "Nalgonda",
    "Narayanpet",
    "Nirmal",
    "Nizamabad",
    "Peddapalli",
    "Rajanna_Sircilla",
    "Rangareddy",
    "Sangareddy",
    "Siddipet",
    "Suryapet",
    "Vikarabad",
    "Wanaparthy",
    "Warangal",
    "Yadadri_Bhuvanagiri",
]


def _boto_ssm(region: str):  # type: ignore[no-untyped-def]
    import boto3

    return boto3.client("ssm", region_name=region)


def _get_current(ssm) -> frozenset[str]:  # type: ignore[no-untyped-def]
    try:
        resp = ssm.get_parameter(Name=_PARAM_NAME)
        value = resp["Parameter"]["Value"].strip()
        return frozenset(d.strip() for d in value.split(",") if d.strip())
    except ssm.exceptions.ParameterNotFound:
        return frozenset()


def _set_districts(ssm, districts: frozenset[str], apply: bool) -> None:  # type: ignore[no-untyped-def]
    value = ",".join(sorted(districts))
    print(
        f"  {'[DRY-RUN] ' if not apply else ''}Setting {_PARAM_NAME} = {value or '(empty → all allowed)'}"
    )
    if apply:
        ssm.put_parameter(
            Name=_PARAM_NAME,
            Value=value,
            Type="String",
            Overwrite=True,
        )
        print("  ✅ SSM parameter updated")


def cmd_list(_args: argparse.Namespace) -> int:
    try:
        ssm = _boto_ssm(_REGION)
        current = _get_current(ssm)
    except Exception as exc:
        print(f"  ERROR: {exc} — check AWS credentials", file=sys.stderr)
        return 1

    total = len(_ALL_DISTRICTS)
    enabled = len(current)
    pct = enabled / total * 100 if total else 0

    print(f"\n  Staged rollout status ({_ENV})")
    print(f"  SSM param: {_PARAM_NAME}")
    if not current:
        print("  Status: ALL districts enabled (parameter absent or empty)")
    else:
        print(f"  Enabled: {enabled}/{total} districts ({pct:.0f}%)")
        print(f"  Districts: {', '.join(sorted(current))}")
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    try:
        ssm = _boto_ssm(_REGION)
        current = _get_current(ssm)
        new = current | frozenset(args.districts)
        print(f"  Adding: {set(args.districts) - current}")
        _set_districts(ssm, new, args.apply)
    except Exception as exc:
        print(f"  ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    try:
        ssm = _boto_ssm(_REGION)
        current = _get_current(ssm)
        removing = frozenset(args.districts)
        not_present = removing - current
        if not_present:
            print(f"  Warning: these districts were not enabled: {not_present}")
        new = current - removing
        print(f"  Removing: {removing & current}")
        _set_districts(ssm, new, args.apply)
    except Exception as exc:
        print(f"  ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_full(args: argparse.Namespace) -> int:
    print(f"  Enabling all {len(_ALL_DISTRICTS)} TG+AP districts")
    try:
        ssm = _boto_ssm(_REGION)
        _set_districts(ssm, frozenset(_ALL_DISTRICTS), args.apply)
    except Exception as exc:
        print(f"  ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    """Remove the SSM parameter → all districts allowed."""
    if not args.apply:
        print(f"  [DRY-RUN] Would delete {_PARAM_NAME} (allows all districts)")
        return 0
    try:
        ssm = _boto_ssm(_REGION)
        ssm.delete_parameter(Name=_PARAM_NAME)
        print(f"  ✅ Deleted {_PARAM_NAME} — all districts now allowed")
    except Exception as exc:
        print(f"  ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    global _ENV, _REGION, _PARAM_NAME
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--env", default=_ENV)
    parser.add_argument("--region", default=_REGION)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")

    p_en = sub.add_parser("enable")
    p_en.add_argument("districts", nargs="+", help="District canonical names")
    p_en.add_argument("--apply", action="store_true")

    p_dis = sub.add_parser("disable")
    p_dis.add_argument("districts", nargs="+")
    p_dis.add_argument("--apply", action="store_true")

    p_full = sub.add_parser("full")
    p_full.add_argument("--apply", action="store_true")

    p_reset = sub.add_parser("reset")
    p_reset.add_argument("--apply", action="store_true")

    args = parser.parse_args()
    _ENV = args.env
    _REGION = args.region
    _PARAM_NAME = f"/fg-voice/{_ENV}/rollout/enabled_districts"

    dispatch = {
        "list": cmd_list,
        "enable": cmd_enable,
        "disable": cmd_disable,
        "full": cmd_full,
        "reset": cmd_reset,
    }
    return dispatch[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
