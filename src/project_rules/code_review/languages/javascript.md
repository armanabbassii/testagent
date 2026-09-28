## JavaScript / TypeScript-Specific Rules

Apply these in addition to general review rules when reviewing JS/TS files:

### Style
- Prefer `const` over `let`; flag use of `var`
- Flag missing semicolons in non-ESM projects
- Flag missing TypeScript types on function parameters and return values

### Correctness
- Flag `==` comparisons — use `===`
- Flag unhandled Promise rejections (missing `.catch()` or `try/catch` around `await`)
- Flag direct DOM manipulation in framework components (React/Vue) — use state

### Security
- Flag `innerHTML` assignments with unsanitized user input
- Flag `eval()` usage