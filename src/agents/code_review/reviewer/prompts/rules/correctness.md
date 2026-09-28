## Correctness Rules

Review the diff for the following correctness issues:

### Logic Errors
- Off-by-one errors in loops, slices, or index access
- Incorrect boolean logic (wrong operator, inverted condition)
- Wrong comparison (== vs is, = vs ==)
- Unreachable code or dead branches

### Null / None / Undefined Handling
- Missing null checks before dereferencing
- Uncaught exceptions from operations that can fail (file I/O, network, parsing)
- Functions that may return None used without checking

### Concurrency
- Race conditions on shared mutable state
- Missing locks or incorrect lock usage
- Deadlock potential from lock ordering

### Data Integrity
- Missing input validation before processing or persisting
- Incorrect type coercion that silently truncates or corrupts data
- Missing transaction boundaries around multi-step database operations
- Inconsistent state left behind on error paths

### Edge Cases
- Empty collection not handled (first/last element assumptions)
- Integer overflow or underflow
- Floating-point precision issues in comparisons or financial calculations
- Timezone-unaware datetime handling