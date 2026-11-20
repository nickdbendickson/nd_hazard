# Security Policy

## Reporting a vulnerability

Please report security problems **privately**, not in a public issue or pull
request.

Use GitHub's private vulnerability reporting for this repository:
**Security → Advisories → Report a vulnerability**
(<https://github.com/nickdbendickson/nd_hazard/security/advisories/new>).

Include the version or commit, which plugin, the platform, and the steps to
reproduce. You'll get an acknowledgement, and a fix or an explanation of why
it is not a problem.

Please do not publish details until a fix is available.

If the private advisory form is not available, contact the maintainer
privately rather than opening a public issue.

## Scope

The plugins in this repository run inside QGIS. Data a user opens or imports
— QGIS project files, dam files, offline data packs, DSS-WISE packages, zip
archives, and other shared records — is treated as untrusted input. Reports
that matter most involve code execution, reading or writing files outside a
plugin's intended output folders, following a solver or executable path from
an untrusted project or pack, or unpacking a malicious zip or DSS-WISE
package.

## Secrets

Never commit API keys, tokens, or other credentials. If one is exposed, rotate
it at the provider immediately — rotation, not deletion from git history, is
what makes an exposed credential safe.
