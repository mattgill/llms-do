# CollabSync: Real-Time Collaboration Platform — Technical Architecture

**Version:** 2.4.0
**Last Updated:** 2026-07-15
**Status:** Production
**Owner:** Platform Engineering

---

## 1. System Overview and Design Goals

CollabSync is a real-time collaborative editing platform supporting rich-text documents, spreadsheets, and whiteboard canvases. It enables hundreds of concurrent users to edit the same document simultaneously with sub-100ms latency for operation propagation.

### Core Design Goals

| Goal | Target | Rationale |
|---|---|---|
| Latency | P99 < 100ms end-to-end | Real-time collaboration must feel instantaneous |
| Throughput | 50,000 ops/sec per document shard | Support peak concurrency on viral documents |
| Consistency | Causal consistency with eventual strong convergence | CRDT-based model guarantees all replicas converge |
| Availability | 99.99% uptime (52m downtime/year) | Multi-AZ, multi-region active-active deployment |
| Durability | 11 nines (99.999999999%) | WAL-backed operation log with cross-region replication |
| Scalability | 10M concurrent users | Horizontal scaling at every tier; auto-scaling policies |
| Offline Support | Full offline editing with automatic merge | CRDTs enable offline-first architecture |

### Non-Functional Requirements

- **Security:** SOC 2 Type II, GDPR compliant, encryption at rest and in transit
- **Extensibility:** Plugin API for custom document types and collaboration primitives
- **Observability:** OpenTelemetry-compliant tracing, structured logging, Prometheus metrics
- **Cost Efficiency:** Spot-instance friendly, tiered storage, adaptive compression

---

## 2. High-Level Architecture

### System Topology

```
                              ┌──────────────────────────────────────────────────────────┐
                              │                        CDN / Edge                        │
                              │  ┌──────────┐  ┌──────────┐  ┌──────────┐               │
                              │  │ CloudFront│  │  Fastly  │  │ Cloudflare│              │
                              │  └─────┬────┘  └─────┬────┘  └─────┬────┘               │
                              └────────┼─────────────┼─────────────┼─────────────────────┘
                                       │             │             │
                              ┌────────┴─────────────┴─────────────┴─────────────────────┐
                              │                    API Gateway (Envoy)                    │
                              │              Rate Limiting / Auth / Routing               │
                              └────────┬──────────────────────────────┬──────────────────┘
                                       │                              │
                     ┌─────────────────┴────────────┐    ┌────────────┴──────────────────┐
                     │      HTTP/REST Services       │    │      WebSocket Services        │
                     │  ┌──────────────────────────┐ │    │  ┌───────────────────────────┐ │
                     │  │   Document Service        │ │    │  │   Gateway Service          │ │
                     │  │   Workspace Service       │ │    │  │   (Connection Management)  │ │
                     │  │   User Service            │ │    │  └───────────┬───────────────┘ │
                     │  │   Auth Service            │ │    │              │                 │
                     │  │   Search Service          │ │    │  ┌───────────┴───────────────┐ │
                     │  └──────────────────────────┘ │    │  │   Collaboration Engine     │ │
                     └──────────────┬───────────────┘    │  │   (OT/CRDT Processing)      │ │
                                    │                    │  └───────────┬───────────────┘ │
                                    │                    │              │                 │
                     ┌──────────────┴────────────────────┴──────────────┴─────────────────┐
                     │                          Message Broker                             │
                     │                    Apache Pulsar (cluster)                          │
                     │           ┌─────────┬─────────┬─────────┬─────────┐                │
                     │           │ ops.topic│presence │notify   │analytics│               │
                     │           └────┬─────┴────┬────┴────┬────┴────┬────┘                │
                     └────────────────┼──────────┼─────────┼─────────┼────────────────────┘
                                      │          │         │         │
          ┌───────────────────────────┼──────────┼─────────┼─────────┼────────────────────┐
          │                    Processing Layer  │         │         │                    │
          │  ┌────────────────┐  ┌──────────────┴┐ ┌──────┴──────┐  ┌┴──────────────────┐ │
          │  │ Op Persister   │  │ Presence      │ │ Notifier   │  │ Analytics Pipeline│ │
          │  │ (WAL Writer)   │  │ Tracker       │ │ (Push/Email)│  │ (ClickHouse/Kafka)│ │
          │  └───────┬────────┘  └──────┬────────┘ └──────┬──────┘  └────────┬──────────┘ │
          └──────────┼─────────────────┼─────────────────┼───────────────────┼────────────┘
                     │                 │                 │                   │
          ┌──────────┴─────────────────┴─────────────────┴───────────────────┴────────────┐
          │                          Persistence Layer                                     │
          │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐   │
          │  │  PostgreSQL   │  │    Redis     │  │  S3 / MinIO  │  │  Elasticsearch   │   │
          │  │  (metadata)   │  │  (cache/     │  │  (snapshots, │  │  (full-text      │   │
          │  │               │  │   pubsub/    │  │   blobs)     │  │   search)        │   │
          │  │               │  │   presence)  │  │              │  │                  │   │
          │  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────────┘   │
          └────────────────────────────────────────────────────────────────────────────────┘
```

