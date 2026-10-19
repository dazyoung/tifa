# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub's **private vulnerability reporting**:
open the repository's **Security** tab and choose **Report a vulnerability**.
Do not open a public issue, pull request or discussion for a security problem.

Useful details:

- what is affected (the REST API, the MCP endpoint, the poller, the deployment scripts or workflows);
- how to reproduce it, with the requests you sent (remove anything that identifies you);
- what an attacker could achieve.

We aim to acknowledge a report within a week and to agree a fix and a disclosure date with you.
Please give us a reasonable time to fix a problem before you publish it.

## Scope

In scope:

- this repository's code, container image, workflows and infrastructure scripts;
- the public Tifa API and MCP endpoint, including ways around its fair-use limits that could run up cost or deny
  service to others.

Out of scope:

- TfL's own APIs and data (report those to Transport for London);
- volumetric denial of service, and findings that need a compromised Google Cloud or GitHub account;
- reports from automated scanners without a demonstrated impact.

Please do not run load tests or scans against the public service; the fair-use guard may close it for everyone.
A local copy (`TIFA_STORE=memory`) behaves the same way.

## Supported versions

Only the latest commit on `main`, which is what is deployed, receives fixes.

## Data held

Tifa stores TfL's public line-status data only. Client IP addresses are held in memory for rate limiting and are not
written to storage; request logs below HTTP 500 are excluded from Cloud Logging. The only secret is a TfL app key,
kept in Google Secret Manager. Deployment uses Workload Identity Federation, so no cloud credentials exist in this
repository or in GitHub.
