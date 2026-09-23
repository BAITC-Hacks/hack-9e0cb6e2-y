from fastapi.testclient import TestClient

from autoprotocol.config import Settings
from autoprotocol.main import create_app
from autoprotocol.storage import connect, initialize


def test_app_reports_unimplemented_pipeline_and_serves_local_assets(tmp_path):
    settings = Settings(data_dir=tmp_path, _env_file=None)
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        assert client.get("/health/live").status_code == 200
        readiness = client.get("/health/ready")
        assert readiness.status_code == 503
        assert readiness.json()["database"] == "ok"
        page = client.get("/")
        assert page.status_code == 200
        assert "Скачать протокол DOCX" in page.text
        assert client.get("/static/app.css").status_code == 200
        assert client.get("/docs").status_code == 404


def test_database_initialization_preserves_existing_data(tmp_path):
    path = tmp_path / "app.sqlite3"
    initialize(path)
    with connect(path) as db:
        db.execute("CREATE TABLE sentinel (value TEXT)")
        db.execute("INSERT INTO sentinel VALUES ('сохранено')")
    initialize(path)
    with connect(path) as db:
        assert db.execute("SELECT value FROM sentinel").fetchone()[0] == "сохранено"
        assert db.execute("SELECT count(*) FROM schema_version").fetchone()[0] == 6
