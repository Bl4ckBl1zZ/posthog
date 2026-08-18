# RestoreCord PostHog fork

This fork tracks [PostHog/posthog](https://github.com/PostHog/posthog) and adds a small set
of changes for running PostHog self-hosted.

## What this fork changes

- **Opt-in multi-organization mode.** Setting `MULTI_ORG_ENABLED=true` lets a self-hosted
  instance create and manage multiple organizations without consulting the instance
  license. The organization switcher trusts the preflight permission and opens the
  creation dialog directly.
- **Replay Vision on self-hosted.** An Anthropic scanner provider, and feature flags
  evaluated locally instead of against PostHog Cloud.
- **Retention settings on self-hosted.** Team and project retention overrides stay
  editable when the instance is not running on Cloud.
- **A low-memory ClickHouse profile** for small self-hosted deployments.
- **Container images** built from `Dockerfile.multi-org`, `Dockerfile.replay-overlay`, and
  `Dockerfile.recording-rasterizer`, published to `ghcr.io/bl4ckbl1zz/posthog`.

Only the MIT-licensed core organization permission, preflight, and organization creation
UI paths are changed. The separate license in `ee/LICENSE` still governs any enterprise
code included in or used by the upstream base image, including production use.

## Staying current with upstream

`.github/workflows/fork-upstream-sync.yml` runs daily. It merges `PostHog/posthog@master`
into `automation/upstream-sync`, re-applies the CI policy below, checks that the fork's own
changes survived, then opens a PR and merges it.

The merge is a real merge commit, not a squash. Squashing would drop the link to the
upstream parent, so every later sync would try to replay thousands of commits it already
has.

When a merge conflicts outside the paths the policy owns, the automation stops: the PR is
left as a draft with the conflict markers in place, listing the files that need attention.
Resolve them on the branch and mark the PR ready.

To sync early, or to sync a different upstream ref, run the workflow from the Actions tab.

### Required: the FORK_SYNC_TOKEN secret

The sync needs a personal access token with **`repo` and `workflow` scope**, stored as the
`FORK_SYNC_TOKEN` repository secret. Without it the workflow fails on its first step.

Merging upstream always rewrites files under `.github/workflows/`, and GitHub refuses to let
the built-in `GITHUB_TOKEN` push those. No `permissions:` block can grant it the `workflow`
scope, so a PAT (or a GitHub App token with `workflows: write`) is the only option.

A PAT has a second benefit: pushes made with `GITHUB_TOKEN` never trigger workflows, so the
sync PR would otherwise run no checks beyond the sync's own `verify` job.

Create it at **Settings > Developer settings > Personal access tokens**, then add it under
**Settings > Secrets and variables > Actions** in this repository.

## CI policy

Upstream's CI targets Depot and a self-hosted runner pool this fork does not have, and it
keeps adding jobs that reference them. So the fork's CI overrides are generated rather than
hand-maintained:

- `.github/fork-ci-policy.yml` declares the runner mapping, the action replacements, which
  workflows keep their upstream triggers, and which fork changes must never disappear.
- `bin/fork-ci-normalize.py` applies that policy to `.github/workflows/` and
  `.github/actions/`.
- `bin/fork-sync-resolve.py` resolves the merge conflicts the policy covers. Under
  `.github/` upstream always wins, because the normalizer re-applies the fork's overrides
  straight afterwards.

Everything runs on GitHub-hosted standard runners, which are free and unlimited on a public
repository.

PostHog's full CI is around 190 jobs sized for a paid fleet. Running all of it on free
runners would queue for hours and never go green, which would block the sync from merging.
So only the workflows this fork maintains keep their upstream triggers. Every other
workflow keeps `workflow_dispatch` and can still be run by hand from the Actions tab.

To bring a workflow back into the automatic set, add its filename to `enabled_workflows` in
`.github/fork-ci-policy.yml` and run:

```bash
bin/fork-ci-normalize.py
```

`.github/workflows/fork-ci-guard.yml` runs `bin/fork-ci-normalize.py --check` on every PR,
so a change that reintroduces an unavailable runner or drops a tracked fork change fails
before it lands.
