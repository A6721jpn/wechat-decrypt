# Per-message local media manifest proposal

Status: accepted direction for [issue #6](https://github.com/tzwkb/wechat-decrypt/issues/6),
pending verified message-to-resource fixtures. This document specifies a proposed
interface; transcript-to-image association is not implemented yet.

## First increment

Export a local JSON sidecar for the selected conversation and date window. Each
image entry identifies the account snapshot, conversation/table, database shard
and local message ID; include the server ID when present. A local ID can repeat
across both shards and conversation tables. A server ID is optional and cannot
serve as the sole identity.

An asset entry contains its kind and one of `resolved`, `not_found` or
`encrypted_unsupported`. A resolved entry has a path relative to the export root.
Unresolved entries keep the transcript placeholder and report a reason. An
ambiguous mapping stays unresolved; it must not choose the first candidate.

TXT can append the relative path. Markdown/HTML links or previews can follow once
the mapping and sidecar are validated. The resolver must use a demonstrated
resource-index relationship. A matching filename or an XML MD5 alone does not
establish that relationship. Export only assets selected by this conversation and
date window, retaining distinct destinations for same-named files.

Keep the manifest local. Exclude raw database or image keys, authentication
fields, CDN URLs and raw packed resource payloads. The first increment does not
decode encrypted image originals or trigger media downloads.

## Evidence needed before implementation

Provide minimal table definitions and synthetic rows showing the relationships
between message identity, resource-index identity and an existing local asset.
Record the WeChat version and platform from which those relationships were
verified, without including private messages, account identifiers or key material.
The public implementations inspected so far use filename/MD5 heuristics for
images and do not supply a verified fixture for this repository's resolver.

Acceptance cases must cover:

- A known message resolves to the correct local image through the index.
- Equal local IDs in different shards or conversations remain distinct.
- An absent server ID can still be handled using the full local identity.
- Missing, encrypted, ambiguous and unsupported resources remain explicit.
- Equal basenames retain distinct exported files; paths cannot escape the export root.
- Conversation/date filtering excludes unrelated assets, and secrets stay absent.

Keep issue #6 open until the mapping fixtures, resolver and transcript links pass
these cases. The attachment fixes in PRs #5 and #10 are useful prerequisites but
do not complete this feature.
