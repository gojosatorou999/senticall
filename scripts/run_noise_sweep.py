"""Noise sweep CLI — spec §9.5 exit gate artifact.

Usage:

    python -m scripts.run_noise_sweep \\
        --manifest data/eval/noise/manifest.json \\
        --denoise rnnoise \\
        --stt mock \\
        --out data/eval/noise/reports/

The script emits a JSON report and prints a per-cell table + the
exit-gate verdict. Exit code 0 if the P3 exit-gate targets are met,
1 otherwise (CI can use the exit code to fail the branch).

STT backends:

- `mock` — passes the reference transcript verbatim. Isolates the
  denoise + extractor contribution from any real STT error.
- `thresholded` — returns empty transcript below a 3 dB SNR floor.
  Sanity for the harness aggregation curve.
- `deepgram` — NOT WIRED in-CLI. Requires a live key; add via a
  follow-up commit that pulls `pipeline/stt_flux_ws.py` in.

Denoise providers mirror `Settings.denoise_provider`:
- `none` — passthrough baseline.
- `rnnoise` — the ships-today spectral-gate fallback.
- `krisp` — falls back to NoOp with warning if the SDK isn't wired.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tests.noise.harness import (
    CorpusManifest,
    check_exit_gate,
    make_stt_backend,
    run_sweep,
    write_report,
)

from fg_voice.audio.denoise import Denoiser, build_denoiser


def _print_table(cells) -> None:  # type: ignore[no-untyped-def]
    header = f"{'noise':<12}{'snr_db':>8}{'n':>6}{'wer':>8}{'slot_acc':>10}{'turns':>8}{'false_bi':>10}{'cutoff':>10}"
    print(header)
    print("-" * len(header))
    for c in cells:
        d = c.to_dict()
        print(
            f"{d['noise_type']:<12}"
            f"{d['snr_db']:>8.1f}"
            f"{d['n']:>6d}"
            f"{d['wer_mean']:>8.3f}"
            f"{d['slot_accuracy']:>10.3f}"
            f"{d['turns_to_completion_mean']:>8.2f}"
            f"{d['false_barge_in_rate']:>10.3f}"
            f"{d['premature_cutoff_rate']:>10.3f}"
        )


def _parse_snrs(raw: str) -> tuple[float, ...]:
    return tuple(float(v.strip()) for v in raw.split(",") if v.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sentinel voice-agent noise sweep")
    parser.add_argument("--manifest", required=True, type=Path, help="corpus manifest JSON")
    parser.add_argument(
        "--denoise",
        default="rnnoise",
        choices=["none", "rnnoise", "krisp"],
        help="Denoise provider (mirrors Settings.denoise_provider)",
    )
    parser.add_argument(
        "--stt",
        default="mock",
        choices=["mock", "thresholded"],
        help="STT backend. 'deepgram' lands in a follow-up commit.",
    )
    parser.add_argument(
        "--snrs",
        default="20,15,10,5,0",
        help="Comma-separated SNR levels in dB",
    )
    parser.add_argument("--out", type=Path, default=Path("./data/eval/noise/reports"))
    args = parser.parse_args(argv)

    if not args.manifest.exists():
        print(f"error: manifest not found: {args.manifest}", file=sys.stderr)
        return 2

    manifest = CorpusManifest.from_json(args.manifest)
    denoiser_factory: type[Denoiser] = type(build_denoiser(args.denoise))
    stt = make_stt_backend(args.stt)

    cells = run_sweep(
        manifest,
        snr_levels_db=_parse_snrs(args.snrs),
        stt=stt,
        denoiser_factory=denoiser_factory,
    )

    _print_table(cells)
    report = write_report(cells, args.out, denoise_provider=args.denoise, stt_backend_name=args.stt)
    print(f"\nreport written: {report}")

    passed, failures = check_exit_gate(cells)
    if passed:
        print("\n✓ P3 exit-gate targets MET.")
        return 0
    print("\n✗ P3 exit-gate targets NOT met:")
    for f in failures:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
