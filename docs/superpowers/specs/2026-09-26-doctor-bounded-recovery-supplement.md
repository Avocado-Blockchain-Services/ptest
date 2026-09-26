# Doctor bounded review recovery supplement

This supplement changes only the private per-item model review protocol. The
public `ptest.agent-assessment/v1` report schema and offline behavior remain
unchanged.

Each model-reviewed item may make an initial call and at most one follow-up.
For a valid initial reply, the follow-up independently verifies that reply and
may include bounded reserve evidence requested by valid source IDs. For a
completed initial reply rejected by protocol, schema, or source-ID validation,
the follow-up is one fresh judgment: it receives the original selected source
units and IDs with fixed metadata, no malformed draft, and must return empty
`needs`. The implementation does not repair IDs, preserve invalid prose, or
silently change the initial evidence selection.

Provider or tool failures, cancellation, deadlines, and resource or output
limits are not retried. An invalid follow-up ends as `unknown`; no third call,
new source collection, or deadline reset is allowed. The disclosure call bound
remains one initial call plus at most one follow-up for each model-reviewed
item.

## Retrieval and request scope

Source collection remains automatic, suite-aware, static, and subject to the
existing per-child, per-file, per-item, and traversal bounds. Python support
may follow a selected caller's direct literal import or re-export, including a
function-local import, when the binding is used by that selected owner. A
module-level import is followed only when that particular selected function
loads the name without a local binding shadow; a direct local import replaces
the same-named module binding for that function. Shadowing is evaluated per
selected function, so one function's parameter or assignment cannot suppress a
separate selected function's valid module import. A non-autouse fixture
contributes only when the shown caller or fixture chain requests it. Duplicate
module or function-local imports, and conditional re-imports of the same name,
remain ambiguous and are not followed. Dynamic, conditional, ambiguous,
shadowed, excluded, or over-budget edges remain unresolved with their omission
reason; they do not authorize a broader scan.

For network ranking, a bounded same-test source pattern may connect an
unshadowed imported runtime client, an injected local request function, a
bound client method passed to a called helper, and a positive request
assertion showing an HTTP method and path. Literal-table `it.each` callbacks
are supported. This pattern only helps select evidence; it does not establish
a verdict, evaluate template strings, or follow arbitrary imports. Comments,
type-only imports, unconsumed clients, disconnected assertions, shadowed
bindings, and conditional client construction do not establish the connection.

The request metadata is projected after the final source-unit budget shrink.
It retains selected-unit roles, outgoing dependency edges and their missing
reasons, deciding configuration and suite facts, coverage counts, reserve
inventory, and explicit item or collector budget cuts. Unselected context
roles, incoming edges, and unrelated missing paths are summarized as counts
by reason without disclosing their path inventory. Child-relative paths are
normalized against the declared child before projection.

## Assessment boundary

Assess the concrete operation and ownership boundary shown by reachable
callers. Examples support only their demonstrated scope. A read-only shared
object identity, configured path, connection field, or environment variable
alone does not show mutation, allocation, initialization, or deletion. A gap
requires a cited concrete violation; a missing fact matters only when it is
decisive for a shown operation. Preserve contrary operations, including
timeout and cancellation cleanup. A fresh instance, ordinary SQLite use,
per-instance map, managed temporary root, or in-process fake is assessed only
for the callers and owners it actually serves.
