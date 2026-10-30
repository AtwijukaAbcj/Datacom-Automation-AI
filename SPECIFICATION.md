# Kudos System Specification

**Status:** Approved for a standalone Python + SQLite prototype  
**Product:** Internal employee portal  
**Feature:** Peer-to-peer kudos with a moderated dashboard feed

## Initial feature request

> Create a feature for our internal web app that allows users to give 'kudos' to their colleagues. A user should be able to select another user from a list, write a short message of appreciation, and submit it. There should also be a public feed on the main dashboard where all recently submitted kudos are visible.

## Scope and assumptions

- "Public" means visible to authenticated employees of the organization, not to the internet.
- The existing employee portal provides authentication and a trusted employee directory. This feature does not create accounts or credentials.
- The authenticated employee identity and roles come from the portal's trusted identity provider; clients cannot choose or spoof the sender.
- Only active employees can give or receive kudos. Users cannot give kudos to themselves.
- Kudoses are attributed to the sender; anonymous kudos are not in scope.
- A kudos message is plain text, trimmed, and limited to 500 characters.
- The feed displays the newest visible kudos first, 20 per page.
- Moderators can hide, restore, or delete a kudos. Hide is reversible; delete permanently removes the message and recipient/sender association, while retaining a minimal audit event without message content.
- Initial anti-abuse policy: no more than 10 submissions per user per hour; identical repeated submissions by the same sender to the same recipient within 60 seconds are rejected. The limit and duplicate window are configurable.
- The design is technology-agnostic and integrates with the host portal's database, API framework, UI framework, and deployment pipeline.
- For this exercise only, the implementation is a local Python standard-library HTTP server with SQLite and a browser UI. It is a demonstrator, not a production-ready replacement for portal SSO, directory, infrastructure, or security controls.

## Functional requirements

### User stories

1. As an authenticated employee, I can select an active colleague from a searchable list so I can give kudos to the intended person.
2. As an authenticated employee, I can write and submit a short appreciation message so recognition is recorded.
3. As an authenticated employee, I can view recent kudos on the dashboard so I can see recognition shared across the organization.
4. As an administrator/moderator, I can hide an inappropriate kudos, optionally record a reason, and restore it if appropriate.
5. As an administrator/moderator, I can permanently delete an inappropriate kudos while retaining a minimal moderation audit event.

### Acceptance criteria

#### Employee selection and submission

- Only authenticated active employees can load the recipient list and submit kudos.
- Recipient search returns active colleagues only and supports paging or incremental search.
- The current user cannot select themselves as recipient.
- The sender is derived from the authenticated server-side principal, never from a trusted client-supplied sender ID.
- The message is trimmed before validation and storage; empty/whitespace-only messages and messages over 500 characters are rejected with a useful validation response.
- Submitted message text is rendered as text, not interpreted as HTML or script.
- A successful submission records the sender, recipient, message, creation time, and initial visible state, and appears in the feed.
- The server rejects repeated identical submissions from the same sender to the same recipient within 60 seconds and enforces the configurable rate limit of 10 submissions per hour.
- Validation or authorization failures do not create a kudos.

#### Dashboard feed

- The feed includes only non-deleted kudos for which `is_visible = true`.
- Results are ordered by creation time descending, with a stable ID tie-breaker.
- The default page size is 20; callers cannot request more than 100 records in one page.
- Feed items show sender, recipient, message, and submission time, subject to the portal's employee directory/privacy policy.
- The feed supports pagination and does not load the entire kudos history at once.
- A hidden or deleted kudos disappears from subsequent feed queries.

#### Moderation

- Only users with the trusted moderator/administrator role can hide, restore, or delete kudos.
- Hiding sets `is_visible = false` and stores the moderator, timestamp, and optional reason.
- Restoring sets `is_visible = true` and stores an audit event identifying the moderator and time.
- Deleting removes the kudos content and sender/recipient association from the active kudos table; the moderation audit event records the action and kudos identifier but not the deleted message.
- Moderator actions are authorized and audited server-side. Repeated requests are idempotent where practical.
- Unauthorized users receive an authorization error and cannot infer or modify hidden message content through feed endpoints.

## Technical design

### Data model

#### Employee

Employee data is owned by the host portal's identity/directory service; the Kudos feature stores references, not duplicate credentials.

