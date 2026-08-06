# Offline liveness adapter

Inventory search is owned by BinKeeper and does not depend on transcript
liveness. The optional liveness lane accepts only an explicit local JSON file;
it never opens an Engram database connection or queries transcript tables.

The file contract is:

```json
{
  "schema_version": "binkeeper.liveness-export.v1",
  "mentions": [
    {
      "source_kind": "capture",
      "source_ref": "offline:stable-reference",
      "content_text": "used the torque wrench",
      "mentioned_at": "2026-07-01T12:00:00Z",
      "privacy_tier": 1
    }
  ]
}
```

The export is inert input: BinKeeper cannot write back through it. Missing,
unversioned, or malformed input never becomes an empty success. An unconfigured
adapter reports `status: unavailable`, which the harvest result surfaces as
`source_status: unavailable`; malformed configured files raise a typed adapter
error. Approved-source and privacy-tier policy is applied again inside
BinKeeper before any append-only liveness evidence is recorded. No deployment
surface configures this file today: the adapter is constructed
programmatically, the harvest entry point is not wired into the CLI, MCP, or
service, and the unavailable default is production behavior.

This contract authorizes no transcript export job, network access, model,
embedding, semantic index, or cloud service. A later deployment item must name
any producer and owner-controlled transfer mechanism before enabling it.