### Service Descriptions

#### API Gateway (Envoy)
Handles TLS termination, rate limiting (token bucket, 1000 req/s per user), JWT validation, request routing based on path prefix, and WebSocket upgrade negotiation. Configured via xDS control plane for dynamic route updates.

#### Document Service
RESTful CRUD for document metadata, version management, snapshot creation, and access control. Stateless; all state stored in PostgreSQL. Implements optimistic concurrency control via document version vectors.

#### Collaboration Engine
The core real-time processing unit. Receives operations from WebSocket connections, transforms them against concurrent ops using the CRDT merge algorithm (see §4), broadcasts to session peers, and appends to the operation log in Pulsar. Each engine instance is shard-pinned to a subset of document IDs via consistent hashing (256 virtual nodes per instance).

#### Gateway Service
Manages WebSocket lifecycle: connection upgrades, heartbeats (30s interval, 90s timeout), authentication handshake, and routing to the correct Collaboration Engine shard. Maintains in-memory routing table updated via etcd watch.

#### Presence Tracker
Aggregates ephemeral presence state (cursor position, selection range, user status) from all connected clients. Uses Redis Streams for intra-cluster broadcasting with TTL-based expiration (60s heartbeat timeout). Publishes presence snapshots to subscribed clients every 500ms.

#### Op Persister
Consumes operation events from Pulsar, writes them to the PostgreSQL operation log table in configurable batch sizes (default 100 ops/batch), and periodically triggers snapshot creation when the op-to-snapshot ratio exceeds 1000:1.

---

## 3. Data Model

### Entity-Relationship Overview

```
workspaces ──< memberships >── users
    │                              │
    └── documents                  │
          │                        │
          ├── operations            │
          ├── snapshots             │
          └── document_presence ────┘
```

### Table Definitions

#### `workspaces`

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `UUID` | PK, DEFAULT gen_random_uuid() | Workspace identifier |
| `name` | `VARCHAR(255)` | NOT NULL | Display name |
| `slug` | `VARCHAR(128)` | UNIQUE, NOT NULL | URL-safe identifier |
| `plan` | `ENUM('free','pro','enterprise')` | NOT NULL, DEFAULT 'free' | Billing tier |
| `owner_id` | `UUID` | FK → users.id, NOT NULL | Creator/owner |
| `settings` | `JSONB` | DEFAULT '{}' | Workspace-level configuration |
| `created_at` | `TIMESTAMPTZ` | DEFAULT NOW() | Creation timestamp |
| `updated_at` | `TIMESTAMPTZ` | DEFAULT NOW() | Last modification |
| `deleted_at` | `TIMESTAMPTZ` | NULL | Soft delete |

Indexes: `idx_workspaces_owner` on `owner_id`, unique index on `slug WHERE deleted_at IS NULL`.

#### `documents`

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `UUID` | PK, DEFAULT gen_random_uuid() | Document identifier |
| `workspace_id` | `UUID` | FK → workspaces.id, NOT NULL | Parent workspace |
| `title` | `VARCHAR(512)` | NOT NULL | Document title |
| `type` | `ENUM('richtext','spreadsheet','whiteboard','code')` | NOT NULL | Document kind |
| `version` | `BIGINT` | NOT NULL, DEFAULT 0 | Monotonic version counter |
| `state_hash` | `VARCHAR(64)` | NOT NULL | SHA-256 of current CRDT state |
| `blob_snapshot_key` | `VARCHAR(512)` | NULL | S3 key of latest binary snapshot |
| `created_by` | `UUID` | FK → users.id, NOT NULL | Creator |
| `archived` | `BOOLEAN` | DEFAULT FALSE | Archive flag |
| `created_at` | `TIMESTAMPTZ` | DEFAULT NOW() | |
| `updated_at` | `TIMESTAMPTZ` | DEFAULT NOW() | |

Indexes: `idx_documents_workspace` on `workspace_id`, `idx_documents_type` on `type`.

#### `users`

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `UUID` | PK | User identifier |
| `email` | `VARCHAR(320)` | UNIQUE, NOT NULL | Login email |
| `display_name` | `VARCHAR(128)` | NOT NULL | Public name |
| `password_hash` | `VARCHAR(256)` | NULL | bcrypt hash (null for SSO) |
| `avatar_url` | `VARCHAR(2048)` | NULL | Profile image URL |
| `role` | `ENUM('admin','member','guest')` | DEFAULT 'member' | System role |
| `sso_provider` | `VARCHAR(64)` | NULL | 'google', 'github', 'saml' |
| `sso_subject` | `VARCHAR(256)` | NULL | External identity |
| `last_seen_at` | `TIMESTAMPTZ` | NULL | Last activity |
| `created_at` | `TIMESTAMPTZ` | DEFAULT NOW() | |

