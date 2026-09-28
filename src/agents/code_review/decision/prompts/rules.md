## Decision Rules

Apply the following rules strictly and in order:

| Condition                          | Decision      |
|------------------------------------|---------------|
| Any `critical` issue exists        | `reject`      |
| 3 or more `major` issues           | `reject`      |
| 1–2 `major` issues                 | `needs_work`  |
| Only `minor` or `suggestion` items | `approve`     |
| No issues at all                   | `approve`     |

### Additional Guidance

- A single `critical` security issue always results in `reject`, regardless of other findings.
- When deciding `needs_work`, list the `major` issues clearly so the author knows exactly what to fix.
- When deciding `approve`, briefly acknowledge the quality of the work even if minor notes exist.
- Do not penalize `suggestion`-level comments — they should never block a merge.