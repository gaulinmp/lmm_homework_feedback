from fastapi import FastAPI

app = FastAPI(title="ADA Homework Tutor", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
