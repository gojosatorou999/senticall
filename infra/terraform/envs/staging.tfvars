environment = "staging"

# Populate these values before running `make deploy ENV=staging`:
#   domain_name         = "voice-staging.sentinel.in"
#   acm_certificate_arn = "arn:aws:acm:ap-south-1:<account>:certificate/<id>"
#
# Leave as empty strings here; CI/CD passes them via environment-specific
# Terraform variable files or GitHub Actions inputs.

domain_name         = "voice-staging.sentinel.in"
acm_certificate_arn = ""   # fill in after ACM cert is issued

# Sizing — staging uses minimal capacity to reduce burn rate
voice_agent_cpu           = 512
voice_agent_memory        = 1024
voice_agent_min_tasks     = 1
voice_agent_max_tasks     = 5
voice_api_cpu             = 256
voice_api_memory          = 512
rds_instance_class        = "db.t4g.micro"
rds_allocated_storage_gb  = 20
redis_node_type           = "cache.t4g.micro"
cyclone_season_min_tasks  = 2
target_calls_per_task     = 4