Indexes: `idx_users_email_lower` on `LOWER(email)`.

#### `memberships`

| Column | Type | Constraints | Description |
|---|---|---|---|
| `workspace_id` | `UUID` | FK → workspaces.id, PK (composite) | |
| `user_id` | `UUID` | FK → users.id, PK (composite) | |
| `role` | `ENUM('owner','admin','editor','viewer')` | NOT NULL, DEFAULT 'editor' | Workspace role |
| `joined_at` | `TIMESTAMPTZ` | DEFAULT NOW() | |
| `invited_by` | `UUID` | FK → users.id, NULL | Referrer |

#### `operations`

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `BIGSERIAL` | PK | Monotonic operation ID |
| `document_id` | `UUID` | FK → documents.id, NOT NULL | Target document |
| `user_id` | `UUID` | FK → users.id, NOT NULL | Originator |
| `op_type` | `VARCHAR(32)` | NOT NULL | 'insert', 'delete', 'move', 'format', 'cell_update' |
| `op_data` | `JSONB` | NOT NULL | Operation payload (CRDT op) |
| `lamport_clock` | `BIGINT` | NOT NULL | Lamport timestamp for causal ordering |
| `parent_version` | `BIGINT` | NOT NULL | Document version this op was based on |
| `session_id` | `UUID` | NOT NULL | Client session identifier |
| `created_at` | `TIMESTAMPTZ` | DEFAULT NOW() | |

Partitioned by `document_id` (hash partitioning, 64 partitions). Index on `(document_id, lamport_clock)`.

#### `snapshots`

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `UUID` | PK | Snapshot identifier |
| `document_id` | `UUID` | FK → documents.id, NOT NULL | |
| `version` | `BIGINT` | NOT NULL | Document version at snapshot time |
| `state` | `BYTEA` | NOT NULL | Full CRDT state (msgpack encoded) |
| `s3_key` | `VARCHAR(512)` | NULL | Offloaded to S3 if > 5 MB |
| `created_at` | `TIMESTAMPTZ` | DEFAULT NOW() | |

Index on `(document_id, version DESC)`.

#### `document_presence`

Ephemeral table (unlogged in PostgreSQL, or stored purely in Redis):

| Column | Type | Description |
|---|---|---|
| `document_id` | `UUID` | Joined document |
| `user_id` | `UUID` | Connected user |
| `cursor` | `JSONB` | `{line, column, anchor}` position |
| `selection` | `JSONB` | `{start, end}` range |
| `status` | `VARCHAR(32)` | 'active', 'idle', 'away' |
| `last_heartbeat` | `TIMESTAMPTZ` | Last ping received |

---

## 4. Conflict Resolution: CRDT Algorithm

CollabSync uses a **List CRDT based on RGA (Replicated Growable Array)** augmented with **LSEQ (Linear Sequence)** identifiers for total ordering without tombstones.

### RGA/LSEQ Core Concepts

Each character or block in the document is identified by a position identifier:

```
PositionID = (sequence_number, site_id, offset)
```

- `sequence_number`: A monotonically increasing integer unique to the `site_id`
- `site_id`: A unique, ordered identifier for each collaborating peer (derived from session UUID)
- `offset`: A sub-sequence counter for batch inserts

The total order over PositionIDs is:

```
(a_seq, a_site, a_off) < (b_seq, b_site, b_off) iff:
    a_seq < b_seq
    OR (a_seq == b_seq AND a_site < b_site)
    OR (a_seq == b_seq AND a_site == b_site AND a_off < b_off)
```

### Operation Types

```typescript
type InsertOp = {
  type: "insert";
  position: PositionID;
  content: string;            // UTF-8 encoded text
  attributes: Attributes;     // { bold?: boolean, italic?: boolean, link?: string, ... }
  after: PositionID | null;   // Left neighbor (null = start of document)
};

type DeleteOp = {
  type: "delete";
  positions: PositionID[];    // Set of positions to tombstone
  range: [PositionID, PositionID];  // Inclusive range
};

type FormatOp = {
  type: "format";
  positions: PositionID[];
  attributes: Partial<Attributes>;  // Delta to apply
};

type MoveOp = {
  type: "move";
  positions: PositionID[];
  after: PositionID | null;  // Target insertion point
};
```

### Merge Algorithm

The collision engine runs the following deterministic merge for each incoming operation:

