# Global FIRMS activity with HEAVY.AI on AWS

Deploy a global NASA FIRMS collector and a ready-made map demo on **one `g5.4xlarge`**. Use native HeavyIQ in Immerse with a separately hosted HeavyLM endpoint. Optionally generate daily evidence-linked briefings through the Fable API.

**Validation status:** local unit tests and AWS CloudFormation template validation pass. A complete AWS runtime deployment, HEAVY.AI image compatibility, native HeavyIQ generation, and live FIRMS collection have **not yet been verified**. The deployment deliberately requires a release image and its supported HeavyIQ configuration instead of inventing vendor flags or shipping a broken image reference.

## What users provide once

- AWS CLI v2, Python 3.10 or newer, and an AWS profile with deployment permissions.
- The [AWS Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) for local access.
- A free [NASA FIRMS MAP_KEY](https://firms.modaps.eosdis.nasa.gov/api/map_key/).
- An accessible, versioned HEAVY.AI GPU image containing HeavyDB, Immerse and native HeavyIQ; a license if required by that release. Image binaries and licenses are not redistributed in this repository.
- The release-supported `[iq]` configuration for your external model endpoint, including `disabled = false`. Obtain the exact fields from the supplied release. The public configuration documentation does not provide a complete portable endpoint example.
- Optional: an Anthropic API key and the exact supported Fable model ID for scheduled briefings.

A g5.4xlarge has one A10G with 24 GB nominal GPU memory. The documented quantized HeavyLM needs at least 48 GB, so **this instance runs HEAVY.AI; HeavyLM inference runs elsewhere**. External model availability and costs are separate. This repository does not provision that second service or silently replace HeavyLM with a smaller model.

## Quick start

Clone this repository and configure your AWS profile. The default profile is `heavyai`; substitute your own using `--profile` before the subcommand.

```bash
python3 scripts/deploy.py --profile heavyai configure \
  --image YOUR_REGISTRY/YOUR_HEAVYAI_IMAGE:PINNED_VERSION \
  --iq-config /path/to/release-supported-iq.conf \
  --license /path/to/heavyai.license

python3 scripts/deploy.py --profile heavyai deploy --plan
python3 scripts/deploy.py --profile heavyai deploy
python3 scripts/deploy.py --profile heavyai connect
```

`configure` prompts for the FIRMS key without echoing it, generates database passwords, and stores credentials in an ignored local file with mode 0600. `--license` is optional for releases that do not require one. `FIRMS_MAP_KEY` can also be supplied through the environment. Do not put keys in command arguments or commit configuration files.

Open **http://localhost:8080** while the SSM session is running. The map already has time-window, satellite and confidence filters, collection timestamps, and the underlying SQL. Its 1° bins are a display aggregation, not physical activity clusters; their geographic areas vary with latitude. Browser map assets and basemap tiles come from Leaflet/unpkg and CARTO, so browser internet access is required.

For native HeavyIQ/Immerse:

```bash
python3 scripts/deploy.py --profile heavyai connect --port 6273
```

Open http://localhost:6273 and log in as `firms_reader` using the generated `reader_password` from `.deploy/secrets.json`. Administrators use the generated `heavyai_password`; the bootstrap rotates the default password before enabling collection. Import the domain guidance from `sql/heavyiq-guidance.txt` into the native Guidance panel using the administrator account. Automatic guidance import is release-specific and is not implemented here.

For an explicitly collection-only deployment, replace `--iq-config` with `--without-heavyiq`. The map still works; native conversational analytics is disabled and deployment status reports that fact.

## Image archives

You may supply a Docker-save archive instead of pulling a registry image:

```bash
python3 scripts/deploy.py configure \
  --image IMAGE_NAME_AND_TAG_CONTAINED_IN_ARCHIVE \
  --archive https://YOUR_ACCESSIBLE_HOST/release-render-docker.tar.gz \
  --sha256 ARCHIVE_SHA256 \
  --iq-config /path/to/release-supported-iq.conf
```

HTTPS archives must be downloadable by the instance. An S3 URI also works when the instance role has access; by default that role can read only this deployment's own archive bucket. Cross-account/private buckets need an explicit policy grant. Private registries need a credential helper/login on the instance; automated private-registry authentication is not implemented. Prefer an accessible HTTPS release archive for a self-service demo. The archive checksum is verified before `docker load`.

The historical `heavyai/heavyai-ee-cuda:v9.0.2` Docker Hub reference could not be resolved when this project was prepared. There is deliberately no unverified default image.

## Deployment behavior

The CLI creates a fresh VPC, subnet, internet route, an EC2 instance, a private encrypted/versioned S3 bucket and a Secrets Manager secret. No existing VPC, key pair, internal image bucket or IAM role is required. The generated instance role can access only its own bucket and secret, plus the AWS-managed SSM permissions.

There are **no inbound security-group ports** and no SSH key. The instance has outbound internet access and a public address for downloads and SSM. Database and demo ports bind to loopback. Access uses SSM port forwarding; users need AWS `ssm:StartSession` permission. This is an operator-access demo, not a public multi-tenant web service. A public/shared service would additionally need a TLS endpoint, user authentication and a deliberate access model.

Bootstrap installs NVIDIA drivers, Docker, the NVIDIA Container Toolkit and Python dependencies, reboots, then initializes HEAVY.AI, the database schema, and scheduled collection. Startup can take 10–20 minutes or longer with large release downloads. The CLI waits for GPU readiness and reports SSM bootstrap progress. AWS quota/capacity or missing deployment permissions can prevent launch.

The `heavyai` profile defaults to `us-east-1`. Edit the ignored `config.json` after configuring to change the region, disk size or retention. Instance type is intentionally restricted to g5.4xlarge. The default encrypted gp3 root disk is 300 GB; local observations are retained for 90 days, and raw S3 responses for 365 days. Initial collection retrieves only the configured three-day window; longer baselines accumulate over time. Historical backfill is not currently automated.

## Collection and publication

- Hourly collection retrieves `world` for NOAA-20, NOAA-21 and Suomi-NPP, using a fixed UTC start date and a rolling three-calendar-day window.
- Each response is validated before it is archived. Error pages do not count as empty successful collections. Empty CSVs with valid headers do.
- Acquisition time and ingestion time remain separate. IDs include product, satellite, timestamp and coordinates. Revised processing versions retain the observation ID; duplicate rows in one response are collapsed.
- A new batch is loaded and its row count checked before one publication statement selects it for the affected source/days. Failed loads leave the previous batch selected. This avoids assuming that HeavyDB supports a multi-statement transaction.
- Old batches are removed after publication. Raw snapshots preserve provenance, including preliminary detections that a later response replaces.
- A process lock prevents overlapping collectors. Retries use backoff, and logs do not include MAP_KEY-bearing URLs.

The display queries `firms_observations`, a view that selects the latest published source/day batch. `firms_collection_runs` supplies collection status and freshness. The batch tables are internal to ingestion.

FIRMS global coverage is not continuous observation of every location. Cloud, overpass timing, sensor availability and processing latency affect detections. The demo does not estimate coverage-normalized anomalies, infer fire cause or intent, or equate detections with distinct fires. Infrastructure proximity requires loading an additional infrastructure dataset.

## HeavyIQ verification

Start with HeavyLM and the provided guidance. Native HeavyIQ configuration depends on the exact installed release. `status` reports whether native IQ was configured, **not whether generated SQL is correct**. Verify at least these questions in native SQL Notebook and compare the results with the SQL in `sql/demo.sql`:

1. Show nominal- and high-confidence detections in the last 24 hours.
2. Count detections per satellite and confidence category.
3. Show UTC daily counts for the last 30 days; distinguish a short collection history from zero activity.
4. Show the most recent successful collection and observation for each product.
5. Explain what the dataset cannot tell us about distinct incidents and cause.

Review generated SQL for time windows, confidence semantics and coordinate units. A versioned benchmark against representative schemas and expected results is needed before claiming HeavySQL model accuracy. Custom fine-tuning and a native HeavyIQ evaluation API adapter are not included.

## Optional Fable briefing

Set `ANTHROPIC_API_KEY` in your environment and re-run `configure --fable-model YOUR_SUPPORTED_FABLE_MODEL_ID` with the same image/IQ settings. Redeploy to update the secret and start the daily timer.

At 09:15 UTC daily, Fable receives results of three predefined, read-only HeavyDB queries and produces a briefing citing query IDs. The briefing, SQL and result evidence are saved under `briefings/` in S3. This sends query summaries to Anthropic. The model has no arbitrary SQL execution or cloud mutation tools; the hourly collection scheduler works independently of Fable. Expanded agent-driven investigations are a future extension.

## Operations and cleanup

```bash
python3 scripts/deploy.py --profile heavyai status
python3 scripts/deploy.py --profile heavyai destroy
```

`destroy` removes the EC2 instance and networking. **The root disk is deleted; the S3 archive and Secrets Manager secret are retained**, with continuing storage charges until removed separately. Preserve anything needed from the database before teardown. S3 snapshots contain the raw observations but an automated database restore/replay command is not included.

For diagnostics, use an SSM shell session. Check `/var/log/firms-base-install.log`, `/var/log/firms-runtime-install.log`, `docker logs heavyai`, and `journalctl -u firms-collect.service`. These may contain private vendor configuration; do not paste logs into a public issue without reviewing them. Collection health exits nonzero if any product has not succeeded within two hours. It is checked on demand; alert routing is not configured.

AWS resources, EBS/S3 storage, Secrets Manager and optional model calls incur charges. Stopping the instance does not remove storage costs. The deployment does not obtain software/model access or license rights on the user's behalf.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q app scripts infra
bash -n infra/userdata.sh scripts/bootstrap.sh
python3 infra/template.py
```

Tests run with the Python standard library and fake database failures; they do not certify a real HeavyDB release. CI runs the same checks with Python 3.10. No AWS credentials are needed for CI.

Source references: [NASA FIRMS Area API](https://firms.modaps.eosdis.nasa.gov/api/area), [AWS G5 specifications](https://aws.amazon.com/ec2/instance-types/g5/), [HeavyLM deployment](https://docs.heavy.ai/heavyiq-conversational-analytics/heavyiq-model-overview-heavylm), [current HEAVY.AI installation](https://docs.nvidia.com/heavyai/installation-and-configuration/installation), [HeavyDB CSV import](https://docs.nvidia.com/heavyai/loading-and-exporting-data/command-line/load-data).

Repository code is MIT licensed. HEAVY.AI binaries, model weights, satellite products, basemap services and API services retain their own licenses and terms.
