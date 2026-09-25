"""启动期持久库结构初始化/核对测试。

覆盖三种启动情形：
  - 空库：照常按模型全量建表，API 就绪（/health 200）
  - 完整旧库：保留既有数据并正常启动，不重建不补建
  - 半旧库（已有应用表但缺表/缺必需列，如 events 缺 reveal_deadline）：
    明确列出缺失的表名与列名，阻止 API 就绪（启动失败、/health 503），
    且不在半旧库上补建其它表
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

_tmpdir = tempfile.mkdtemp(prefix="sealed_predictions_schema_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/module_default.db"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, inspect  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import app.main as main_module  # noqa: E402
from app.database import SchemaIncompleteError, initialize_schema  # noqa: E402
from app.models import Event  # noqa: E402

APP_TABLES = {"events", "commitments", "reveals", "deadline_extensions", "settlements"}

# 旧版 events 表：缺少 reveal_deadline 列（复现历史缺陷场景）
LEGACY_EVENTS_DDL = (
    "CREATE TABLE events ("
    "id CHAR(32) PRIMARY KEY, "
    "title VARCHAR(200) NOT NULL, "
    "description TEXT, "
    "commit_deadline DATETIME NOT NULL, "
    "created_at DATETIME NOT NULL)"
)

T0 = datetime(2030, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def make_engine(name: str):
    tmpdir = tempfile.mkdtemp(prefix=f"sealed_predictions_schema_{name}_")
    return create_engine(f"sqlite:///{tmpdir}/{name}.db")


def create_legacy_events_table(engine) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(LEGACY_EVENTS_DDL)


def test_empty_database_is_initialized_from_models():
    engine = make_engine("empty")
    assert initialize_schema(engine) == "initialized"
    assert set(inspect(engine).get_table_names()) == APP_TABLES
    # 幂等：再次启动按完整旧库处理
    assert initialize_schema(engine) == "verified"


def test_complete_legacy_database_preserves_data_and_starts():
    engine = make_engine("legacy_full")
    assert initialize_schema(engine) == "initialized"

    # 旧库中已有业务数据
    with Session(engine) as session:
        session.add(
            Event(
                title="旧库既有事件",
                commit_deadline=T0 + timedelta(hours=1),
                reveal_deadline=T0 + timedelta(hours=2),
            )
        )
        session.commit()

    # 模拟重启：结构完整 -> 正常启动，数据保留，不做任何重建
    assert initialize_schema(engine) == "verified"
    with Session(engine) as session:
        events = session.query(Event).all()
    assert [e.title for e in events] == ["旧库既有事件"]
    assert set(inspect(engine).get_table_names()) == APP_TABLES


def test_half_old_events_table_missing_column_blocks_startup():
    """已有 events 表缺 reveal_deadline：报错点名表与列，且不补建其它表。"""
    engine = make_engine("half_old_column")
    create_legacy_events_table(engine)

    with pytest.raises(SchemaIncompleteError) as excinfo:
        initialize_schema(engine)

    err = excinfo.value
    assert err.missing_columns == {"events": ["reveal_deadline"]}
    assert err.missing_tables == [
        "commitments",
        "deadline_extensions",
        "reveals",
        "settlements",
    ]
    message = str(err)
    assert "events" in message
    assert "reveal_deadline" in message
    for table in err.missing_tables:
        assert table in message

    # 不在半旧库上补建其它表：库中仍只有原来的半旧 events 表
    assert set(inspect(engine).get_table_names()) == {"events"}


def test_existing_tables_missing_whole_table_blocks_startup():
    """已有全部表但缺 settlements 整表：报错点名缺失表，且不补建。"""
    engine = make_engine("half_old_table")
    assert initialize_schema(engine) == "initialized"
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE settlements")

    with pytest.raises(SchemaIncompleteError) as excinfo:
        initialize_schema(engine)

    err = excinfo.value
    assert err.missing_tables == ["settlements"]
    assert err.missing_columns == {}
    assert "settlements" in str(err)
    assert "settlements" not in set(inspect(engine).get_table_names())


def test_lifespan_blocks_api_readiness_on_half_old_database(monkeypatch):
    """半旧库上 lifespan 启动失败：API 不就绪，/health 不误报 ok。"""
    engine = make_engine("api_half_old")
    create_legacy_events_table(engine)
    monkeypatch.setattr(main_module, "engine", engine)
    monkeypatch.setattr(main_module, "schema_ready", False)

    with pytest.raises(SchemaIncompleteError):
        with TestClient(main_module.app):
            pass

    assert main_module.schema_ready is False
    # 启动校验未通过时健康检查返回 503，而不是错误的 {"status": "ok"}
    client = TestClient(main_module.app)
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "SCHEMA_NOT_READY"


def test_lifespan_initializes_empty_database_and_reports_ready(monkeypatch):
    """空库照常初始化并就绪：/health 200；退出 lifespan 后就绪标志复位。"""
    engine = make_engine("api_empty")
    monkeypatch.setattr(main_module, "engine", engine)

    with TestClient(main_module.app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert set(inspect(engine).get_table_names()) == APP_TABLES

    assert main_module.schema_ready is False
