# handbook

A one-page dashboard over a folder of markdown notes. Your task board, your Linear tickets in plan order, your pull requests with their CI state, your deals with their next step, and a questions inbox, on one screen. One Python file, standard library only, nothing to install.

Status: alpha. It works for one person every day. Expect churn.

## Quickstart

```bash
git clone https://github.com/AlexHelo/handbook
cd handbook
python3 server.py --vault starter
```

Open http://127.0.0.1:4322. That is the bundled starter vault, with every integration off. Point `--vault` at your own folder of markdown once it follows the contract in `AGENTS.md`.

## Configure

Drop a `handbook.json` in your vault root. Every key is optional:

```json
{
  "owner": "Sam",
  "repos": ["you/your-repo"],
  "repos_git": [".", "../another-repo"],
  "domain_tones": {"10-Company": "#C08A3E"},
  "integrations": ["github"]
}
```

Private extras: a `<vault>/.dashboard/extras.py` with an `anchor(config)` function returning HTML is rendered in the day header. That is where anything personal lives that does not belong in a public repo.


Integrations are opt-in per vault: `"integrations": ["attio", "linear", "github"]`. Attio reads `ATTIO_TOKEN` or `~/.attio-token`, Linear reads `LINEAR_API_KEY` or `~/.secrets/linear-api-key` and shows the issues assigned to you or in projects you lead, GitHub uses `gh auth login`.

## Security

No auth, by design. It binds to localhost, refuses form posts from any other origin, and writes every file atomically under one lock. Run it behind Tailscale if you want it on your phone.

## The loop it belongs to

The dashboard only reads and renders. What writes the notes is up to you: your own hands, a script, or a coding agent that reads `AGENTS.md`. The starter vault shows the shapes it expects.

Maintained casually. No roadmap promises. MIT.
