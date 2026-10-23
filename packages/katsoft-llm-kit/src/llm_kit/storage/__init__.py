"""llm_kit.storage: SQLite connection and transaction mechanics, atomic JSON.

Layer 0; imports nothing from llm_kit. No root re-exports; import from the
submodules:

- ``llm_kit.storage.connection``: ConnectionPolicy, connect, connect_probe,
  connect_readonly, ensure_wal, readonly_uri, DEFAULT_BUSY_TIMEOUT_MS.
- ``llm_kit.storage.transaction``: transaction, write_transaction,
  read_transaction.
- ``llm_kit.storage.schema_version``: read_schema_version.
- ``llm_kit.storage.network``: looks_like_network_path.
- ``llm_kit.storage.atomic_json``: read, write.

Product DDL, migration loops, version bookkeeping, ownership policy and
warning text stay with the products (jobs, pipeline.execution, spend ledger).
Each product describes its own pragma order and isolation posture with a
ConnectionPolicy rather than sharing one.
"""
