# Kudos System Process Flow

**Status:** Approved for the local prototype — follows [SPECIFICATION.md](./SPECIFICATION.md).

## Employee gives kudos and views the feed

```mermaid
flowchart TD
    A[Employee opens dashboard] --> B{Authenticated by portal?}
    B -- No --> B1[Redirect to portal sign-in]
    B -- Yes --> C[Load visible kudos feed]
    C --> D[Show newest 20 visible, non-deleted kudos]
    D --> E{Employee chooses to give kudos?}
    E -- No --> F[Continue browsing feed]
    E -- Yes --> G[Search active colleague directory]
    G --> H{Recipient active and not self?}
    H -- No --> H1[Show validation error; select another colleague]
    H1 --> G
    H -- Yes --> I[Enter appreciation message]
    I --> J{Trimmed message is 1-500 characters?}
    J -- No --> J1[Show field error; retain draft]
    J1 --> I
    J -- Yes --> K[Submit recipient and message]
    K --> L{Authenticated principal active?}
    L -- No --> L1[Return 401/403; do not write]
    L -- Yes --> M{Rate limit and duplicate check pass?}
    M -- No --> M1[Return 429 with retry guidance]
    M -- Yes --> N[Persist kudos with is_visible=true and server UTC timestamp]
    N --> O{Persistence succeeded?}
    O -- No --> O1[Return retryable error; log correlation ID; no success response]
    O -- Yes --> P[Return created kudos]
    P --> Q[Refresh feed; show new item]
```

## Moderator hides, restores, or deletes kudos

```mermaid
flowchart TD
    A[Moderator opens moderation view] --> B{Authenticated portal user?}
    B -- No --> B1[Return 401]
    B -- Yes --> C{Trusted moderator role?}
    C -- No --> C1[Return 403; record authorization outcome]
    C -- Yes --> D[Select kudos and moderation action]
    D --> E{Action}
    E -- Hide --> F[Set is_visible=false; set moderator/time/reason]
    E -- Restore --> G[Set is_visible=true; set moderator/time/reason]
    E -- Delete --> H[Confirm permanent content/participant-reference removal]
    H --> I{Confirmed?}
    I -- No --> J[Cancel; leave kudos unchanged]
    I -- Yes --> K[Remove content and participant references]
    F --> L[Append audit event without message content]
    G --> L
    K --> L
    L --> M{State change and audit commit succeeded?}
    M -- No --> M1[Rollback; return error with correlation ID]
    M -- Yes --> N[Invalidate affected feed/cache]
    N --> O[Return success; item hidden, restored, or removed]
```

## Key system boundaries and checks

1. The portal identity provider authenticates employees and supplies trusted identity/role claims.
2. The employee directory is the authority for active status and display names.
3. The Kudos API derives sender/moderator identities server-side and enforces all validation and authorization.
4. The database stores kudos state and append-only moderation audit events.
5. Feed queries return only visible, non-deleted items and are cursor-paginated.
6. Logs and moderation audit events must not include kudos message content.

## Implementation sequence

1. Approve this flow and the accompanying specification; confirm host portal contracts/policies.
2. Implement schema, API flows, and tests.
3. Implement accessible user and moderator interfaces against the approved API.
4. Run end-to-end/security tests and deployment readiness checks.
5. Deploy through the host portal pipeline and monitor.
