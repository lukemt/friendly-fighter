@AGENTS.md

## For Claude Code in particular

- **Your auto-memory is per folder.** A family session in `.worktrees/<family>` doesn't see the
  coordinator's memory, and the coordinator doesn't see its memory. Anything others need goes into
  the repository instead: the family's README, a `lab note`, or (through the coordinator)
  `docs/STRATEGY.md`. Memory is for your own working notes only.
- **`.claude/settings.json` denies `git push` and `pkill -f`.** When the user wants a push, they
  run it themselves (`! FF_ALLOW_PUSH=1 git push ...`).
- **Other Claude sessions on this machine can be messaged** (SendMessage / ListAgents). The
  coordinator uses this to announce engine rebuilds and champion changes. Codex sessions can't
  receive messages: write anything everyone must know as a `lab note` too.
- Don't spawn subagents to run lab experiments. Lab runs are already parallel, inside the game
  slots.
