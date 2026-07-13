# RestoreCord PostHog fork

This fork keeps PostHog's existing single-organization behavior by default. Setting
`MULTI_ORG_ENABLED=true` opts a self-hosted instance into creating and managing
multiple organizations without consulting the instance license.

The custom image is built from `Dockerfile.multi-org` and published as
`ghcr.io/bl4ckbl1zz/posthog:multi-org-latest` plus an immutable commit tag.

Only the MIT-licensed core organization permission and preflight paths are changed.
The separate license in `ee/LICENSE` still governs any enterprise code included in
or used by the upstream base image, including production use.