```
function applyOperation(docState: CRDTState, op: Operation): CRDTState {
  // 1. Check if operation already applied (idempotency via op hash)
  if (docState.appliedOps.has(hash(op))) return docState;

  switch (op.type) {
    case "insert":
      // Find insertion index via total order
      const idx = binarySearchByPosition(docState.positions, op.position);
      // Guard against duplicates (same PositionID = same operation from same site)
      if (docState.positions[idx]?.equals(op.position)) return docState;
      docState.positions.splice(idx, 0, op.position);
      docState.content.splice(idx, 0, op.content);
      docState.attributes.splice(idx, 0, op.attributes);
      break;

    case "delete":
      // Tombstone: mark positions as deleted, retain structure
      for (const pos of op.positions) {
        docState.tombstones.add(pos);
      }
      break;

    case "format":
      for (const pos of op.positions) {
        const idx = findPositionIndex(docState.positions, pos);
        if (idx >= 0) {
          docState.attributes[idx] = mergeAttributes(
            docState.attributes[idx], op.attributes
          );
        }
      }
      break;

    case "move":
      // Remove from current location, re-insert at target
      const moved = splicePositions(docState, op.positions);
      const targetIdx = op.after
        ? findPositionIndex(docState.positions, op.after) + 1
        : 0;
      docState.positions.splice(targetIdx, 0, ...moved.positions);
      docState.content.splice(targetIdx, 0, ...moved.content);
      docState.attributes.splice(targetIdx, 0, ...moved.attributes);
      break;
  }

  docState.appliedOps.add(hash(op));
  docState.lamportClock = max(docState.lamportClock, op.lamportClock) + 1;
  return docState;
}
```

### Anti-Entropy Protocol

When a client reconnects after disconnection, the server computes a state delta:

1. Client sends its last known `lamport_clock` and `state_hash`
2. Server fetches all operations with `lamport_clock > client_clock` from the op log (up to a configurable limit, default 500 ops)
3. If the operation count exceeds the limit, the server sends the latest snapshot + operations since snapshot
4. Client applies the delta and re-hashes to validate convergence

### Interleaving Guarantee

For text editing, the algorithm guarantees **intention preservation**: if user A inserts "hello" and user B inserts "world" at the same position (same `after`), the total order ensures a deterministic result (e.g., A's insert before B's). The fractional indexing scheme allows infinite interleaving without rebalancing.

---

## 5. API Design

### REST API Endpoints

#### Authentication

```
POST   /api/v1/auth/register          # Create account
POST   /api/v1/auth/login             # Email/password login
POST   /api/v1/auth/refresh           # Refresh JWT token pair
POST   /api/v1/auth/logout            # Invalidate refresh token
GET    /api/v1/auth/sso/{provider}    # Initiate SSO flow
POST   /api/v1/auth/sso/callback      # SSO callback
```

#### Workspaces

```
GET    /api/v1/workspaces                  # List user's workspaces
POST   /api/v1/workspaces                  # Create workspace
GET    /api/v1/workspaces/{id}             # Get workspace details
PATCH  /api/v1/workspaces/{id}             # Update workspace
DELETE /api/v1/workspaces/{id}             # Soft-delete workspace
GET    /api/v1/workspaces/{id}/members     # List members
POST   /api/v1/workspaces/{id}/members     # Invite member
PATCH  /api/v1/workspaces/{id}/members/{uid}  # Update role
DELETE /api/v1/workspaces/{id}/members/{uid}  # Remove member
```

#### Documents

```
GET    /api/v1/workspaces/{wid}/documents                    # List documents
POST   /api/v1/workspaces/{wid}/documents                    # Create document
GET    /api/v1/documents/{id}                                # Get metadata
PATCH  /api/v1/documents/{id}                                # Update metadata
DELETE /api/v1/documents/{id}                                # Archive document
GET    /api/v1/documents/{id}/versions                       # List snapshots
POST   /api/v1/documents/{id}/versions/{ver}/restore         # Restore snapshot
GET    /api/v1/documents/{id}/export?format=pdf|docx|md      # Export document
```

#### Search

```
GET    /api/v1/search?q={query}&workspace_id={wid}&type={type}
GET    /api/v1/search/suggest?q={prefix}
```

### WebSocket Protocol

Connection: `wss://ws.collabsync.io/v1/session?token={jwt}`

#### Client → Server Messages

```jsonc
// Join a document for collaboration
{
  "type": "join",
  "document_id": "uuid",
  "last_clock": 42,
  "state_hash": "sha256hex"
}

// Submit an operation
{
  "type": "op",
  "seq": 15,                          // Client-side monotonic seq (for dedup)
  "op": {
    "type": "insert",
    "position": [12345, "a1b2c3", 0],
    "content": "Hello",
    "attributes": { "bold": true },
    "after": [12344, "a1b2c3", 0]
  },
  "lamport_clock": 43
}

// Update cursor/selection
{
  "type": "presence",
  "cursor": { "line": 12, "column": 4 },
  "selection": null
}

// Heartbeat
{
  "type": "ping",
  "client_time": 1715788800123
}
```

#### Server → Client Messages

