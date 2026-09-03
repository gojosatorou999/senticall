# Noise Sweep Eval Corpus

This directory holds the audio corpus that the P3 exit-gate noise
sweep (`spec §9.5`) runs against.

## What ships in this repo

**Nothing but scaffolding.** The real corpus is licensed / recorded
material and lives in the ops S3 bucket, pulled at eval-time. Ship
gate:

- `data/eval/noise/manifest.json` — populated per the schema below.
- `data/eval/noise/utterances/` — mono 16-bit PCM WAV at 8 kHz.
- `data/eval/noise/noise/` — mono 16-bit PCM WAV at 8 kHz.
- `data/eval/noise/reports/` — timestamped JSON reports from every
  sweep run. Git-ignored.

## Manifest schema

```json
{
  "utterances": [
    {
      "id": "u001",
      "wav": "utterances/u001.wav",
      "transcript": "yes",
      "slot_extractor": "intent",
      "expected_slot": "yes"
    }
  ],
  "noise_samples": [
    {"type": "rain", "wav": "noise/rain_kakinada_1.wav"},
    {"type": "wind", "wav": "noise/wind_gopalpur_1.wav"},
    {"type": "traffic", "wav": "noise/traffic_visakhapatnam_1.wav"},
    {"type": "crowd", "wav": "noise/market_kakinada_1.wav"},
    {"type": "sea", "wav": "noise/waves_rk_beach_1.wav"},
    {"type": "handling", "wav": "noise/phone_handling_1.wav"}
  ]
}
```

`slot_extractor` matches the extractor registry in
`tests/noise/harness.py::_EXTRACTORS`:

- `intent` — yes/no
- `hazard_type` — storm / sludge_oil / abnormal_tide / erosion / other
- `severity` — light / moderate / extreme
- `confirmation` — yes / no / restart

## Required corpus shape (P3 exit gate)

Per spec §9.5:

- **≥20 Indian-English speakers** across AP + Telangana + Assam,
  mixed genders and ages.
- **6 noise types**: rain, wind, sea/waves, traffic, crowd/market,
  TV/radio babble, mobile handling (7 if you split TV/radio).
- **SNR sweep**: 0, 5, 10, 15, 20 dB.
- **Channel simulation** (applied by the harness after mix):
  μ-law codec round-trip, 1%/3%/5% packet loss, 30/60 ms jitter.
  Codec is on today; loss + jitter are TODO in `harness.py`.

## Running the sweep

```bash
# One-off local run against the synthetic smoke corpus:
python -m scripts.run_noise_sweep \
  --manifest data/eval/noise/manifest.json \
  --denoise rnnoise \
  --stt mock

# CI-style: exit non-zero if the P3 gate isn't met.
python -m scripts.run_noise_sweep \
  --manifest data/eval/noise/manifest.json \
  --denoise rnnoise \
  --stt mock \
  --snrs 10,5
```

The `mock` STT backend passes the reference transcript verbatim so
the sweep isolates the denoise + extractor contribution from real
STT error. Once the Deepgram Flux WS transport is wired into the CLI
(`scripts/run_noise_sweep.py::main`), a real STT run becomes:

```bash
DEEPGRAM_API_KEY=... python -m scripts.run_noise_sweep \
  --manifest data/eval/noise/manifest.json \
  --denoise rnnoise \
  --stt deepgram
```

## Ship targets

- `slot_accuracy >= 0.92` at 10 dB SNR across every noise type.
- `slot_accuracy >= 0.85` at 5 dB SNR.
- `false_barge_in_rate < 0.02`.
- `premature_cutoff_rate < 0.03`.

Anything below → the exit gate rejects and CI turns red. Anything
above → the denoiser paid for its latency and the gate passes.

## What to do if the sweep fails

1. **Slot accuracy low at 10 dB** — check the extractor. If the
   noise-type histogram shows a single culprit (e.g. `handling`),
   the fix is usually in the frontend (higher-order HPF, tighter
   noise gate) rather than the denoiser.
2. **False barge-in high** — the interrupt controller's lockout
   window is likely too short for the caller's phone speaker. Raise
   `BARGE_IN_LOCKOUT_MS` (`pipeline/interrupt.py`) in 50 ms steps
   and re-run.
3. **Premature cutoff high** — the EOT knobs on the affected node
   are too tight. Loosen the per-node overrides on `graph.py`
   (usually ASK_LOCATION or ASK_DESCRIPTION) and re-run.
4. **Denoiser regresses vs `--denoise none`** — drop the denoiser
   from the deploy. §9.1: "if it doesn't measurably help, don't pay
   for the latency."
