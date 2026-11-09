# Confirmation card

The card translates an informal request into a precise, bounded, verifiable
change. It is not a decorative restatement and it is not shown for every task.

## When it appears

- `light`: no card. Record the explicit choice with `select-work-profile` before
  the first write; the Hook still blocks an unselected write.
- `standard` or higher: show the five-column card before the first write.
- When approving a card task through the semantic bridge, pass the chosen
  `--work-profile`; the user approves scope, while the Agent owns process depth.
- New scope, shared components, changed target, changed allowed files, or a new
  requirement: update the card and ask again.
- Pure questions, searches, and read-only inspection: no card and no write task.

## Required five columns

| Column | Required content |
|---|---|
| `现象` | The current observable problem, using the user's words and facts from the actual code |
| `期望` | The concrete target state; for UI, include values such as alignment and spacing |
| `定位` | Real file/function/selector candidates found by read-only inspection |
| `边界` | What must not change, including allowed files and outside targets |
| `疑点` | The ambiguity and a recommended decision; write `无` only when genuinely unambiguous |

Do not guess an ambiguous target. Put the recommendation in `疑点` and wait for
confirmation. A user reply such as "对", "按这个来", or "批准修改这个范围" can
approve the current card in context; it does not approve a later expanded scope.

## Card families

| Card | Trigger | Purpose |
|---|---|---|
| Requirement card | A non-light task gate | Approve the interpreted goal and boundary |
| Five-column confirmation card | Standard/strict implementation, especially screenshot or short feedback | Freeze phenomenon, expected state, location, boundary, and doubts |
| Write confirmation card | A write is attempted while scope is unapproved | Stop the write and restate the exact scope |
| Dangerous-operation card | Destructive command or high-risk operation | Show the exact command and permit only the same input once |
| Permission settings menu | `/agent-approval` or `/设置` | Choose strict/standard/relaxed; it does not approve the current task |

`/agent-audit` is a status checklist, not an approval card and not proof that the
audit passed. It only reports pending, failed, stale, or collected evidence.

Hook cards are rendered by deterministic local rules. The Agent must not display
a second generic card after the Hook has already shown one.
