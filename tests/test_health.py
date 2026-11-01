from fastapi.testclient import TestClient

from app.main import app


def test_healthz():
    r = TestClient(app).get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_lifespan_startup():
    # Старт приложения: миграции идемпотентны, планировщик в тестах выключен
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200


def test_static_vendor_served_locally():
    c = TestClient(app)
    for path in ["/static/vendor/htmx.min.js", "/static/icons.svg", "/static/css/app.css", "/static/js/app.js"]:
        assert c.get(path).status_code == 200, path
