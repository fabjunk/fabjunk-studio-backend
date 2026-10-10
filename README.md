# Fabjunk Studio — Backend

Small API that sits between the Android app and Facebook's Graph API.
It holds your page tokens (they never live in the app) and drafts comment
replies in the commenter's language.

## Deploy (Render free tier)

1. Push this `backend/` folder to a GitHub repo (or the whole `fabjunk-studio` folder).
2. Go to `https://dashboard.render.com` → New → Web Service → connect the repo.
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
   - (Or just use the included `render.yaml` — Render picks it up automatically.)
3. In the service's Environment tab, set:
   - `API_KEY` — any long random string (the app will need the same value).
   - `PAGES_JSON` — your pages and their long-lived tokens, e.g.
     `{"123456789": {"name": "TheFabjunk", "token": "EAA..."}}`
     Add one entry per page you want the app to manage.
   - `LLM_API_KEY` — an OpenAI API key (used only to draft comment replies).
   - `LLM_BASE_URL` / `LLM_MODEL` — defaults work with OpenAI.
4. Deploy. Your backend URL will look like
   `https://fabjunk-studio-backend.onrender.com` — enter it in the app's setup screen.

## Run locally (testing)

```
pip install -r requirements.txt
cp .env.example .env   # then fill in your values
uvicorn app:app --reload
```

## Notes

- Page tokens last ~60 days. When one expires, mint a fresh one
  (Graph API Explorer → me/accounts) and update `PAGES_JSON`.
- The Android app never talks to Facebook directly and never asks for a
  Facebook login — all Facebook calls go through this backend.