| Field | Type | Notes |
|---|---|---|
| `employee_id` | string/UUID | Primary identifier from the trusted directory |
| `display_name` | string | Retrieved from the directory for display |
| `status` | enum | Only active employees can participate |
| `roles` | set of strings | Includes moderator/administrator role where assigned |

#### Kudos

| Field | Type | Constraints / notes |
|---|---|---|
| `kudos_id` | UUID | Primary key |
| `sender_id` | string/UUID | Required directory reference; set from authenticated principal |
| `recipient_id` | string/UUID | Required directory reference; must be active and differ from sender |
| `message` | text | Required; trimmed; 1-500 characters |
| `created_at` | timestamp | Required; UTC, generated by server |
| `is_visible` | boolean | Required; default `true`; false hides the item from employee feeds |
| `moderated_by` | string/UUID, nullable | Moderator directory reference for the latest moderation action |
| `moderated_at` | timestamp, nullable | UTC timestamp for the latest moderation action |
| `moderation_reason` | string, nullable | Optional, bounded plain text; never shown in the public feed |
| `deleted_at` | timestamp, nullable | Null while active; non-null after a logical deletion request |

**Relationships and indexes**

- `sender_id` and `recipient_id` reference active employee directory IDs; prefer directory-backed references where the host database supports them.
- Index `(is_visible, deleted_at, created_at DESC, kudos_id)` for feed queries.
- Index `(sender_id, recipient_id, created_at DESC)` to support duplicate checks and sender history.
- A database constraint or service validation prevents self-awards.
- If the directory is external, validate employee status through its supported service and handle directory unavailability as a retryable dependency error.

#### ModerationAudit

| Field | Type | Constraints / notes |
|---|---|---|
| `audit_id` | UUID | Primary key |
| `kudos_id` | UUID | Identifier of affected kudos; retained even if the kudos is deleted |
| `moderator_id` | string/UUID | Trusted authenticated moderator identity |
| `action` | enum | `hide`, `restore`, or `delete` |
| `reason` | string, nullable | Optional bounded reason; excludes message content |
| `occurred_at` | timestamp | UTC, generated by server |

Moderation events are append-only. The audit table must not store copied kudos message content. Apply the organization's retention policy to audit records.

### API endpoints

All endpoints require portal authentication and use JSON. The server derives the actor from the authenticated principal.

| Method and path | Authorization | Purpose |
|---|---|---|
| `GET /api/employees?query=&cursor=` | Authenticated employee | Search active recipients; excludes the current user |
| `POST /api/kudos` | Authenticated active employee | Create kudos. Body: `{ "recipient_id": "...", "message": "..." }` |
| `GET /api/kudos?cursor=&limit=` | Authenticated employee | Read paginated visible, non-deleted feed |
| `GET /api/moderation/kudos?limit=` | Moderator | List up to 100 hidden, non-deleted kudos in the moderation queue |
| `POST /api/kudos/{id}/hide` | Moderator | Hide kudos. Optional body: `{ "reason": "..." }` |
| `POST /api/kudos/{id}/restore` | Moderator | Restore hidden kudos |
| `DELETE /api/kudos/{id}` | Moderator | Logically delete kudos and append audit event; erase message and participant references per deletion policy |

**Response and error behavior**

- Create returns `201 Created` with the created feed item.
- Invalid input returns `400 Bad Request` or `422 Unprocessable Entity` with field-level errors.
- Unauthenticated requests return `401`; authenticated but unauthorized requests return `403`.
- Missing or unavailable active resources return `404` where exposing their existence would be inappropriate.
- Duplicate/rate-limited submissions return `429 Too Many Requests` with a retry hint where known.
- Unexpected service failures return a generic `500` response with a correlation ID; internal details are logged server-side, not exposed.
- Use cursor pagination; validate limits and cursors.

### Frontend components

- **Kudos composer:** searchable colleague picker, message input with remaining-character count, submit state, and accessible validation feedback.
- **Kudos feed:** paginated newest-first cards with sender/recipient names, message text, timestamp, loading/empty/error states.
- **Moderation controls:** visible only to authorized moderators; hide, restore, and delete actions include confirmation for deletion and optional reason entry.
- **Dashboard integration:** renders the feed using the authenticated portal context and responsive layout.
- Components must be keyboard accessible, label form controls, announce status/errors to assistive technology, and work on supported mobile and desktop widths.

### Security, privacy, and abuse prevention

