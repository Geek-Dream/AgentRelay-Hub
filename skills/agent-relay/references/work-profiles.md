# Work profiles

Every task that may write a workspace file selects one process profile, including
`light` tasks that do not show a user card. Pure questions, searches, and
read-only checks do not select a profile and do not create an audit.

The main Agent owns the choice. Hook rules compute a minimum so a difficult task
cannot silently use a cheaper process. The Agent may choose a stronger profile
than the minimum, but must never choose a weaker one.

| Profile | Use when | User-visible card |
|---|---|---|
| `light` | One clear, local, low-risk change with an obvious check | No |
| `standard` | Normal implementation work with requirements, diff, behavior, and review evidence | Yes |
| `strict` | Cross-file, shared, high-risk, ambiguous, reference-driven, or strict-policy work | Yes |
| `user_required` | The user explicitly asks for full review, full regression, or every item to be checked | Yes |

Minimum rules:

- Read-only work: no profile.
- Simple local write: at least `light`.
- Multiple files, multiple domains, shared/global impact, UI reference drift,
  or strict permission mode: at least `strict`.
- Explicit complete review or full test request: `user_required`; it cannot be
  downgraded by the Agent or by the host permission mode.

The selected profile controls evidence depth, not permission to write. The
normal task gate, dangerous-command gate, change contract, and user approval
remain independent.

For a non-card task, record the choice before the first write:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/hooks/agent_relay_tracker.py" \
  select-work-profile --session "<当前真实 session_id>" --profile light \
  --reason "单文件、低风险、单点样式修改"
```

For a card task, pass the same decision with the semantic approval:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/hooks/agent_relay_tracker.py" \
  resolve-intent --session "<当前真实 session_id>" --decision approve \
  --source-sha256 "<当前消息摘要>" --work-profile standard \
  --reason "用户明确批准当前范围"
```

The Hook blocks the first write until the Agent has recorded a profile. It never
asks the user to choose one, and a missing or damaged audit baseline also blocks
the write instead of silently continuing. If a task starts simple and later
expands, stop, update the five-column card, and require confirmation on the
larger scope.
