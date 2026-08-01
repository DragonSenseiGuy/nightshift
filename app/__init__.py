"""The v1 user interface (Phase 11).

`app.service` is the whole of it: a UI-agnostic layer that knows how to read the night's
state, trigger a run, open the briefing and drive the approval queue. `app.menubar` is a
thin rumps rendering of that service and is the *only* module here that imports rumps or
touches AppKit — so the Phase 17 SwiftUI client can be written against `app.service` (or a
localhost shell over it) without re-deriving a single rule.

Nothing in this package performs a side effect of its own: approving calls
`approvals.ApprovalQueue.approve`, which is where security rule 3 lives.
"""