```jsonc
// Operation broadcast (sent to all peers except origin)
{
  "type": "op",
  "user_id": "uuid",
  "user_name": "Alice",
  "op": { ... },
  "lamport_clock": 44,
  "server_seq": 9823
}

// Operation acknowledgement (to origin only)
{
  "type": "ack",
  "client_seq": 15,
  "server_seq": 9823,
  "lamport_clock": 44
}

// Presence update (throttled to 500ms)
{
  "type": "presence",
  "users": [
    { "user_id": "uuid", "name": "Alice", "cursor": {...}, "status": "active" },
    { "user_id": "uuid", "name": "Bob", "cursor": {...}, "selection": {...}, "status": "idle" }
  ]
}

// State sync response (on join or out-of-sync detection)
{
  "type": "sync",
  "ops": [ ... ],                    // Array of missed operations
  "snapshot": null,                  // Full state if ops exceed threshold
  "current_clock": 44
}

// Error
{
  "type": "error",
  "code": "RATE_LIMITED",
  "message": "Too many operations",
  "retry_after_ms": 1000
}
```

### Rate Limits

| Endpoint | Limit | Window |
|---|---|---|
| REST API (total) | 1000 req/s per user | 1 second |
| WebSocket ops | 100 ops/s per connection | 1 second |
| WebSocket presence | 4 updates/s | 1 second |
| Document creation | 10/min per workspace | 1 minute |
| Auth endpoints | 20 req/min per IP | 1 minute |

---

## 6. Authentication and Authorization

### JWT Token Architecture

CollabSync uses a dual-token system:

| Token | Lifetime | Storage | Purpose |
|---|---|---|---|
| Access Token (JWT) | 15 minutes | In-memory (browser) | API authorization |
| Refresh Token (opaque) | 30 days | HttpOnly secure cookie | Renew access tokens |

Access token claims:

```json
{
  "sub": "user-uuid",
  "email": "user@example.com",
  "role": "member",
  "workspaces": {
    "ws-uuid-1": "editor",
    "ws-uuid-2": "viewer"
  },
  "iat": 1715788800,
  "exp": 1715789700,
  "jti": "unique-token-id"
}
```

### Authentication Flow

```
Client                          API Gateway                    Auth Service
  │                                  │                              │
  │  POST /auth/login                │                              │
  │  {email, password}               │                              │
  │ ────────────────────────────────>│                              │
  │                                  │  Validate credentials        │
  │                                  │ ────────────────────────────>│
  │                                  │                              │
  │                                  │  {access_token,              │
  │                                  │   refresh_token}             │
  │                                  │ <────────────────────────────│
  │  Set-Cookie: refresh_token       │                              │
  │  {access_token}                  │                              │
  │ <────────────────────────────────│                              │
  │                                  │                              │
  │  WebSocket connect + access_token│                              │
  │ ────────────────────────────────>│                              │
  │                                  │  Validate JWT, upgrade       │
  │  101 Switching Protocols         │                              │
  │ <────────────────────────────────│                              │
  │                                  │                              │
  │  [access_token expires]          │                              │
  │  POST /auth/refresh              │                              │
  │  Cookie: refresh_token           │                              │
  │ ────────────────────────────────>│                              │
  │                                  │  Validate refresh token      │
  │                                  │ ────────────────────────────>│
  │  {new_access_token}              │                              │
  │ <────────────────────────────────│                              │
```

### Authorization Model

Access control is evaluated at three levels:

1. **Gateway Level:** JWT signature validation, expiration check, revoked token check (Redis bloom filter)
2. **Service Level:** Workspace membership check against JWT claims (validated per-request via sidecar)
3. **Document Level:** Document-specific permissions resolved from memberships table, cached in Redis for 5 minutes

Permission matrix:

| Role | View Docs | Edit Docs | Share/Invite | Manage Workspace | Delete Workspace | Billing |
|---|---|---|---|---|---|---|
| Owner | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Admin | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ |
| Editor | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
| Viewer | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ |

### SSO Integration

Supports SAML 2.0 and OpenID Connect. Identity providers configured per-workspace (Enterprise plan). Just-in-time user provisioning on first SSO login. IdP-initiated and SP-initiated flows both supported.

---

## 7. Infrastructure

### Kubernetes Cluster Topology

