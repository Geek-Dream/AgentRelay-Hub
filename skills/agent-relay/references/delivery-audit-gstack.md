# Delivery audit reference: gstack

AgentRelay 的交付审计流程参考了 **gstack** 的公开实现，未安装或执行其安装脚本、
编译工具、Hook、遥测或 Provider。这里保存的是流程设计说明，不是 gstack 的发行副本。

- Upstream: https://github.com/garrytan/gstack
- Reference revision observed through GitHub: `549b9a86609474e7218b4c82fdfcc5aba8b820fa`
- Reference material downloaded through the public GitHub API:
  - `review/SKILL.md`: scope drift, complete diff review, exploratory QA, revalidation and review records.
  - `review/sections/plan-completion.md`: DONE/PARTIAL/NOT DONE/CHANGED/UNVERIFIABLE classification and concrete evidence.
  - `qa/SKILL.md`: reproduce, regression before repair, minimal fix, re-test and final QA.
  - `ship/SKILL.md`: test, coverage, plan completion, review and final verification gates.
  - `docs/qa-without-a-webpage.md`: real CLI/API/job/webhook contracts without forced browser checks.
  - `LICENSE`: MIT License, Copyright (c) 2026 Garry Tan.

## Adapted rules

1. Do not equate touched files or a green test run with requirements delivered.
2. Compare user intent with the actual diff; record missing work and unrequested behavior.
3. Run real behavior checks for the affected surface. Browser screenshots apply to visual work,
   not as a substitute for API/CLI/job checks. Unavailable or unrun checks stay visible.
4. Review after edits, then revalidate the final content. New edits invalidate older evidence.
5. Independent review is optional infrastructure, not a claim. Record only an actual provider result;
   otherwise finish a local adversarial review and disclose the missing independent coverage.
6. Keep evidence scoped to the current task and workspace. Do not copy gstack's telemetry,
   global auto-update, external review defaults or host installation behavior.

The Python implementation in `scripts/delivery_audit.py` is an independent, small adaptation
for AgentRelay. It does not import or execute gstack code. The original MIT notice is retained below.

## gstack MIT License

MIT License

Copyright (c) 2026 Garry Tan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
