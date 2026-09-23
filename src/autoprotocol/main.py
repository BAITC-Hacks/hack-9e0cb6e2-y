from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from autoprotocol.config import Settings
from autoprotocol.storage import check_database, initialize

ASSETS = Path(__file__).parent / "web"


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        initialize(config.database_path)
        yield

    # Swagger UI по умолчанию использует CDN; оставляем только локальную JSON-схему.
    app = FastAPI(title="Автопротокол", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=ASSETS / "static"), name="static")
    templates = Jinja2Templates(directory=ASSETS / "templates")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(request=request, name="index.html")

    @app.get("/health/live")
    def live():
        return {"status": "ok", "scope": "web-server"}

    @app.get("/health/ready")
    def ready():
        check_database(config.database_path)
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "database": "ok",
                "reason": "Конвейер обработки записи ещё не реализован",
            },
        )

    return app
