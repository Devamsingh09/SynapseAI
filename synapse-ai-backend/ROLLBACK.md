# Deployment rollback guide

## Backup tags (safe restore points)

| Tag | What it was | Repo |
|-----|-------------|------|
| `deploy-backup-20260303` | Last HF backend **before voice + new tools** | `synapse-ai-backend` (HF) |
| `frontend-backup-20260303` | Last GitHub frontend **before voice UI** | SynapseAI root |

---

## Roll back Hugging Face backend

```bash
cd synapse-ai-backend
git fetch huggingface
git checkout master
git reset --hard deploy-backup-20260303
git push huggingface master --force
git push huggingface master:main --force
```

HF Space will rebuild on the old commit (~1–2 min).

---

## Roll back frontend (Vercel / GitHub)

```bash
cd ..   # SynapseAI root
git fetch origin
git checkout main
git reset --hard frontend-backup-20260303
git push origin main --force
```

Vercel will redeploy the previous frontend if connected to `main`.

---

## Roll back only voice (keep new tools)

If tools work but voice breaks, revert voice-specific commits instead of full reset:

```bash
cd synapse-ai-backend
git log --oneline -5   # find voice deploy commit
git revert <voice-commit-sha>
git push huggingface master
git push huggingface master:main
```

---

## Verify after rollback

- Backend: `https://<your-hf-space>.hf.space/health`
- Frontend: chat without mic / voice still works
