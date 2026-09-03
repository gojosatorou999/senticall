# fg-voice Terraform

Populated in **P7**. The layout follows spec §14:

```
infra/terraform/
├── network.tf         # VPC, subnets, NAT, IGW, route tables
├── ecs.tf             # Fargate services: fg-voice-agent, -api, -csv-projector, -flows
├── alb.tf             # ALB with WSS support, idle_timeout = 900
├── rds.tf             # PG16 Multi-AZ, PostGIS + pgvector, PITR 7 d
├── redis.tf           # ElastiCache 7, Multi-AZ
├── s3.tf              # recordings, transcripts, reports, rag artifacts
├── secrets.tf         # Twilio / Deepgram / TTS / LLM keys, rotation on
├── iam.tf             # per-service task roles, no wildcards
├── observability.tf   # CloudWatch, OTel collector, Grafana workspace
├── waf.tf             # rate-limit + Twilio allowlist on /voice/*
├── autoscaling.tf     # target-tracking on fg_voice_concurrent_calls_per_task
└── envs/
    ├── dev.tfvars
    ├── staging.tfvars
    └── prod.tfvars
```

Backend: S3 + DynamoDB lock (bootstrap manually once, then commit
`backend.tf`). CI uses OIDC to assume a per-env deploy role — no
long-lived AWS keys.
