from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.auth import CSRFMiddleware, LoginRequired
from app.config import PROJECT_ROOT
from app.routes import admin as admin_routes
from app.routes import auth as auth_routes
from app.routes import student as student_routes

app = FastAPI(title="ADA Homework Tutor", version="0.1.0")

app.add_middleware(
    CSRFMiddleware,
    exempt_paths=("/login", "/healthz"),
)

app.mount(
    "/static",
    StaticFiles(directory=str(PROJECT_ROOT / "app" / "static")),
    name="static",
)

app.include_router(auth_routes.router)
app.include_router(student_routes.router)
app.include_router(admin_routes.router)


@app.exception_handler(LoginRequired)
async def _login_required_handler(_request: Request, _exc: LoginRequired) -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
