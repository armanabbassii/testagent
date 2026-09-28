## Python-Specific Rules

Apply these in addition to general review rules when reviewing Python files:

### Style
- Enforce PEP 8 — flag lines over 100 characters, missing blank lines between functions
- Flag missing type hints on public functions and class methods
- Flag use of `print()` in non-script code — suggest `logging` instead

### Correctness
- Flag mutable default arguments: `def f(x=[])` — suggest `def f(x=None)`
- Flag bare `except:` — must catch specific exceptions
- Flag `== None` comparisons — use `is None`

### Performance
- Flag list comprehensions inside loops that create unused lists
- Flag repeated `str.split()` or `str.replace()` on the same string in a loop