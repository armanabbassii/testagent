## Project Code Review Rules

Apply these rules in addition to the standard review criteria:

### Language
- Write all review comments in **Persian (Farsi)**
- Use technical English terms where no standard Persian equivalent exists (e.g., "refactor"، "cache"، "endpoint")

### Comment Style
- Be concise — one short paragraph per issue, maximum 3 sentences
- For obvious issues, skip the explanation and state only what to fix
- For complex issues, include a brief explanation and a concrete suggestion

### Severity Guidance
- Mark as `critical` only genuine security vulnerabilities or data-loss risks
- Do not mark style issues as `major` — use `minor` or `suggestion`

### References
- For `critical` security issues, add a reference link if one exists (OWASP, CVE, docs)
- Example: `برای اطلاعات بیشتر: https://owasp.org/...`