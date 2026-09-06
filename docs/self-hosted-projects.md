# Projects on self-hosted instances

Every organization in this fork can create multiple projects without a Cloud subscription or license.
Organization admins and owners can create projects through the project picker or the API.
Existing organizations receive the same access automatically when the updated application starts; no database backfill is required.
The API retains its safety cap of 1,500 projects per organization and its existing membership and demo-project restrictions.
Cloud deployments retain their billing limits.

Build and deploy the updated `Dockerfile.multi-org` image to apply the change to a running instance.
The image workflow uses a native ARM64 runner for its ARM64 image and boot check.
The frontend build includes the workspace packages required by the current upstream frontend, including `packages/llm-normalizer`.
The organization model must be included in the image alongside the API serializer so the project picker and creation endpoint use the same entitlements.

Other feature restrictions remain in place.
The entitlement catalog in `posthog/constants.py` includes group analytics, SAML, SCIM, access controls, survey customization, advanced alerts, and retention features.
Actual availability depends on the feature's implementation, deployment configuration, and any required services.
AI session summaries and map rendering also have explicit Cloud deployment checks.
Enabling multiple projects does not provision those services or change those other entitlements.
