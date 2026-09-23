import asyncio
import hashlib
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from autoprotocol import jobs, review
from autoprotocol.config import Settings
from autoprotocol.media import probe
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
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])
    app.mount("/static", StaticFiles(directory=ASSETS / "static"), name="static")
    templates = Jinja2Templates(directory=ASSETS / "templates")

    @app.exception_handler(review.ReviewError)
    async def review_error(request: Request, error: review.ReviewError):
        return JSONResponse(status_code=error.status, content={"detail": str(error)})

    def require_same_origin(request: Request):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Изменения разрешены только со страницы приложения.")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "max_upload_mb": config.max_upload_mb,
                "max_duration_minutes": config.duration_limit_seconds // 60,
            },
        )

    def meeting_or_404(meeting_id: str):
        row = jobs.get(config.database_path, meeting_id)
        if row is None:
            raise HTTPException(404, "Встреча не найдена.")
        return row

    @app.get("/api/meetings")
    def list_meetings():
        return jobs.recent(config.database_path)

    @app.get("/api/meetings/{meeting_id}")
    def meeting_status(meeting_id: str):
        return jobs.public(meeting_or_404(meeting_id))

    @app.get("/api/meetings/{meeting_id}/result")
    def meeting_result(meeting_id: str, original: bool = False):
        return review.get_result(config.database_path, meeting_id, original=original)

    @app.patch("/api/meetings/{meeting_id}/result")
    def edit_result(meeting_id: str, patch: review.ReviewPatch, request: Request):
        require_same_origin(request)
        return review.save(config.database_path, meeting_id, patch)

    @app.get("/api/meetings/{meeting_id}/revisions")
    def result_history(meeting_id: str):
        return review.history(config.database_path, meeting_id)

    @app.get("/api/meetings/{meeting_id}/audio")
    def meeting_audio(meeting_id: str):
        row = meeting_or_404(meeting_id)
        if row["status"] != "ready":
            raise HTTPException(409, "Аудио ещё не готово.")
        return FileResponse(row["audio_path"], media_type="audio/wav")

    @app.post("/api/meetings", status_code=202)
    async def upload(
        request: Request,
        title: str = Query(min_length=1, max_length=200),
        timezone: str = "Asia/Almaty",
        occurred_on: date | None = None,
        consent: bool = False,
        idempotency_key: uuid.UUID | None = Header(default=None),
    ):
        if not consent:
            raise HTTPException(422, "Подтвердите право обработки записи.")
        title = title.strip()
        if not title:
            raise HTTPException(422, "Укажите название встречи.")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(422, "Неизвестный часовой пояс.") from None
        require_same_origin(request)
        if request.headers.get("content-type", "").split(";")[0] != "application/octet-stream":
            raise HTTPException(415, "Отправьте содержимое файла как application/octet-stream.")
        key = str(idempotency_key or uuid.uuid4())
        existing = jobs.by_key(config.database_path, key)
        if existing:
            return jobs.public(existing)
        limit = config.max_upload_mb * 1024**2
        length = request.headers.get("content-length")
        if length:
            try:
                declared = int(length)
                if declared < 0:
                    raise ValueError
            except ValueError:
                raise HTTPException(400, "Некорректный размер файла.") from None
            if declared > limit:
                raise HTTPException(413, "Файл превышает допустимый размер.")
        meeting_id = uuid.uuid4().hex
        folder = config.data_dir.resolve() / "meetings" / meeting_id
        folder.mkdir(parents=True)
        path = folder / "source"
        registered = False
        try:
            size = 0
            digest = hashlib.sha256()
            with path.open("xb") as stream:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, "Файл превышает допустимый размер.")
                    stream.write(chunk)
                    digest.update(chunk)
            if size == 0:
                raise HTTPException(422, "Файл пуст.")
            try:
                duration = await asyncio.to_thread(
                    probe, path, config.ffprobe, config.duration_limit_seconds
                )
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            except RuntimeError as error:
                raise HTTPException(503, str(error)) from None
            record = {
                "id": meeting_id,
                "request_key": key,
                "title": title,
                "occurred_on": occurred_on.isoformat() if occurred_on else None,
                "timezone": timezone,
                "now": time.time(),
                "duration_seconds": duration,
                "source_path": str(path),
                "audio_sha256": digest.hexdigest(),
            }
            try:
                jobs.enqueue(config.database_path, record)
            except sqlite3.IntegrityError:
                existing = jobs.by_key(config.database_path, key)
                if existing:
                    return jobs.public(existing)
                raise
            registered = True
            return jobs.public(jobs.get(config.database_path, meeting_id))
        finally:
            if not registered:
                path.unlink(missing_ok=True)
                folder.rmdir()

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
                "reason": "Доступен черновой транскрипт; поручения и экспорт ещё не реализованы",
            },
        )

    return app
