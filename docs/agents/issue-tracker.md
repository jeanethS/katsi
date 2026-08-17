# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues. Use the `gh` CLI for all operations.

## Conventions

- Create an issue with `gh issue create --title "..." --body "..."`.
- Read an issue with `gh issue view <number> --comments`.
- List issues with `gh issue list`, including labels and comments when needed.
- Comment with `gh issue comment <number> --body "..."`.
- Apply or remove labels with `gh issue edit`.
- Close with `gh issue close <number> --comment "..."`.
- Infer the repository from the configured Git remote.

## Pull requests as a triage surface

**PRs as a request surface: no.**

GitHub Issues—not pull requests—are the request and triage surface.

## Skill operations

- “Publish to the issue tracker” means creating a GitHub issue.
- “Fetch the relevant ticket” means reading it with `gh issue view <number> --comments`.

## Wayfinding operations

- A map is an issue labelled `wayfinder:map`.
- Child tickets are GitHub sub-issues where supported, otherwise task-list entries linked back to the map.
- Dependencies use GitHub’s native issue dependencies where supported, otherwise a `Blocked by:` line.
- Unblocked, unassigned children form the work frontier.
- Claim work by assigning the issue to the current user.
- Resolve it by recording the answer, closing the issue, and updating the map.
