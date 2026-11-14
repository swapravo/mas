"""Local test bench: a small FastAPI app over service.py, plus the TypeScript page in web/.

    uvicorn app:app --reload --port 8000     (bound to localhost; there is no login, local use only)
"""
import datetime as dt
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import service
from harness import LLMUnavailable
from tools import TZ

WEB = Path(__file__).parent / "web"
app = FastAPI(title="Collections agent test bench")
app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")


class Chat(BaseModel):
    body: str = Field(min_length=1, max_length=500)


class Clock(BaseModel):
    advance_hours: float | None = Field(default=None, ge=0, le=24 * 30)
    to: dt.datetime | None = None        # absolute simulated time; naive values are treated as India time
    reset: bool = False


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/state")
def state():
    return service.get_state()


@app.post("/api/reset")
def reset():
    service.reset()
    return service.get_state()


@app.post("/api/chat")
def chat(m: Chat):
    service.resident_says(m.body)
    return service.get_state()


@app.post("/api/clock")
def clock(c: Clock):
    if c.reset:
        service._state["offset"] = dt.timedelta(0)
    elif c.to is not None:
        service.set_clock(to=c.to if c.to.tzinfo else c.to.replace(tzinfo=TZ))
    elif c.advance_hours is not None:
        service.set_clock(advance_hours=c.advance_hours)
    return service.get_state()


@app.post("/api/agent/run")
def run_agent():
    try:
        return {"run": service.run_agent(), "state": service.get_state()}
    except LLMUnavailable as e:
        raise HTTPException(503, f"No free model answered: {e}")
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.post("/api/drafts/{draft_id}/approve")
def approve(draft_id: uuid.UUID):
    try:
        outcome = service.approve(draft_id)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"outcome": outcome, "state": service.get_state()}


@app.post("/api/drafts/{draft_id}/reject")
def reject(draft_id: uuid.UUID):
    try:
        service.reject(draft_id)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return service.get_state()
