"""Local stand-ins for external systems: the stub model (stub_model.py) and the application journey
that takes the S3 hand-off's signed intake (journey.py, Step 21)."""

from fastapi import FastAPI

import journey
import stub_model

app = FastAPI(title="SurakshaSetu stubs")
app.include_router(stub_model.router)
app.include_router(journey.router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
