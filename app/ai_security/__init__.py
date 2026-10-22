"""AI guardrails: task contracts, request budgeting, injection signals, evidence validation, telemetry.

Every LLM call goes through ``gateway.run_task`` with a registered ``contracts.TaskContract``. AI output is
advisory: it is schema-validated and never mutates workflow state, approves anything or activates rules.
"""