```
┌─────────────────────────────────────────────────────────────────────┐
│                      Kubernetes Cluster (EKS 1.30)                   │
│                                                                      │
│  ┌──────────────────────────┐    ┌──────────────────────────────┐   │
│  │   System Namespace        │    │   Application Namespaces     │   │
│  │  ┌────────────────────┐  │    │                              │   │
│  │  │ cert-manager       │  │    │  ┌─────────────────────────┐ │   │
│  │  │ external-dns       │  │    │  │ collabsync-prod         │ │   │
│  │  │ aws-load-balancer  │  │    │  │  ├─ api-gateway (6)     │ │   │
│  │  │ cluster-autoscaler │  │    │  │  ├─ doc-service (4)     │ │   │
│  │  │ metrics-server     │  │    │  │  ├─ ws-gateway (8)      │ │   │
│  │  │ prometheus-stack   │  │    │  │  ├─ collab-engine (12)  │ │   │
│  │  │ opentelemetry      │  │    │  │  ├─ presence-tracker (3)│ │   │
│  │  │ fluent-bit         │  │    │  │  └─ op-persister (4)    │ │   │
│  │  └────────────────────┘  │    │  └─────────────────────────┘ │   │
│  └──────────────────────────┘    └──────────────────────────────┘   │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                    Node Groups                                 │   │
│  │  ┌─────────────────┐  ┌─────────────────┐  ┌───────────────┐ │   │
│  │  │ Compute (c6i)   │  │ Memory (r6i)    │  │ Spot (mixed)  │ │   │
│  │  │ API/WS services │  │ Collab engines  │  │ Batch/workers │ │   │
│  │  │ 6 nodes, 4 AZs  │  │ 8 nodes, 4 AZs  │  │ 4 nodes, 2 AZs│ │   │
│  │  └─────────────────┘  └─────────────────┘  └───────────────┘ │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### Data Store Configuration

#### PostgreSQL (RDS, Multi-AZ)
- **Instance:** db.r6g.4xlarge (16 vCPU, 128 GB RAM)
- **Storage:** 2 TB gp3 (12,000 IOPS, 500 MB/s throughput), auto-scaling to 8 TB
- **Replicas:** 2 read replicas (cross-AZ) for search and analytics queries
- **Version:** PostgreSQL 16
- **Key config:** `max_connections=500`, `shared_buffers=32GB`, `effective_cache_size=96GB`, `wal_level=logical`

#### Redis (ElastiCache, Cluster Mode)
- **Instance:** cache.r6g.xlarge, 3 shards × 2 replicas (6 nodes total)
- **Use cases:**
  - Session store (TTL: 15 min for access token blocks)
  - Distributed rate limiting (sliding window counters)
  - Presence ephemeral storage (TTL: 90s per entry)
  - Pub/Sub for intra-cluster event broadcasting
  - CRDT state cache (hot documents, LRU eviction)
  - Bloom filter for token revocation

#### S3 / MinIO
- **Buckets:**
  - `collabsync-snapshots-prod` — CRDT state snapshots (> 5 MB offloaded)
  - `collabsync-assets-prod` — User-uploaded images and attachments
  - `collabsync-exports-prod` — Generated PDF/DOCX exports (TTL: 7 days)
  - `collabsync-backups-prod` — pg_dump exports and WAL archives
- **Lifecycle policies:** Snapshots older than 90 days transition to S3 Intelligent-Tiering; exports auto-expire at 7 days
- **Encryption:** SSE-KMS with customer-managed keys, rotated annually

#### Apache Pulsar
- **Cluster:** 3 broker nodes, 3 bookkeeper nodes
- **Topics:**
  - `persistent://collabsync/ops/{document_id}` — Partitioned, keyed by document_id
  - `persistent://collabsync/presence/{region}` — Ephemeral presence events
  - `persistent://collabsync/notifications` — Email/push notification queue
  - `persistent://collabsync/analytics` — Raw analytics events for pipeline
- **Retention:** ops topic retains 7 days; analytics topic retains 30 days

---

## 8. Performance Benchmarks and Scaling Strategy

### Benchmarks (Measured at Steady State)

| Metric | Target | Measured (P50) | Measured (P99) |
|---|---|---|---|
| REST API latency (GET document) | < 50ms | 12ms | 45ms |
| REST API latency (List documents, 100 items) | < 100ms | 35ms | 82ms |
| WebSocket op propagation (single region) | < 100ms | 28ms | 67ms |
| WebSocket op propagation (cross-region) | < 300ms | 110ms | 245ms |
| State sync upon join (100 ops behind) | < 500ms | 180ms | 410ms |
| Snapshot creation (100K ops document) | < 2s | 650ms | 1.8s |
| Document export (PDF, 50 pages) | < 5s | 1.2s | 4.3s |

### Load Test Profile

Test conducted with k6, simulating 100,000 concurrent connections:

```
export const options = {
  stages: [
    { duration: '5m', target: 10000 },    // Ramp up
    { duration: '30m', target: 100000 },   // Steady state
    { duration: '5m', target: 0 },         // Ramp down
  ],
  thresholds: {
    'http_req_duration': ['p(99)<200'],
    'ws_op_latency': ['p(99)<100'],
  },
};
```

### Horizontal Scaling Strategy

| Component | Scaling Metric | Min | Max | Target Utilization |
|---|---|---|---|---|
| API Gateway (Envoy) | Request rate | 3 | 20 | 70% CPU |
| Document Service | Request rate | 2 | 16 | 60% CPU |
| WS Gateway | Active connections | 4 | 32 | 50% memory |
| Collaboration Engine | Ops/sec and connection count | 4 | 48 | 65% CPU |
| Op Persister | Pulsar consumer lag | 2 | 16 | Lag < 1000 messages |
| Presence Tracker | Active sessions | 2 | 8 | 60% CPU |

