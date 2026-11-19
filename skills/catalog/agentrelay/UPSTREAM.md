# Bundled third-party Skills

These directories are vendored snapshots, not live checkouts. Each skill keeps
its upstream license in its own `LICENSE` file. Integrity counts and SHA-256
values are declared in `scripts/skill_catalog.py`.

## Plinth

- Repository: https://github.com/jabrena/plinth
- Pinned commit: `ef907871d4bd09825bba56f6585ed227a9179727`
- License: Apache-2.0
- Upstream paths:
  - `skills/121-java-object-oriented-design`
  - `skills/122-java-type-design`
  - `skills/301-frameworks-spring-boot-core`
  - `skills/302-frameworks-spring-boot-rest`
  - `skills/303-frameworks-spring-boot-validation`
  - `skills/312-frameworks-spring-data-jdbc`

The Java/Spring skills are bundled as reference guidance. They do not replace
project-specific architecture tests, compilation, linting, or human review.
`312-frameworks-spring-data-jdbc` is optional because it is specific to Spring
Data JDBC rather than a universal persistence recommendation.

## Code Review Excellence

- Repository: https://github.com/wshobson/agents
- Pinned commit: `42203085cfc5736204bf0f6d436dff7e1da0bf3f`
- License: MIT
- Upstream path: `plugins/developer-essentials/skills/code-review-excellence`

The review skill supplies review checklists and communication guidance; it is
not a substitute for executable quality gates.
