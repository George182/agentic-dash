#!/usr/bin/env bash
# tools/deploy.sh — provision GCP resources and deploy agentic-dash to Cloud Run.
#
# Idempotent: safe to re-run. Existing resources are reused; secrets get a new
# version added if values changed. Reads sensitive values from the repo's
# (gitignored) .env so secret values never end up committed.
#
# Assumes:
#   - You have an authenticated `gcloud` (`gcloud auth login`) with permissions
#     to create Secret Manager secrets, Artifact Registry repos, and Cloud Run
#     services in the target project.
#   - The runtime service account `agentic-dash-sa@<project>.iam.gserviceaccount.com`
#     already exists and has the runtime roles (aiplatform.user, bigquery.jobUser,
#     bigquery.dataViewer, logging.logWriter). This script only adds
#     secretmanager.secretAccessor per-secret.
#
# Usage:
#   bash tools/deploy.sh

set -euo pipefail

# ---------- Repo paths ----------
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cyan()  { printf "\033[36m==> %s\033[0m\n" "$1"; }
green() { printf "\033[32m✓ %s\033[0m\n"   "$1"; }
amber() { printf "\033[33m… %s\033[0m\n"   "$1"; }

# ---------- Load .env (contains the values for both env-vars and secrets) ----------
# Nothing project-specific is hardcoded in this script — all identifiers come
# from .env (which is gitignored). Safe to commit this file to a public repo.
if [ ! -f "${REPO_DIR}/.env" ]; then
  echo "ERROR: ${REPO_DIR}/.env not found. Need it to populate Secret Manager." >&2
  exit 1
fi
# shellcheck disable=SC1091
set -a; . "${REPO_DIR}/.env"; set +a

require_var() {
  if [ -z "${!1:-}" ]; then
    echo "ERROR: \$$1 not set in .env" >&2
    exit 1
  fi
}
for v in GOOGLE_CLOUD_PROJECT BQ_COMPUTE_PROJECT_ID BQ_DATA_PROJECT_ID BQ_DATASET_ID \
         AGENT_ENGINE_RESOURCE_NAME AGENT_ENGINE_SANDBOX_RESOURCE_NAME ; do
  require_var "$v"
done

# ---------- Derived config (overridable; never committed values) ----------
PROJECT="${GOOGLE_CLOUD_PROJECT}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
SERVICE_NAME="${CLOUD_RUN_SERVICE:-agentic-dash}"
AR_REPO="${CLOUD_RUN_AR_REPO:-agentic-dash}"
SA_EMAIL="${CLOUD_RUN_SA:-agentic-dash-sa@${PROJECT}.iam.gserviceaccount.com}"

cyan "Project ${PROJECT}, region ${REGION}, runtime SA ${SA_EMAIL}"

# ---------- 1. Artifact Registry repo ----------
if gcloud artifacts repositories describe "${AR_REPO}" \
     --project="${PROJECT}" --location="${REGION}" >/dev/null 2>&1; then
  amber "Artifact Registry repo '${AR_REPO}' already exists — skipping create"
else
  cyan "Creating Artifact Registry repo '${AR_REPO}'"
  gcloud artifacts repositories create "${AR_REPO}" \
    --project="${PROJECT}" --location="${REGION}" \
    --repository-format=docker --quiet
  green "Artifact Registry repo created"
fi

# ---------- 2. Cloud Build identities — grant roles to BOTH SAs ----------
# Google switched the default Cloud Build identity from the legacy
# <project-number>@cloudbuild.gserviceaccount.com to the Compute Engine default
# SA <project-number>-compute@developer.gserviceaccount.com (April 2024). When
# `gcloud run deploy --source .` builds, it uses the Compute default unless
# overridden — and that SA needs to read the source tarball from GCS, push the
# image to Artifact Registry, and write build logs.
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
LEGACY_CB_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"
COMPUTE_CB_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

cyan "Granting Cloud Build identities the roles needed to build + deploy"
# Legacy Cloud Build SA — kept for backward compat / deploy-to-Cloud Run hook
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${LEGACY_CB_SA}" \
  --role=roles/run.builder \
  --condition=None --quiet >/dev/null

# Compute Engine default SA — the actual Cloud Build identity for new builds.
# `cloudbuild.builds.builder` bundles storage.objects.get + artifactregistry
# writer + logging.logWriter, which covers source pull + image push + logs.
for role in \
  roles/cloudbuild.builds.builder \
  roles/artifactregistry.writer \
  roles/logging.logWriter ; do
  gcloud projects add-iam-policy-binding "${PROJECT}" \
    --member="serviceAccount:${COMPUTE_CB_SA}" \
    --role="${role}" \
    --condition=None --quiet >/dev/null
