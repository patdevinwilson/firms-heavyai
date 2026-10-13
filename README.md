
# Global FIRMS activity with HEAVY.AI and Nemotron on AWS

Deploy global NASA FIRMS collection, a map with instrument imagery popups, source-built HEAVY.AI and local Nemotron inference on one **g5.4xlarge**, using AWS profile **heavyai**. Optional Fable briefings summarize query evidence daily.

**Validation status (2026-10-13):** the AWS source build is complete. All three FIRMS products are collecting successfully; the live view held 301,831 observations at verification. Native HeavyIQ passed all six fixture execution tests and generated a live total-count query matching the database. GPU point and polygon rendering report enabled; a real 20,000-point FIRMS render returned a 640×360 PNG (264 ms GPU render, 2.6 s total for that request). These are smoke checks, not broad model-accuracy or performance claims.

## Quick start

Users need Python 3.10+, AWS CLI v2, the AWS Session Manager plugin, an AWS profile with CloudFormation/EC2/IAM/S3/Secrets Manager/SSM deployment permissions, GPU quota/capacity, and a free [FIRMS MAP_KEY](https://firms.modaps.eosdis.nasa.gov/api/map_key/). No local GPU or Docker installation is required.

```bash
git clone https://github.com/patdevinwilson/firms-heavyai.git
cd firms-heavyai
python3 scripts/deploy.py --profile heavyai configure
python3 scripts/deploy.py --profile heavyai deploy --plan
python3 scripts/deploy.py --profile heavyai deploy
python3 scripts/deploy.py --profile heavyai connect
```

Configure prompts for the FIRMS key without echoing it, generates database passwords, and stores credentials in ignored `.deploy/secrets.json` with mode 0600. `FIRMS_MAP_KEY` can also be supplied through the environment. Deployment credentials, the local `config.json`, and `.deploy/` artifacts are excluded from the GitHub repository.

Open http://localhost:8080 during the SSM session. Filter global detections by time, satellite and confidence. Select a cell, page through its records and open their imagery popups. Display cells are 1° aggregations, not activity clusters; their physical areas vary with latitude.

```bash
python3 scripts/deploy.py --profile heavyai connect --port 6273
```

Open http://localhost:6273 for Immerse/native HeavyIQ. Log in as `firms_reader` with `reader_password` from `.deploy/secrets.json`. Administrative login uses `admin` and `heavyai_password`. The default admin password is rotated automatically. Native IQ uses the user's HeavyDB session permissions.

For Immerse basemaps and address search, set `MAPBOX_TOKEN` and `GOOGLE_API_KEY` before running `configure`. Credentials are stored in the deployment secret and written to the protected `[web]` configuration. The pinned webserver injects `window.MAP_CONFIG` into the index page; this revision does not require a frontend rebuild. Use a Mapbox public token. These browser map credentials are visible to clients allowed by the network access rules and should have provider URL restrictions appropriate to the deployment.

The current deployment also permits direct Immerse access on port 6273 from the user's approved public IPs, through a systemd TCP forwarder on the instance's private interface. This is an operational addition to the existing stack; fresh deployments retain SSM-only access. Direct access requires an explicit security-group rule and `immerse_host` set to the browser-reachable hostname/IP in `config.json`. The default `localhost` setting is suitable for an SSM tunnel. If the client's public IP changes, update the access rule.

## Source build

The deployment follows [NVIDIA's source-build workflow](https://docs.nvidia.com/heavyai/installation-and-configuration/installation/build-from-source), Ubuntu 22.04 and CUDA 12.9.2. It builds the dependencies container, then runs **`dev-tools/dev.sh build all --docker`**. The `all` target includes HeavyIQ; upstream's default build omits it.

HeavyDB/HeavyIQ commits are in `config.example.json`; Immerse/webserver commits are in `scripts/build_source.py`. A manifest records refs and the resulting image ID at `/opt/heavyai-source/manifest.json`. Model and llama.cpp revisions are pinned too. Upstream dependencies/base images retain upstream resolution behavior: this is not a fully hermetic build.

The pinned HeavyIQ revision references an unavailable Chroma development package. The build explicitly replaces `chromadb==1.5.10.dev239` with published `1.5.9`, removes the TestPyPI extra index, and builds the declared `pyheavydb==10.0.0` connector from a pinned public source revision using upstream's local-wheel hook. These changes are recorded in the manifest. Native IQ generation is evaluated after startup; RAG remains disabled, and Chroma/RAG behavior is not certified by this demo.

At the inspected revision, `build deps` builds dependencies from scratch rather than merely pulling an image. Allow **several hours** for the first deployment. Remote bootstrap allows eight hours and logs to `/var/log/firms-runtime-install.log`. Subsequent runs reuse matching artifacts. Eight build jobs limit memory pressure. Default encrypted gp3 storage is **500 GB** for build containers, sources and data.

Base setup installs Ubuntu's `docker-buildx` package: upstream builds Sphinx documentation after the product image and requires BuildKit for that final step.

Native IQ configuration is generated from the pinned schema; users need no external model endpoint or manual IQ stanza. `--without-heavyiq` gives a collection-only deployment. Optional `--license` supports releases requiring one. Software/model license terms remain their owners' terms.

Advanced users can opt out of source builds with `configure --image VERSIONED_IMAGE --iq-config RELEASE_SUPPORTED_CONFIG`. Optional `--archive https://... --sha256 ...` loads a verified Docker-save archive. Private registry authentication and cross-account archive access require separate configuration.

## Selected model and HeavySQL comprehension

The initial candidate is NVIDIA's official [Nemotron 3 Nano 4B GGUF](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-4B-GGUF), **Q4_K_M**, with a 2.84 GB weight file. Source-built llama.cpp runs it on **CPU**, eight threads, zero GPU layers and an 8192-token context. HeavyDB retains the A10G GPU. Model latency and concurrent-user capacity have not been measured.

This is an instruction model adapted for HeavySQL, not a model demonstrated to have been trained specifically on HeavySQL. `app/iq_adapter.py` converts native IQ task-marked text completions into chat messages, adding HeavySQL dialect instructions and SQL examples. HeavyIQ supplies database-scoped guidance, live table schemas and sampled values. The advertised 6000-token budget reserves room for adapter instructions and responses.

Supported tasks: SQL generation/repair, result explanation, table selection, suggested questions and instructions, up to five candidates. Streaming is buffered, then emitted as SSE. Beam search, token-logprob scoring, chain-of-thought and SQL-judge endpoints are unsupported. Generated native configuration disables beam search, logprobs and vector RAG. On first deployment, the nine rules in `sql/heavyiq-guidance.txt` seed a persistent SQLite store, exposed through Immerse's Guidance panel. Administrators and users with `immerse_guidance_manage` can add, edit or delete snippets; changes apply to subsequent SQL, table selection and result explanation prompts. All snippets for the session's database are included, and their IDs are returned for the UI's Guidance Used display. Snippets are limited to 6000 bytes per database to fit the prompt budget. Restarts and redeployments preserve edits and deletions; the source file is only an initial seed. Retrieval of a larger HeavySQL documentation corpus is not implemented.

Native HeavyIQ's tokenizer selection does not recognize Nemotron. A runtime overlay uses the tokenizer embedded in the running GGUF through the adapter's tokenize/detokenize endpoints, preserving the model ID and exact token IDs. It also restores three catalog helpers removed from pyheavydb 10 and uses the configured context budget when the LCEL placeholder lacks one. The adapter accepts native task markers in either the prompt or explicit task stop sequences and handles nullable completion parameters. Generated SQL still executes using the supplied user's database session.

GPU rendering uses a headless EGL Vulkan manifest, the NVIDIA graphics/display capabilities, explicit DRM/modeset device access, unlimited locked memory, and the source-built GEOS libraries in the data volume. Bootstrap runs `scripts/verify_render.py` to check renderer status and render actual FIRMS points through `render_vega`; it saves `.render-check.png` on the deployment host and fails setup if rendering fails.

Bootstrap runs `scripts/evaluate_iq.py` against native `/api/v1/lcel/query`. A nonempty temporary fixture checks categorical confidence, satellite grouping, UTC daily grouping, FRP and timestamp boundaries. Generated SQL executes as `firms_reader` and is compared with gold execution results. Admin creates/removes the fixture. `.iq-evaluation.json` on the instance records results. A failure stops bootstrap with an error; already started map/collection services remain available for diagnosis.

Rerun from an SSM shell after prompt/model changes:

```bash
set -a
. /etc/firms-heavyai.env
set +a
cd /opt/firms-heavyai
.venv/bin/python scripts/evaluate_iq.py
```

Passing six smoke cases does not establish broad HeavySQL accuracy. Equivalent UTC dates and midnight timestamps compare equally in the daily-grouping case. Extend evaluation with real investigations, geometry/geography units, joins, missing data, hallucinations and query limits. Fine-tune only after measuring failures; preserve held-out cases.

## Imagery for each record

Every record receives an image descriptor from its satellite, UTC acquisition date, coordinates and day/night flag. Popups fetch NASA GIBS imagery and mark the detection coordinate.

| Satellite | Day GIBS layer | Night GIBS layer | L1B product |
| --- | --- | --- | --- |
| Suomi-NPP | VIIRS_SNPP_CorrectedReflectance_TrueColor | VIIRS_SNPP_Brightness_Temp_BandI5_Night | VNP02IMG |
| NOAA-20 | VIIRS_NOAA20_CorrectedReflectance_TrueColor | VIIRS_NOAA20_Brightness_Temp_BandI5_Night | VJ102IMG |
| NOAA-21 | VIIRS_NOAA21_CorrectedReflectance_TrueColor | VIIRS_NOAA21_Brightness_Temp_BandI5_Night | VJ202IMG |

**GIBS daily mosaics do not guarantee the exact detection overpass.** Every popup says so. Night imagery is brightness temperature, not true colour. No different satellite is substituted if imagery is unavailable. Transparent pixels can mean cloud, missing swaths or publication delays; receiving a PNG does not establish coverage of the observation.

Images are fetched **when a popup opens**, shared by satellite/date/quarter-degree tile and cached for one hour within a 2 GB local cache. Every record has image access; the collector does not eagerly download one file per global row. GIBS mosaics can be revised. Raw FIRMS snapshots remain archived evidence.

Each popup also offers **Find matching instrument granule**. NASA CMR is queried using the satellite-specific L1B product, point and observation minute; candidates are filtered for temporal overlap. Available browse/data links and acquisition times are displayed. Matches are candidates; exact image-pixel association is not asserted. Full L1B files may need Earthdata login, geolocation products and swath processing. Authenticated downloads and raster crops are not implemented. See [NASA GIBS](https://nasa-gibs.github.io/gibs-api-docs/access-basics/) and the [VIIRS L1B catalog](https://ladsweb.modaps.eosdis.nasa.gov/missions-and-measurements/science-domain/viirs-L0-L1).

## Collection and Fable

Hourly collection fetches `world` from VIIRS_NOAA20_NRT, VIIRS_NOAA21_NRT and VIIRS_SNPP_NRT, using a fixed UTC start date and rolling three-calendar-day window. Acquisition/ingestion times are separate. Stable IDs include satellite, product, time and coordinates; revisions retain the ID. Invalid responses do not count as empty successful collections.

Raw CSV is archived in S3. New batches are loaded and counted before one statement publishes the affected source/days. Failed loads leave previous data visible. `firms_observations` exposes published data; `firms_collection_runs` reports freshness. A lock prevents overlapping collectors. Local retention is 90 days; raw S3 retention is 365 days. Initial history is three days; baselines accumulate. Historical backfill/archive replay are not automated.

Publication uses a numeric sequence under the collector lock because HeavySQL does not support `MAX()` on text batch IDs. Bootstrap migrates existing publication markers and recreates the view without deleting observations.

Detections are thermal pixels, not distinct fires, causes, intent or verified incidents. Missing detections do not prove inactivity. Global product coverage depends on orbits, cloud and processing delays. Infrastructure proximity needs another dataset; coverage-normalized anomaly detection is not implemented.

For Fable, set `ANTHROPIC_API_KEY`, rerun `configure --fable-model YOUR_SUPPORTED_FABLE_MODEL_ID`, then redeploy. The project assumes Fable means the Anthropic model/API; supply its exact supported model ID. At 09:15 UTC daily, Fable summarizes three predefined read-only query results and cites their IDs. Briefings, SQL and results go to S3 `briefings/`. Query summaries are sent to Anthropic. Collection runs independently; Fable has no arbitrary SQL/cloud mutation tools.

## Operations

```bash
python3 scripts/deploy.py --profile heavyai status
python3 scripts/deploy.py --profile heavyai monitor --command-id SSM_COMMAND_ID
python3 scripts/deploy.py --profile heavyai destroy
```

The stack creates its own VPC, subnet, internet route, instance, private encrypted/versioned S3 bucket and secret. No inbound ports or SSH keys. Database/IQ/demo ports bind to host loopback; the model adapter binds to Docker's bridge. SSM supplies access. A public shared service additionally needs authentication and TLS.

If the account has reached its VPC quota, configure with `--vpc-id VPC_ID --subnet-id PUBLIC_SUBNET_ID` to reuse existing networking. The subnet must belong to that VPC and have an active internet-gateway route. The stack creates its own security group and never manages or deletes the supplied network. Confirm the subnet's network ACL allows outbound downloads/SSM. Regional GPU availability still applies.

Diagnostics: `/var/log/firms-base-install.log`, `/var/log/firms-runtime-install.log`, `/opt/heavyai-source/heavydb/build/logs`, `docker logs heavyai`, and `journalctl -u firms-model -u firms-iq-adapter -u firms-collect`. IQ can fail its own dependency installation while HeavyDB starts; evaluation catches this. Status reports collection freshness, native API availability and the completed smoke score.

Deploy/monitor report build stages, component compilation percentages, output age and free disk approximately once a minute. Component percentages are not overall deployment completion. Monitor attaches to an existing SSM command without restarting or cancelling it and does not expose raw runtime log contents.

Destroy removes compute/networking and **deletes the root disk**. S3/Secrets Manager are retained and continue incurring storage charges. Preserve database data before teardown. AWS compute, storage and optional API calls incur charges. Browser assets use Leaflet/unpkg and CARTO and require internet.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q app scripts infra
bash -n infra/userdata.sh scripts/bootstrap.sh scripts/install_model.sh
```
<img width="1548" height="867" alt="Screenshot 2026-10-13 at 3 46 17 PM" src="https://github.com/user-attachments/assets/3d2c0782-fe6c-46ea-b71b-f4ceb3746f55" />

<img width="3825" height="1466" alt="Screenshot 2026-10-13 at 3 28 38 PM" src="https://github.com/user-attachments/assets/eee294ad-308d-4b44-99e8-0f0696516232" />


CI requires no AWS credentials. Tests do not certify a real HeavyDB build/model performance. Repository code is MIT licensed; upstream software, model weights, satellite data and services retain their own licenses and terms.
