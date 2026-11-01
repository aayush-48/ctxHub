# Deploy to Google Cloud (free trial)

This sets up ctx on Cloud Run, Cloud SQL (Postgres + pgvector), Secret Manager, Cloud Storage,
Cloud Tasks and Vertex AI, all in one GCP project. Allow about 45 minutes; most of it is waiting
for Cloud SQL to be created.

**Cost:** the main fixed cost is Cloud SQL (the default `db-f1-micro` tier is the cheapest).
Cloud Run scales to zero, and Vertex AI is billed per call. On the $300 / 90-day trial none of
this charges your card. When the trial ends, resources stop unless you upgrade. Before that,
either upgrade or move to the free-tier stack (Neon + Hugging Face Spaces + Gemini API key);
the app only needs different environment variables.

## 1. Create the project

1. Sign up for the GCP free trial at https://cloud.google.com/free.
2. Create a project (for example `ctx-prod`) and note its **project ID**.
3. Install the gcloud CLI: https://cloud.google.com/sdk/docs/install
4. In a terminal:
   ```bash
   gcloud auth login
   gcloud auth application-default login
   gcloud config set project <PROJECT_ID>
   gcloud projects describe <PROJECT_ID> --format="value(projectNumber)"   # note the number
   ```

Your service URL will be `https://ctx-<PROJECT_NUMBER>.asia-south1.run.app`. It's needed
in step 2.

## 2. Create the Google sign-in client

1. In the Cloud console: **APIs & Services → OAuth consent screen**.
   - User type: **External**. App name: `ctx`. Add your support email.
   - Scopes: leave the defaults (`openid`, `email`, `profile`). These basic scopes don't need
     Google's verification.
   - When you're ready for other people to sign in, click **Publish app**. Until then, only
     the test users you list can sign in.
2. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
   - Application type: **Web application**.
   - Authorized redirect URI:
     `https://ctx-<PROJECT_NUMBER>.asia-south1.run.app/auth/google/callback`
3. Copy the **client ID** and **client secret**.

Optional, for GitHub sign-in: on GitHub, go to **Settings → Developer settings → OAuth Apps →
New**, and set the callback URL to `https://ctx-<PROJECT_NUMBER>.asia-south1.run.app/auth/github/callback`.

## 3. Configure Terraform

Install Terraform (https://developer.hashicorp.com/terraform/install; on Windows,
`winget install Hashicorp.Terraform`), then:

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars: project_id, google_oauth_client_id, google_oauth_client_secret
terraform init
terraform apply
```

The first `terraform apply` enables the APIs and creates everything. Cloud SQL takes 10–15
minutes. If it stops with an "API not enabled yet" error, wait a minute and run it again. It
deploys a placeholder container; the real image comes next.

## 4. Build and deploy the app

```bash
REGION=asia-south1
IMAGE=$REGION-docker.pkg.dev/<PROJECT_ID>/ctx/ctx-server:v1
gcloud builds submit --tag $IMAGE ..          # builds in Cloud Build from the repo root
gcloud run deploy ctx --image $IMAGE --region $REGION
```

On first start the app creates its database tables itself.

Check it:

```bash
terraform output service_url
curl https://ctx-<PROJECT_NUMBER>.asia-south1.run.app/health       # {"status":"ok"}
```

Open the service URL in a browser and sign in with Google.

## 5. Connect clients

- **Claude:** Settings → Connectors → Add custom connector → `terraform output mcp_url`.
- **ChatGPT:** Settings → Connectors (developer mode) → add the same URL.
- **CLI** (anyone):
  ```bash
  pipx install "git+https://github.com/<you>/<repo>.git#subdirectory=cli"
  ctx login https://ctx-<PROJECT_NUMBER>.asia-south1.run.app
  ```

## 6. Optional: automatic deploys from GitHub

1. Push this repo to GitHub.
2. Set `github_repo = "<owner>/<repo>"` in `terraform.tfvars` and run `terraform apply` again.
3. In GitHub, go to **Settings → Secrets and variables → Actions → Variables** and add:
   - `GCP_PROJECT_ID`: your project ID
   - `GCP_REGION`: `asia-south1`
   - `WIF_PROVIDER`: `terraform output workload_identity_provider`
   - `DEPLOY_SA`: `terraform output deploy_service_account`

After that, every push to `main` runs the tests, builds the image and deploys it, without any
stored keys. **Evals (real models)** is a manual workflow that runs the handoff-fidelity
benchmark against Vertex AI.

## Settings you may want to change

| Variable (`terraform.tfvars`) | Default | Notes |
|---|---|---|
| `region` | `asia-south1` | Cloud Run, Cloud SQL, Tasks, Storage |
| `vertex_location` | `us-central1` | Where Gemini and the embedding model run; must offer both |
| `arbiter_model` | `gemini-3-flash-preview` | Served from `arbiter_location` (`global`); check current Gemini model names in the Vertex AI docs |
| `arbiter_provider` | `vertex_gemini` | `vertex_claude` needs Claude enabled in Model Garden (paid, not covered by trial credits) |
| `db_tier` | `db-f1-micro` | `db-g1-small` or larger for heavier use |
| `public_base_url` | run.app URL | Set if you map a custom domain; update the OAuth redirect URIs to match |

## Tear down

```bash
# set db_deletion_protection = false in terraform.tfvars, then:
terraform apply
terraform destroy
```
