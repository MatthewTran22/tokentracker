from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from .db import lifespan
from .routes import router

_INDEX = Path(__file__).parent / "static" / "index.html"

app = FastAPI(lifespan=lifespan, title="tokentracker")
app.include_router(router)


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(_INDEX, media_type="text/html")
