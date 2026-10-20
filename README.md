# QQ AI Companion / QQ AI群友

A local-first, opt-in QQ group AI companion built with Python, SQLite, an AstrBot plugin and OneBot-compatible QQ transport (e.g. NapCat). The companion is visibly identified as AI and can choose **reply, wait or remain silent**.

> **Development-stage project.** The original bot's private QQ logins, database, group IDs, saved chat sessions and 20 historical commits are **not included**. This public repository starts from a reviewed, sanitized snapshot. No official endorsement from QQ, AstrBot, NapCat or OpenAI is implied.

## What it does

- Group allowlists and human-controlled send/intake switches. All switches default off.
- Direct replies, short-term follow-up and low-frequency ambient participation. Nondirected participation shares quiet hours, cooldown and hourly budget.
- Combine consecutive same-speaker messages before planning; maintain per-group context and optional opt-in memory.
- SQLite request lifecycle: pending → generating/ready → sending → sent/uncertain; durable attempts and no blind replays of uncertain work.
- Optional fixed-agent task relay (`companion/agent_queue.py`, `companion/multica_bridge.py`) using a single worker and `run_only` automation; this is not installed or automatically enabled.
- Human-in-the-loop `manual_web` reply flow by default. Optional provider adapters are separate and require valid provider authorization and terms compliance.

## Requirements

- Python 3.11 or newer. The core local bridge uses Python's standard library.
- An existing AstrBot + OneBot-compatible QQ transport, installed and configured **separately**. No QQ credentials, binaries or login sessions are supplied.
- Optional `pip install -r requirements.txt` for the OAuth/OIDC provider's signature validation.

## Start locally

```powershell
python -m companion.cli init
python -m companion.server --port 18761
```

Open `http://127.0.0.1:18761/` from the same computer. Obtain the local bridge token through your own `python -m companion.cli local-token` command; never paste it into GitHub. See `astrbot_plugin_qq_gpt_companion/README.md` for plugin installation.

1. Keep `config.json` private (the template is `config.example.json`). Configure only a group whose members know about the bot.
2. Put the same allowlist in the AstrBot plugin. Keep `allow_outbound_sends=false` until the full model and QQ send chain has been tested and approved.
3. Default model mode is `manual_web`, requiring an operator to enter a reply in the local dashboard. A successfully generated *draft* is not proof of a QQ delivery.
4. If testing automatic providers, set `QQ_COMPANION_TEST_GROUP` in the *process environment* to the specifically approved test group. No actual QQ IDs are hardcoded in this publication. Always test with synthetic fixtures first.

## Model providers and account usage

- **Manual browser workflow** works without automating the consumer ChatGPT website. Do not scrape, extract or repackage its output in violation of service terms.
- **Official OAuth / Sign in with ChatGPT**: `companion/siwc_auth.py`, `companion/siwc_worker.py` are experimental code for *eligible and permitted clients*. Registration/plan entitlements are not assumed; the user must explicitly authorize the application in their own browser. The adapter does not read stored browser or Codex CLI tokens, and remains disabled until an authorized model is tested. See [OpenAI's official documentation](https://developers.openai.com/siwc/token-sharing-open-source) for current eligibility/limitations.
- **Optional Web2API research adapter** is provided for independent examination, disabled by default. Do not connect it to services whose conditions prohibit automated extraction. This is **not** a way to turn subscriptions into unlimited APIs.
- **Multica**: an independent fixed-agent adapter; does not create an issue per message or modify globally shared runtimes. You must supply your own authorized runtime and `MULTICA_LUNA_RUNTIME_ID`, and any required task-scoped permissions.

## Tests

```powershell
python -m unittest discover -s tests -q
```

Tests are synthetic (temporary local DBs and loopback SSE fixtures); they do **not** send QQ messages or consume a real model account. Runtime secrets, user-specific group numbers and historic chat content are deliberately excluded.

## Privacy, contributions and license

Do not upload real QQ group chats, token screenshots, account cookies or local provider profiles in issues or PRs. See [SECURITY.md](SECURITY.md).

Licensed under [MIT](LICENSE). Forks and commercial reuse are allowed under the license terms. Copyright/attribution obligations remain. Contributions are welcome but not guaranteed a response.

## Behavior balance sheet and social behavior roadmap

The existing local dashboard at http://127.0.0.1:18761/ now contains a live **16-knob behavior balance sheet**, with numeric inputs, sliders, three safety-bounded presets (quiet / normal / active) and explicit Save / Restore buttons. These tune real social decisions: ambient sampling, follow-up probability, quiet hours, consecutive message coalescing, stale ambient replies, context size and Codex CLI quotas. Changes use a versioned, Token-protected local endpoint and cannot enable sending, grant a model entitlement, change groups or expose credentials.

The official Codex CLI opt-in worker is available as companion/codex_cli_worker.py, using the machine's own supported ChatGPT sign-in with ephemeral read-only executions. It is not a credential-to-API conversion proxy. Automatic group delivery remains independently gated by AstrBot and the local outbound allowlist. Set QQ_COMPANION_TEST_GROUP to your own *approved* test group in the process environment; none is embedded in the source.

See docs/HUMANIZATION_ACCEPTANCE.md for a realistic feature-by-feature audit against the Tulpa/MaiBot-inspired goals. Natural-language social quality, semantic topic-shift planning, per-group persona customization and multi-bubble native QQ quoting still require more development and consented real-world evaluation.
