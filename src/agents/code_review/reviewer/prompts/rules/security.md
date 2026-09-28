## Security Rules

Review the diff for the following security issues:

### Injection
- SQL injection: user input passed directly to queries without parameterization
- Command injection: unsanitized input passed to shell commands or `eval()`
- XSS: unescaped user input rendered in HTML responses

### Authentication & Authorization
- Missing authentication checks on sensitive endpoints
- Broken access control — user can access other users' data
- Insecure direct object references (IDOR)

### Secrets & Configuration
- Hardcoded API keys, passwords, tokens, or private keys in source code
- Secrets committed to version control
- Sensitive data logged to console or log files

### Cryptography
- Use of weak or deprecated algorithms (MD5, SHA1 for passwords, DES)
- Incorrect use of cryptographic primitives (e.g., static IV, ECB mode)
- Passwords stored in plaintext or with reversible encryption

### Dependencies & Deserialization
- Use of known-vulnerable library versions
- Unsafe deserialization of untrusted data (pickle, YAML load, XML)

### Data Exposure
- Sensitive data (PII, financial) returned in API responses unnecessarily
- Missing or overly permissive CORS configuration