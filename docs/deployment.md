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
- `AI_PROVIDER`: `ollama` locally; a network-accessible provider is required on Vercel.
- `OLLAMA_URL` and `OLLAMA_MODEL`: local Ollama settings.

## Vercel deployment

Vercel can detect Django from `app.py` and `vercel.json`. Import the Git repository
in Vercel or run `vercel deploy`. Configure `DJANGO_SECRET_KEY` in Project Settings
for Production, Preview, and Development.

Important: Vercel Functions cannot call Ollama running on your laptop at
`127.0.0.1`. A public deployment therefore needs a hosted LLM endpoint, while the
local hackathon build can use Ollama for free. Upload storage in `/tmp` is ephemeral;
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
