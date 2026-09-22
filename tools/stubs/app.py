"""Local stand-ins for external systems. Later steps add the stub model, journey and TSA."""

from fastapi import FastAPI

app = FastAPI(title="SurakshaSetu stubs")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
