"""Fabjunk Studio backend — multi-page Facebook Graph API proxy.

Env vars:
  API_KEY       shared secret between the Android app and this backend (required)
  PAGES_JSON    {"<page_id>": {"name": "<page name>", "token": "<long-lived page token>"}} (required)
  LLM_API_KEY   OpenAI-compatible API key for reply drafting (required for /draft)
  LLM_BASE_URL  default https://api.openai.com/v1
  LLM_MODEL     default gpt-4o-mini
  GRAPH_VERSION default v21.0
"""
import json
import os
from typing import Optional

import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel

GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v21.0")
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
API_KEY = os.getenv("API_KEY", "")
PAGES = json.loads(os.getenv("PAGES_JSON", "{}"))
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

app = FastAPI(title="Fabjunk Studio Backend")


# ---------- helpers ----------
def check_key(x_api_key: Optional[str]):
    if not API_KEY or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def page_token(page_id: str) -> str:
    try:
        return PAGES[page_id]["token"]
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Unknown page_id: {page_id}")


def any_token() -> str:
    if not PAGES:
        raise HTTPException(status_code=500, detail="No pages configured on backend")
    return next(iter(PAGES.values()))["token"]


def graph_error(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error", {})
        return f"{err.get('message', 'Graph API error')} (code {err.get('code')})"
    except Exception:
        return f"Graph API error: HTTP {resp.status_code}"


# ---------- health / pages ----------
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/pages")
async def list_pages(x_api_key: Optional[str] = Header(None)):
    check_key(x_api_key)
    return {"pages": [{"id": pid, "name": info["name"]} for pid, info in PAGES.items()]}


# ---------- posts & comments ----------
@app.get("/posts")
async def posts(page_id: str, limit: int = 10, x_api_key: Optional[str] = Header(None)):
    check_key(x_api_key)
    token = page_token(page_id)
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"{GRAPH}/{page_id}/posts",
            params={
                "fields": "id,message,created_time,comments.summary(true),likes.summary(true)",
                "limit": max(1, min(limit, 25)),
                "access_token": token,
            },
        )
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=graph_error(r))
    out = []
    for p in r.json().get("data", []):
        out.append(
            {
                "id": p["id"],
                "caption": (p.get("message") or "")[:120],
                "created_time": p.get("created_time", ""),
                "comments_count": p.get("comments", {}).get("summary", {}).get("total_count", 0),
                "likes_count": p.get("likes", {}).get("summary", {}).get("total_count", 0),
            }
        )
    return {"posts": out}


@app.get("/comments")
async def comments(
    post_id: str, page_id: Optional[str] = None, x_api_key: Optional[str] = Header(None)
):
    check_key(x_api_key)
    token = page_token(page_id) if page_id else any_token()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"{GRAPH}/{post_id}/comments",
            params={
                "fields": "id,message,from{name},created_time",
                "order": "chronological",
                "limit": 100,
                "access_token": token,
            },
        )
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=graph_error(r))
    out = []
    for cm in r.json().get("data", []):
        out.append(
            {
                "id": cm["id"],
                "text": cm.get("message", ""),
                "author": (cm.get("from") or {}).get("name", ""),
                "created_time": cm.get("created_time", ""),
            }
        )
    return {"comments": out}


# ---------- video upload (resumable) ----------
@app.post("/upload")
async def upload(
    page_id: str = Form(...),
    video: UploadFile = File(...),
    caption: str = Form(""),
    scheduled_time: Optional[int] = Form(None),
    as_reel: str = Form("false"),
    x_api_key: Optional[str] = Header(None),
):
    check_key(x_api_key)
    token = page_token(page_id)
    data = await video.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty video file")

    async with httpx.AsyncClient(timeout=600) as c:
        # 1. start
        start_params = {
            "upload_phase": "start",
            "file_size": str(len(data)),
            "access_token": token,
        }
        if caption:
            start_params["description"] = caption
        if scheduled_time:
            start_params["published"] = "false"
            start_params["scheduled_publish_time"] = str(scheduled_time)
        r = await c.post(f"{GRAPH}/{page_id}/videos", params=start_params)
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=graph_error(r))
        video_id = r.json().get("video_id")
        if not video_id:
            raise HTTPException(status_code=502, detail="Upload start returned no video_id")

        # 2. transfer (single chunk)
        r = await c.post(
            f"{GRAPH}/{video_id}",
            params={"upload_phase": "transfer", "start_offset": "0", "access_token": token},
            files={"video_file_chunk": ("chunk.mp4", data, "video/mp4")},
        )
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=graph_error(r))

        # 3. finish
        r = await c.post(
            f"{GRAPH}/{video_id}",
            params={"upload_phase": "finish", "access_token": token},
        )
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=graph_error(r))

        result = {"video_id": video_id, "reel_id": None}
        if as_reel.lower() == "true":
            r = await c.post(
                f"{GRAPH}/{page_id}/video_reels",
                params={"video_id": video_id, "access_token": token},
            )
            if r.status_code == 200:
                result["reel_id"] = r.json().get("id")
            else:
                result["reel_error"] = graph_error(r)
        return result


# ---------- AI reply drafting ----------
class DraftReq(BaseModel):
    page_id: str
    comment_text: str


@app.post("/draft")
async def draft(req: DraftReq, x_api_key: Optional[str] = Header(None)):
    check_key(x_api_key)
    if not LLM_API_KEY:
        raise HTTPException(status_code=500, detail="Reply drafting not configured on backend")
    page_name = PAGES.get(req.page_id, {}).get("name", "our page")
    system = (
        f"You manage the Facebook page '{page_name}'. Write a short, warm reply to the "
        "comment below. Reply in the SAME language the comment is written in "
        "(e.g. Portuguese, Hindi, English). Keep it to 1-2 sentences, no hashtags, no emojis "
        "unless the comment used them. First line must be 'LANG: <language name in English>', "
        "then the reply on the following lines."
    )
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": req.comment_text},
                ],
                "temperature": 0.7,
            },
        )
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Draft service error: HTTP {r.status_code}")
    try:
        text = r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        raise HTTPException(status_code=502, detail="Draft service returned an unreadable response")
    language, reply = "unknown", text
    if text.upper().startswith("LANG:"):
        first, _, rest = text.partition("\n")
        language = first.split(":", 1)[1].strip() or "unknown"
        reply = rest.strip() or text
    return {"draft": reply, "language": language}


# ---------- reply to a comment ----------
class ReplyReq(BaseModel):
    comment_id: str
    message: str
    page_id: Optional[str] = None


@app.post("/reply")
async def reply(req: ReplyReq, x_api_key: Optional[str] = Header(None)):
    check_key(x_api_key)
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Message is empty")
    token = page_token(req.page_id) if req.page_id else any_token()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{GRAPH}/{req.comment_id}/replies",
            params={"message": req.message, "access_token": token},
        )
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=graph_error(r))
    return {"reply_id": r.json().get("id")}
