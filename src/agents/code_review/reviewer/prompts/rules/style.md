## Style & Maintainability Rules

Review the diff for the following style and maintainability issues:

### Naming
- Variable, function, or class names that are unclear, misleading, or too abbreviated
- Inconsistent naming conventions within the same file or module
- Magic numbers or strings that should be named constants

### Function & Class Design
- Functions longer than ~40 lines that should be broken down
- Functions doing more than one thing (violating Single Responsibility)
- Deep nesting (more than 3 levels) that should be flattened with early returns
- God classes that accumulate unrelated responsibilities

### Code Duplication
- Copy-pasted blocks that should be extracted into a shared function
- Similar logic in multiple places that should be parameterized

### Comments & Documentation
- Missing docstring on public functions, classes, or modules
- Comments that describe *what* the code does instead of *why*
- Outdated comments that no longer match the code
- Commented-out code left in the diff

### Error Handling
- Bare `except:` or `except Exception:` that swallows all errors silently
- Error messages that expose internal stack traces to end users
- Missing logging for errors that are caught and suppressed

### Dependencies
- Importing an entire module when only one function is needed
- Circular imports introduced by the change
- New dependency added without being declared in the project manifest