# Security Policy

AI Memory OS is a local-first project memory system.

## Security model

- Private project memory should remain outside the source repository.
- Secrets and credentials must never be stored in memory files.
- Runtime state and source code should remain separated.
- Tests must use temporary memory roots.
- Live promotion requires an explicit target path and an exact second confirmation.
- Network or cloud behavior must not be introduced silently.

## Reporting security issues

Use GitHub private vulnerability reporting when available.

Do not place credentials, customer information, private project history, production databases, private keys, or sensitive reproduction artifacts in public issues.

## Sensitive data

Never commit real:

- API keys
- authentication tokens
- passwords
- private keys
- customer documents
- production configuration
- project checkpoints
- private project histories
