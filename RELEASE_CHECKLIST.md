# AgentRelay Release Checklist

## Repository safety

- [ ] `git status` only lists intended release files
- [ ] `git ls-files` contains no private runtime data
- [ ] No personal absolute paths
- [ ] No login state or browser storage state
- [ ] No historical chats
- [ ] No API token or other secret
- [ ] No Cookie data
- [ ] No logs, sessions, backups, caches, or raw Hook payloads

## Installation

- [ ] `install.py` passes an isolated first-install test
- [ ] Existing `hooks.json` entries are preserved during merge
- [ ] `CODEX_HOME` and default `$HOME/.codex` paths are documented
- [ ] Installed shell Hook is executable
- [ ] Manual Provider login and CAPTCHA requirements are documented

## Static checks

- [ ] All Python files pass `py_compile`
- [ ] `hooks/agent_relay_hook.sh` passes `sh -n`
- [ ] `examples/hooks.json.example` passes `json.tool`
- [ ] GitHub Actions CI passes

## Documentation and release

- [ ] README is complete
- [ ] LICENSE is present
- [ ] Core trigger-chain compatibility has been preserved
- [ ] Final staged diff has been reviewed by a human
- [ ] No commit or push occurs before human approval
