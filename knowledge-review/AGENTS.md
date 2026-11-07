## Architecture Navigation

Read documentation selectively based on the task.

- Cross-module architecture changes:
  Read docs/project-context/ARCHITECTURE.md.

- Changes to agent execution, retrieval, grading,
  or review workflows:
  Read docs/project-context/CALL_CHAINS.md.

- Changes that replace an existing design decision:
  Read docs/project-context/ARCHITECTURE_DECISIONS.md.

Maintain the three markdown files after making changes.
Do not read all architecture documents by default.

## Engineering Constraints
- Prefer small, reviewable changes.
- For complex changes, propose an implementation plan first.
- Avoid unrelated refactors.
- Do not weaken existing tests to make changes pass.
- Explain unexpected deviations from the agreed design.
- Do not commit or push without explicit authorization.