### Dynamic Sharding

The Collaboration Engine uses consistent hashing (ring size: 16384 slots) to map document IDs to engine instances. When a new engine pod joins:
1. It registers in etcd under `/collabsync/engines/{pod-ip}`
2. Neighboring pods on the ring transfer ownership of affected document shards
3. Document sessions are gracefully migrated (existing connections drained over 30s)

### Multi-Region Active-Active

```
┌──────────────┐      ┌──────────────┐      ┌──────────────┐
│  us-east-1   │      │  eu-west-1   │      │  ap-south-1  │
│  (Primary)   │◄────►│  (Replica)   │◄────►│  (Replica)   │
│              │      │              │      │              │
│  PostgreSQL  │      │  PostgreSQL  │      │  PostgreSQL  │
│  RDS (write) │ ───► │  Read Replica│ ───► │  Read Replica│
│              │      │              │      │              │
│  Pulsar      │      │  Pulsar      │      │  Pulsar      │
│  Cluster     │◄────►│  Cluster     │◄────►│  Cluster     │
│ (Geo-Repl)   │      │ (Geo-Repl)   │      │ (Geo-Repl)   │
└──────────────┘      └──────────────┘      └──────────────┘
```

Users are routed to the nearest region via Route53 latency-based routing. Pulsar geo-replication ensures all operations propagate globally. PostgreSQL is single-writer (us-east-1) with cross-region read replicas for local read traffic. Write operations against `documents` or `operations` tables are routed to the primary region with automatic retry.

---

## 9. Disaster Recovery and Backup Strategy

### RPO and RTO Targets

| Scenario | RPO | RTO | Strategy |
|---|---|---|---|
| Single AZ failure | 0 | < 60s | Multi-AZ auto-failover |
| Region failure | < 5s | < 5 min | Route53 failover to secondary region |
| PostgreSQL corruption | < 5 min | < 30 min | Point-in-time recovery from WAL |
| Accidental deletion | 0 (no data loss) | < 1 hour | Soft-delete with 30-day retention |
| Catastrophic multi-region | < 24 hours | < 4 hours | Full restore from cross-account backup |

### Backup Schedule

| Data Store | Method | Frequency | Retention |
|---|---|---|---|
| PostgreSQL | pg_dump (logical) | Daily at 03:00 UTC | 30 days |
| PostgreSQL | WAL archiving (S3) | Continuous (5 min segments) | 7 days |
| Redis | RDB snapshot | Every 6 hours | 3 days |
| S3 (assets) | Cross-region replication | Continuous | Source: S3 Standard, Replica: S3 IA |
| S3 (snapshots) | Cross-account replication | Continuous | 90 days in target account |
| Pulsar | BookKeeper ledger backups | Every 4 hours | 14 days |

### Recovery Procedure (PostgreSQL)

```bash
# 1. Identify recovery point
aws rds describe-db-cluster-snapshots \
  --db-cluster-identifier collabsync-prod \
  --snapshot-type automated \
  --query 'DBClusterSnapshots[-1]'

# 2. Restore to point-in-time
aws rds restore-db-cluster-to-point-in-time \
  --source-db-cluster-identifier collabsync-prod \
  --target-db-cluster-identifier collabsync-recovery \
  --restore-to-time "2026-07-14T22:45:00Z"

# 3. Validate data integrity
psql -h collabsync-recovery.cluster-xxx.us-east-1.rds.amazonaws.com \
  -U admin -d collabsync -c "SELECT count(*) FROM operations;"

# 4. Promote to production (if primary is unrecoverable)
aws rds promote-read-replica \
  --db-instance-identifier collabsync-recovery-instance

# 5. Update DNS
aws route53 change-resource-record-sets \
  --hosted-zone-id ZXXXXXXXXXXXX \
  --change-batch file://failover-dns.json
```

### Chaos Engineering

Monthly GameDay exercises:
- Random pod termination (2% of fleet per hour)
- Network partition simulation (isolate an AZ for 5 minutes)
- Database replica lag injection (artificial 30s delay)
- Redis cluster node failure

All failures must be detected by monitoring within 60 seconds and self-heal or alert within 5 minutes.

---

## 10. Monitoring and Observability

### Metrics

#### RED Metrics (Rate, Errors, Duration) — Every Service

```promql
# Request rate by service
sum(rate(http_requests_total{namespace="collabsync-prod"}[5m])) by (service)

# Error rate (5xx)
sum(rate(http_requests_total{status_code=~"5.."}[5m])) by (service)
  / sum(rate(http_requests_total[5m])) by (service)

# P99 latency
histogram_quantile(0.99,
  sum(rate(http_request_duration_seconds_bucket[5m])) by (service, le))
```

