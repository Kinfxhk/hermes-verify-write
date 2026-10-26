# verify-write

**A Hermes Agent plugin that makes silent no-op writes visible.**

An agent tool returning success is not the same as the external system having changed.
`DOM.setFileInputFiles` reports ok with `input.files.length === 0`. A dashboard shows
"Changes saved" while the draft was never written. `gh api PUT` answers `200` and nobody
reads the body back. The next turn then reports work that does not exist.

`verify-write` watches the mutations it understands, reads the target back immediately,
and puts anything that did not check out **in front of the model on its next turn** —
where it cannot be skipped.

```
[verify-write] 1 action(s) this session did NOT read back clean:
- FAILED write_file /srv/app/config.yaml (15:31:07): sha mismatch on read-back (cba06b57 != 11507a0e)
(3 other write(s) verified ok)
Do not report these as done until you have re-checked them yourself.
```

## Install

```bash
hermes plugins install Kinfxhk/hermes-verify-write      # from GitHub
hermes plugins enable verify-write
```

Or drop the directory into `~/.hermes/plugins/verify-write/` and restart the gateway.
No dependencies, no env vars, no tools registered — two hooks and one slash command.

Check it loaded: `hermes plugins list` (look for `verify-write`), then `/verify-write`
in any chat to print the ledger (`/verify-write <session-id>` narrows it).

## What it checks

| Mutation | Read-back |
|---|---|
| `write_file`, `patch` | file exists; for `write_file` the sha256 of `content` matches the file on disk |
| `gh api` with `-X PUT/POST/PATCH/DELETE` (or `-f`/`-F`) | `GET` the same path; verdict carries the returned `sha` / `html_url` as proof |
| `curl -X PUT/POST/PATCH/DELETE <url>` | `GET` the URL, expect 2xx |
| `adb install <file.apk>` | `aapt2 dump badging` → package id + versionCode, then `dumpsys package` on the device |
| `git -C <dir> push` | `rev-parse HEAD` + `status -sb`; flags a branch still ahead of upstream |

Verdicts:

- **verified** — the change is visible at the target. Silent, no token cost.
- **failed** — the target disagrees with the claim. Surfaced next turn.
- **unverified** — the probe could not run (missing binary, no path in the command,
  GraphQL write). Surfaced next turn too: an honest "I could not check this" beats
  a silent assumption.

## What it does not see

Deliberate scope. Everything here is invisible to the plugin:

- Anything not in the table above, including `gh release create`, `gh pr create`,
  `docker push`, `systemctl`, package managers, and file writes done through `terminal`.
- Writes performed inside a third-party tool call (`mcp__*`, other plugins' tools).
- `git push` invoked without `git -C <dir>` — the hook has no cwd, so it records
  `unverified` rather than guessing the repo.

If a probe would be slow or ambiguous, it is reported as unverified, never as verified.

## Design notes

- **Hooks only.** `post_tool_call` is an observer; nothing the plugin does can change or
  block a tool call. The only place it speaks is `pre_llm_call`, which appends context
  to the next user message.
- **Never raises.** Every hook body is wrapped: a broken verifier must not break the turn.
- **Bounded.** 60 entries per session, 8 s per probe, context capped at the six most
  recent problems. A clean session injects nothing at all.
- **No LLM.** Every verdict is a deterministic read-back; there is nothing to hallucinate.

## Development

```bash
python3 -m unittest discover -s tests -v     # 19 tests, no Hermes import, no network
hermes plugins validate .                    # manifest + capability probe
```

## License

MIT
