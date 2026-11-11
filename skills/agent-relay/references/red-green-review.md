# RED to GREEN evidence

Logic changes at `standard`, `strict`, or `user_required` use observable
RED-to-GREEN evidence. Pure copy, color, spacing, and other non-behavioral style
changes may skip RED, but still need their matching target/boundary checks.

## Evidence sequence

1. Establish the audit before the real write. The Hook does this after all gates
   allow the tool and before the tool runs; the stored baseline therefore
   represents the unmodified workspace.
2. Record RED on that exact baseline. RED must be a real non-zero test/probe
   result, must execute tests when `kind=test`, and must not change the workspace.
3. Perform the minimal write.
4. Refresh the candidate fingerprint after the successful write.
5. Record GREEN on the current candidate. GREEN must be zero exit, execute at
   least one test when `kind=test`, and bind to the candidate fingerprint.
6. Run self-review and independent review on that candidate. The first review
   freezes `review_fingerprint`; any later file change makes the review stale.

The CLI exposes the phase directly:

```bash
CODEX_ROOT="${CODEX_HOME:-$HOME/.codex}"
PYTHON="$CODEX_ROOT/agentrelay-env/bin/python"
AUDIT="$CODEX_ROOT/skills/agent-relay/scripts/delivery_audit.py"

"$PYTHON" "$AUDIT" run-behavior --home "$CODEX_ROOT" \
  --session "<session_id>" --phase red --kind test \
  --cmd "$PYTHON -m unittest tests.test_the_failing_behavior"

# Make the minimal implementation change here.

"$PYTHON" "$AUDIT" run-behavior --home "$CODEX_ROOT" \
  --session "<session_id>" --phase green --kind test \
  --cmd "$PYTHON -m unittest tests.test_the_failing_behavior"
```

On Windows PowerShell, set `$CODEX_ROOT` and `$PYTHON` as shown in the main
`SKILL.md`, then invoke the interpreter with `& $PYTHON`.

Never invent RED after the code was already changed. If the original baseline is
gone, report the missing RED as a verification gap instead of fabricating it.

## Canonical scope drift

An audit owns a frozen canonical requirement list. The default is the original
request as `R1`; before the first write, a multi-part task should record each
original requirement as a stable ID.

Requirement evidence must use that ID:

```json
[
  {
    "requirement_id": "R1",
    "item": "The behavior the user asked for",
    "status": "DONE",
    "evidence": "Test output and file/line evidence"
  }
]
```

Allowed statuses:

- `DONE`: implemented and supported by current evidence.
- `CHANGED`: intentionally adjusted with the user's explicit agreement.
- `PARTIAL`: some behavior exists but the full requirement is not met.
- `NOT_DONE`: missing or explicitly deferred.
- `UNVERIFIABLE`: current evidence cannot establish the result.

A passed requirements check must cover every canonical ID exactly once and may
only contain `DONE` or `CHANGED`. `PARTIAL`, `NOT_DONE`, and `UNVERIFIABLE` remain
visible and block clean completion. Scope cannot be shrunk after the first write.