#### Business Metrics

| Metric | Prometheus Name | Dimensions |
|---|---|---|
| Active documents | `collabsync_documents_active` | workspace_id, type |
| Concurrent sessions | `collabsync_sessions_active` | region, document_id |
| Operations ingested/sec | `collabsync_ops_ingested_total` | document_type, region |
| CRDT state size (bytes) | `collabsync_crdt_state_size` | document_id |
| WS connections | `collabsync_ws_connections` | gateway_pod, region |
| Presence updates/sec | `collabsync_presence_updates_total` | region |
| Snapshot generation time | `collabsync_snapshot_duration_seconds` | document_type |

#### Infrastructure Metrics

```promql
# PostgreSQL connection utilization
pg_stat_database_numbackends / pg_settings_max_connections

# Redis cache hit ratio
rate(redis_keyspace_hits_total[5m])
  / (rate(redis_keyspace_hits_total[5m]) + rate(redis_keyspace_misses_total[5m]))

# Pulsar consumer lag
pulsar_consumer_msg_backlog{subscription="op-persister"}
```

### Distributed Tracing

All services instrumented with OpenTelemetry SDKs. Trace context propagated via:
- HTTP: W3C Trace Context headers (`traceparent`, `tracestate`)
- WebSocket: Custom frame metadata on connect, session-scoped trace ID
- Pulsar: Trace context in message properties

Critical spans:

```
Client WS Connect
  ├── Gateway: Upgrade & Auth (JWT validation)
  │   └── Auth Service: Validate token
  ├── Gateway: Route to Engine
  │   └── etcd: Lookup engine for document
  └── Collab Engine: Join Document
      ├── PostgreSQL: Fetch current version & hash
      ├── Redis: Subscribe to document channel
      └── Pulsar: Publish join event
```

Sampling strategy: 100% for errors and latency > 500ms, 10% for all other requests. Tail-based sampling in OpenTelemetry Collector using `tail_sampling` processor.

### Logging

Structured JSON logging with the following schema:

```json
{
  "timestamp": "2026-07-15T14:32:11.123Z",
  "level": "INFO",
  "service": "collab-engine",
  "trace_id": "0af7651916cd43dd8448eb211c80319c",
  "span_id": "b7ad6b7169203331",
  "document_id": "d290f1ee-6c54-4b01-90e6-d701748f0851",
  "user_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "message": "Operation applied",
  "op_type": "insert",
  "lamport_clock": 1042,
  "processing_time_ms": 3.4
}
```

Log pipeline: `fluent-bit (DaemonSet)` → `Kinesis Firehose` → `S3 (raw)` + `OpenSearch (indexed)`. Log retention: 30 days hot (OpenSearch), 365 days cold (S3 Glacier Deep Archive).

### Alerting Rules

| Alert | Expression | Severity | Notification Channel |
|---|---|---|---|
| High Error Rate | `rate(http_requests_total{status_code=~"5.."}[5m]) > 1` | Critical | PagerDuty |
| High P99 Latency | `histogram_quantile(0.99, ...) > 0.5` (500ms) | Warning | Slack #oncall |
| WS Connection Drops | `deriv(collabsync_ws_connections[5m]) < -100` | Warning | Slack #oncall |
| Pulsar Consumer Lag | `pulsar_consumer_msg_backlog > 5000` for 5m | Critical | PagerDuty |
| PostgreSQL Replica Lag | `pg_stat_replication_flush_lag > 30s` | Warning | Slack #infra |
| Redis Memory > 80% | `redis_memory_used_bytes / redis_memory_max_bytes > 0.8` | Warning | Slack #infra |
| CRDT Divergence | `collabsync_crdt_divergence_detected > 0` | Critical | PagerDuty |
| Certificate Expiry | `certmanager_certificate_expiry_seconds < 604800` (7d) | Warning | Slack #infra |
| Snapshot Failure Rate | `rate(collabsync_snapshot_failures_total[15m]) > 0.01` | Warning | Slack #oncall |

### Dashboards

**Operational Dashboard:**
- Real-time connection count by region (time series)
- Operations per second (time series, stacked by document type)
- P50/P95/P99 latency (heat map)
- Error rate by service (sparkline grid)
- Pod restart count (stat panel)
- Database connection pool utilization (gauge)

**Business Dashboard:**
- DAU/WAU/MAU (stat + time series)
- Documents created per hour (bar chart)
- Average session duration (stat)
- Top 10 most-edited documents (table)
- Workspace growth rate (time series)

**System Health Dashboard:**
- Node CPU/Memory/Disk utilization (gauge grid by node)
- Redis hit ratio (line chart)
- Pulsar throughput in/out (dual time series)
- CDN cache hit ratio (stat)
- SSL certificate expiry countdown (stat)

---

*Document version 2.4.0 — Approved by Architecture Review Board 2026-07-10.*
