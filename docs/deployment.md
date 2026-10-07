# ProofIQ deployment (Django)

## Local setup

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pytest -q
.\run_proofiq.ps1
```

The launcher starts the repository-local Ollama runtime and Django. The default
model is `qwen3.5:4b`; no OpenAI login or paid API credit is required locally.

## Environment variables and secrets

Copy `.env.example` to `.env.local`. Never commit `.env.local`. Important values:

- `DJANGO_SECRET_KEY`: a long random production value.
- `DJANGO_ALLOWED_HOSTS`: comma-separated hosts.
- `AI_PROVIDER`: use `ollama` for both local and Ollama Cloud modes.
- `OLLAMA_URL`: `http://127.0.0.1:11434` locally or `https://ollama.com` on Vercel.
- `OLLAMA_MODEL`: `qwen3.5:4b` locally; set a cloud model on Vercel.
- `OLLAMA_API_KEY`: required only for Ollama Cloud. Store it as a Vercel Secret,
  never in Git or a client-side variable.

## Vercel deployment

Vercel can detect Django from `app.py` and `vercel.json`. Import the Git repository
in Vercel or run `vercel deploy`. Configure `DJANGO_SECRET_KEY` in Project Settings
for Production, Preview, and Development. For working cloud analysis, also set:

```text
AI_PROVIDER=ollama
OLLAMA_URL=https://ollama.com
OLLAMA_MODEL=gpt-oss:120b-cloud
OLLAMA_API_KEY=<create this in your Ollama account>
```

Important: Vercel Functions cannot call Ollama running on your laptop at
`127.0.0.1`. The production values above call Ollama Cloud's HTTPS API instead,
while the local hackathon build can keep using the local model. Upload storage in `/tmp` is ephemeral;
for a multi-user production release, replace it with Vercel Blob or another object
store. The current deployment is suitable for a short demo, not durable storage.

## Troubleshooting

- `DisallowedHost`: add the exact hostname to `DJANGO_ALLOWED_HOSTS`.
- `Ollama offline`: run `.\run_proofiq.ps1` locally.
- Vercel analysis failure: configure a hosted provider; localhost is not reachable.
- Upload disappears: serverless `/tmp` is not durable; use object storage.

## Security notes

Secrets stay in environment variables. Upload names are normalized, size/type
limits are enforced, and user-supplied filesystem paths are not accepted. The
production architecture should use the typed `core/` planner and deterministic
executor; generated Python is display-only evidence and must never be executed.
