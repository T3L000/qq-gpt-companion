# Security and privacy

Please report suspected vulnerabilities privately through GitHub's **Report a vulnerability** feature if available. Do not file a public issue containing tokens, cookies, personal QQ identifiers or chat logs.

This repository is a **source-only** publication, not a copy of the developer's live bot. Do not commit `config.json`, `.env`, `data/`, `backups/`, `services/` or third-party login state. The default configuration disables message intake, outbound delivery and model automation.

On your own deployment, use a dedicated local bridge token, strict single-group allowlist while testing, and keep the bridge bound to `127.0.0.1`. Only AstrBot should send QQ messages. Unknown delivery outcomes must not be blindly resent.

Never share ChatGPT browser cookies or Codex CLI login state. The optional OAuth provider requires supported app eligibility and interactive user permission. A ChatGPT subscription is not an unrestricted API key.

Before publishing logs or bug reports, remove member IDs, group IDs, session data, prompts containing private messages and local machine paths.
