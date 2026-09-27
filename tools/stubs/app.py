"""Local stand-ins for external systems: the stub model (stub_model.py). Later steps add the
application journey."""

from fastapi import FastAPI

import stub_model

app = FastAPI(title="SurakshaSetu stubs")
app.include_router(stub_model.router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
