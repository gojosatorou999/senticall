environment = "dev"

# Dev uses localstack + docker-compose; Terraform is used for staging/prod only.
# This file is a placeholder so `make deploy ENV=dev` works without missing-file
# errors. Actual dev deploys use `make dev` (docker-compose).

domain_name         = "voice-dev.sentinel.in"
acm_certificate_arn = ""

voice_agent_cpu          = 256
voice_agent_memory       = 512
voice_agent_min_tasks    = 1
voice_agent_max_tasks    = 3
voice_api_cpu            = 256
voice_api_memory         = 512
rds_instance_class       = "db.t4g.micro"
rds_allocated_storage_gb = 20
redis_node_type          = "cache.t4g.micro"
cyclone_season_min_tasks = 1
target_calls_per_task    = 4
