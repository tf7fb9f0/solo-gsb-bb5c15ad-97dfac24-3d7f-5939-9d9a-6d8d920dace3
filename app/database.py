import os

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# Compose 环境通过环境变量覆盖为 postgresql+psycopg2://...@db:5432/...
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:postgres@localhost:5432/predictions",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class SchemaIncompleteError(RuntimeError):
    """持久库已含应用表，但表/列与当前 SQLAlchemy 模型不完整匹配。

    ``create_all`` 只能创建缺失的表、**不会为已存在的表补列**。在半旧库
    （如旧版 ``events`` 缺少 ``reveal_deadline``）上继续启动会让 ``/health``
    照常报 ok，而依赖缺失列的接口直到请求期才以 500 暴露。因此启动时检测到
    这种结构即明确列出缺失项并阻止 API 就绪，不在半旧库上补建其它表。
    """

    def __init__(
        self,
        missing_tables: set[str],
        missing_columns: dict[str, list[str]],
    ):
        self.missing_tables = sorted(missing_tables)
        self.missing_columns = {
            table: sorted(columns)
            for table, columns in sorted(missing_columns.items())
            if columns
        }
        details = []
        if self.missing_tables:
            details.append("缺少数据表：" + "、".join(self.missing_tables))
        for table, columns in self.missing_columns.items():
            details.append(f"数据表 {table} 缺少必需列：" + "、".join(columns))
        super().__init__(
            "持久库已存在应用表，但结构与当前模型不完整匹配，"
            "已阻止 API 就绪（不会在半旧库上自动补建表或列）："
            + "；".join(details)
            + "。请使用与当前版本匹配的数据库，或先备份数据后按新版本迁移/重建。"
        )


def initialize_schema(db_engine=None) -> str:
    """启动时初始化或校验持久库结构，返回初始化模式（``initialized`` / ``verified``）。

    区分三种情形：

    1. **空库**（不存在任何应用表）：按当前 SQLAlchemy 模型 ``create_all``
       全量建表，返回 ``"initialized"``；
    2. **完整旧库**（全部应用表及模型要求的列均已存在）：不重建、不补建，
       保留既有数据，返回 ``"verified"``；
    3. **半旧库**（已有部分应用表，但缺表或缺必需列）：**不在其上补建其它
       表**，抛出 :class:`SchemaIncompleteError`，错误信息明确列出缺失的
       表名与列名，由调用方阻止 API 就绪。

    ``create_all`` 不会 ALTER 已存在的表，因此情形 3 若继续启动，缺失列
    （如 ``events.reveal_deadline``）只会在请求期以数据库错误暴露。
    """
    if db_engine is None:
        db_engine = engine
    # 延迟导入：app.models 依赖本模块的 Base，避免循环导入
    from . import models as _models  # noqa: F401

    metadata = Base.metadata
    inspector = inspect(db_engine)
    existing_tables = set(inspector.get_table_names())
    app_tables = set(metadata.tables)
    present_tables = app_tables & existing_tables

    if not present_tables:
        # 空库（或仅有与本应用无关的表）：照常全量初始化
        metadata.create_all(bind=db_engine)
        return "initialized"

    # 已有应用表：按当前模型核对全部应用表及必需列
    missing_tables = app_tables - existing_tables
    missing_columns: dict[str, list[str]] = {}
    for table_name in sorted(present_tables):
        present_columns = {c["name"] for c in inspector.get_columns(table_name)}
        missing = [
            column.name
            for column in metadata.tables[table_name].columns
            if column.name not in present_columns
        ]
        if missing:
            missing_columns[table_name] = missing

    if missing_tables or missing_columns:
        raise SchemaIncompleteError(missing_tables, missing_columns)

    return "verified"
