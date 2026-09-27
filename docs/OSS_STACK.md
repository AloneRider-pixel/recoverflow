# RecoverFlow OSS integration stack

RecoverFlow uses a small number of permissively licensed open-source projects where they improve a real product boundary.

## Integrated

### Frictionless Framework — `frictionlessdata/frictionless-py`
Purpose: validate and normalize CSV/XLS/XLSX receivables before they touch the database.

Why: the first customer workflow is import-first, and Frictionless provides schema-aware tabular validation and Excel support.

License: MIT.

Source: https://github.com/frictionlessdata/frictionless-py

### LangGraph — `langchain-ai/langgraph`
Purpose: structure the explainable next-best-action workflow as score → recommend → schedule.

Why: collections is a stateful workflow. LangGraph provides graph-based orchestration, while RecoverFlow retains deterministic business rules and human approval.

License: MIT.

Source: https://github.com/langchain-ai/langgraph

### Sentry Python SDK — `getsentry/sentry-python`
Purpose: production exception tracing for FastAPI when `SENTRY_DSN` is configured.

Why: payment and collection workflows are revenue-critical and need observable failures without logging secrets or payment credentials.

License: MIT.

Source: https://github.com/getsentry/sentry-python

## Considered, not yet enabled

### pgvector — `pgvector/pgvector-python`
Purpose: semantic retrieval over historical collection conversations.

Render Postgres supports the `vector` extension, but enabling it is a database change. The feature should be activated only after the first customers generate enough collection history to justify semantic retrieval.

Source: https://github.com/pgvector/pgvector-python

### Taskiq — `taskiq-python/taskiq`
Purpose: distributed background execution for scheduled collections.

It is a good fit technically, but deploying a queue adds infrastructure. RecoverFlow should prove the workflow before introducing a worker/broker service.

Source: https://github.com/taskiq-python/taskiq

## Product rule

Open-source dependencies are components, not the product. RecoverFlow's differentiated value remains its receivables data model, collection decisioning, customer memory, action workflow, integrations, and measurable collection outcomes.