- Enforce authentication, active status, sender identity, and moderator role on the server; UI visibility is not an authorization boundary.
- Use CSRF protection where required by the portal's session model, and secure transport for all requests.
- Treat user messages and moderation reasons as untrusted plain text; apply context-appropriate output encoding and request-size limits.
- Do not permit arbitrary HTML, script, or rich-text content.
- Enforce rate limits and duplicate detection atomically to reduce race-condition bypasses.
- Avoid exposing hidden/deleted message content through feed, error, cache, or search paths.
- Do not cache personalized or moderation-sensitive responses in shared caches. Invalidate feed caches after create/hide/restore/delete.
- Follow organizational privacy, retention, directory-access, and audit policies. Minimize retained data after deletion.

### Performance, reliability, and observability

- Use indexed cursor pagination and cap page sizes.
- Fetch directory display data in batches or through an approved cached directory interface; do not issue one directory request per feed item.
- Log creation and moderation outcomes, authorization failures, rate limiting, and dependency errors with request correlation IDs. Never log kudos message bodies or credentials.
- Monitor request latency, error rate, feed query latency, rate-limit counts, and moderation action counts without collecting message content.
- Make writes transactional across kudos state and moderation audit changes where they share a database.
- If the directory or database is unavailable, fail explicitly with a retryable response; do not report a successful write that was not persisted.

## Process flow

See [PROCESS_FLOW.md](./PROCESS_FLOW.md) for employee submission/feed and moderator hide/restore/delete flows, including validation and error branches.

## Implementation plan

1. Confirm approval and host portal identity/directory integration, database, API/UI conventions, and privacy/retention policy.
2. Add schema/migration for `Kudos` and append-only `ModerationAudit`, indexes, and constraints.
3. Implement authenticated recipient search and server-side recipient validation.
4. Implement kudos creation with message validation, duplicate/rate-limit enforcement, transactional persistence, and audit-safe logs.
5. Implement cursor-paginated visible feed and directory display-name resolution.
6. Implement moderator authorization and hide/restore/delete operations with audit events.
7. Implement accessible responsive composer, feed, and moderator controls.
8. Add unit, API/integration, authorization, abuse-limit, migration, accessibility, and error-path tests.
9. Review security/privacy, run CI, perform migration and rollback rehearsal, and deploy behind the portal's standard release controls.
10. Monitor after release and tune configurable limits based on approved policy.

### Dependencies

- Step 1 precedes schema and interface implementation.
- Step 2 precedes persistence API work.
- Steps 3-6 may proceed in parallel after identity and schema contracts are approved.
- Frontend work depends on stable endpoint contracts; integration tests depend on both API and UI flows.
- Production rollout depends on successful tests, security/privacy review, and migration/rollback readiness.

### Testing and deployment

- Unit-test validation, duplicate/rate-limit rules, visibility, ordering, pagination, and moderation audit records.
- Integration-test authenticated creation, directory status changes, hidden/deleted feed exclusion, unauthorized moderator actions, and transactional failure paths.
- Test malicious markup as inert text; test CSRF protections according to portal auth.
- Test responsive layouts and keyboard/screen-reader interaction.
- Deploy migration and application via the existing portal pipeline; use staged rollout/feature flag if supported, verify health metrics, and retain a rollback plan.

## Prototype implementation result

- **Selected stack:** Python standard library HTTP server, SQLite, and responsive browser UI.
- **Implemented:** employee search, authenticated kudos creation, visible feed with cursor pagination, database `is_visible`, moderator queue, hide/restore/delete, moderation audit events, message validation, configurable duplicate/rate limits, and controlled errors.
- **Development authentication:** environment-provided bearer tokens map to a seeded local employee directory. This is a local-demo adapter only, not portal SSO.
- **Validation:** `python -m unittest discover -s tests -v` passes 24 service and HTTP-level tests; Python compilation also passes.
- **Production work not claimed:** host-portal SSO/directory wiring, HTTPS deployment, organizational privacy/security review, and public repository publication require the host integration and repository details.

## Approval record

- **Status:** Approved for the local Python + SQLite prototype.
- **Approved by:** Requester, through the instruction to implement this specification.
- **Approval date:** 2026-10-30.
- The host portal's SSO/directory integration and production public-repository details remain out of scope for the local prototype and must be reviewed before production deployment or public publication.
