# apps/api

FastAPI server backed by Postgres and RustFS/S3 from the dev compose stack. It
provides:

- agents/versions/deployments CRUD (Create, Read, Update, Delete)
- auth
- plugin bundle validate/store/fetch
- Langfuse proxy endpoints
- GitHub App integration (the git-flow engine that promotes on merge)
- the Langfuse-backed metrics/logs endpoints