done
green "Cloud Build identities have required roles"

# ---------- 3. Secret Manager — create + grant accessor ----------
create_or_update_secret() {
  local name="$1" value="$2"
  if gcloud secrets describe "${name}" --project="${PROJECT}" >/dev/null 2>&1; then
    amber "Secret '${name}' exists; adding new version with current .env value"
    printf '%s' "${value}" | gcloud secrets versions add "${name}" \
      --project="${PROJECT}" --data-file=- >/dev/null
  else
    cyan "Creating secret '${name}'"
    printf '%s' "${value}" | gcloud secrets create "${name}" \
      --project="${PROJECT}" --replication-policy=automatic --data-file=- >/dev/null
  fi
  gcloud secrets add-iam-policy-binding "${name}" \
    --project="${PROJECT}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role=roles/secretmanager.secretAccessor \
    --condition=None --quiet >/dev/null
}

create_or_update_secret google-cloud-project                "${GOOGLE_CLOUD_PROJECT}"
create_or_update_secret bq-compute-project-id               "${BQ_COMPUTE_PROJECT_ID}"
create_or_update_secret bq-data-project-id                  "${BQ_DATA_PROJECT_ID}"
create_or_update_secret bq-dataset-id                       "${BQ_DATASET_ID}"
create_or_update_secret agent-engine-resource-name          "${AGENT_ENGINE_RESOURCE_NAME}"
create_or_update_secret agent-engine-sandbox-resource-name  "${AGENT_ENGINE_SANDBOX_RESOURCE_NAME}"
green "All 6 secrets present + runtime SA granted secretAccessor"

# ---------- 4. Cloud Run deploy ----------
cyan "Deploying ${SERVICE_NAME} (Cloud Build → Artifact Registry → Cloud Run)"

# Non-secret env vars (config flags / tuning). Secrets are wired via --set-secrets.
ENV_VARS="GOOGLE_GENAI_USE_VERTEXAI=1"
ENV_VARS+=",GOOGLE_CLOUD_LOCATION=${GOOGLE_CLOUD_LOCATION:-us-central1}"
ENV_VARS+=",BQ_MAX_ROWS=${BQ_MAX_ROWS:-1000}"
ENV_VARS+=",AGENT_MODEL=${AGENT_MODEL:-gemini-2.5-flash}"
ENV_VARS+=",AGUI_USER_ID=${AGUI_USER_ID:-dashboard}"
ENV_VARS+=",GEMINI_MIN_INTERVAL_S=${GEMINI_MIN_INTERVAL_S:-1.2}"

SECRETS="GOOGLE_CLOUD_PROJECT=google-cloud-project:latest"
SECRETS+=",BQ_COMPUTE_PROJECT_ID=bq-compute-project-id:latest"
SECRETS+=",BQ_DATA_PROJECT_ID=bq-data-project-id:latest"
SECRETS+=",BQ_DATASET_ID=bq-dataset-id:latest"
SECRETS+=",AGENT_ENGINE_RESOURCE_NAME=agent-engine-resource-name:latest"
SECRETS+=",AGENT_ENGINE_SANDBOX_RESOURCE_NAME=agent-engine-sandbox-resource-name:latest"

gcloud run deploy "${SERVICE_NAME}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --source="${REPO_DIR}" \
  --service-account="${SA_EMAIL}" \
  --no-allow-unauthenticated \
  --ingress=internal-and-cloud-load-balancing \
  --max-instances=1 \
  --memory=2Gi --cpu=1 \
  --timeout=3600 \
  --set-env-vars="${ENV_VARS}" \
  --set-secrets="${SECRETS}" \
  --quiet

green "Cloud Run service deployed"

URL=$(gcloud run services describe "${SERVICE_NAME}" \
        --project="${PROJECT}" --region="${REGION}" \
        --format='value(status.url)')

cat <<EOF

==> Done.

Service URL:        ${URL}
Service account:    ${SA_EMAIL}
Ingress:            internal-and-cloud-load-balancing  (laptop curl will NOT work)

Test from Cloud Shell (or a VPC-attached VM in this project):
  curl -H "Authorization: Bearer \$(gcloud auth print-identity-token)" ${URL}/

Tail Cloud Run logs:
  gcloud logging read 'resource.type=cloud_run_revision AND resource.labels.service_name=${SERVICE_NAME}' \\
    --project=${PROJECT} --limit=50 \\
    --format='table(timestamp,severity,textPayload)'

Rollback to a previous revision (if needed):
  gcloud run revisions list --service=${SERVICE_NAME} --region=${REGION}
  gcloud run services update-traffic ${SERVICE_NAME} \\
    --to-revisions=<previous-revision>=100 --region=${REGION}
EOF
