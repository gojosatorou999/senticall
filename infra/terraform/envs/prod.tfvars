environment = "prod"

domain_name         = "voice.sentinel.in"
acm_certificate_arn = ""   # fill in after ACM cert is issued

# Production sizing per §14.1
voice_agent_cpu           = 2048   # 2 vCPU
voice_agent_memory        = 4096   # 4 GB
voice_agent_min_tasks     = 2
voice_agent_max_tasks     = 50
voice_api_cpu             = 512    # 0.5 vCPU
voice_api_memory          = 1024
rds_instance_class        = "db.t4g.large"
rds_allocated_storage_gb  = 100
redis_node_type           = "cache.t4g.small"
cyclone_season_min_tasks  = 10
target_calls_per_task     = 8
