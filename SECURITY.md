# Security and privacy

Never put API keys, tokens, SSH configuration, publisher cookies, signed URLs,
private network addresses, research databases, or downloaded papers in an Issue
or pull request. Revoke a credential immediately if it was disclosed; deleting
a file from a later commit does not remove it from history.

Use GitHub's private vulnerability reporting/advisory feature when available.
If it is unavailable, open an Issue with a minimal nonsensitive description and
request a private reporting channel. Do not include exploit secrets or private
deployment logs in a public report.

The server restricts network endpoints, redirects, file identity and size; it
does not make arbitrary downloads or challenges safe. Review host allowlists,
API permissions and background job state before extending or upgrading it.
