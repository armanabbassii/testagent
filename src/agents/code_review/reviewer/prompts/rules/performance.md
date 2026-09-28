## Performance Rules

Review the diff for the following performance issues:

### Database
- N+1 query problem: queries inside loops that should be batched
- Missing indexes on columns used in WHERE, JOIN, or ORDER BY
- SELECT * instead of selecting only needed columns
- Missing pagination on endpoints returning unbounded result sets
- Unnecessary eager loading of large relationships

### Algorithms & Data Structures
- O(n²) or worse algorithm where a better one exists
- Linear search on a collection that should be a set or dict for O(1) lookup
- Repeated computation inside a loop that can be moved outside
- Unnecessary deep copies of large objects

### Memory
- Accumulating large lists in memory that should be streamed or paginated
- Memory leaks from unclosed resources (files, connections, cursors)
- Large objects kept alive longer than necessary (e.g., in module-level globals)

### I/O & Network
- Synchronous blocking calls in an async context
- Missing connection pooling for database or HTTP clients
- Redundant API calls that can be cached or batched
- Missing timeouts on external calls