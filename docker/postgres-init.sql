-- Runs once when the postgres volume is first initialised.
-- The custom image (docker/Dockerfile.postgres) ships PostGIS + pgvector,
-- matching the RDS setup in §12.1.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS postgis;
