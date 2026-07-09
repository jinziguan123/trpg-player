import os
import sys
from pathlib import Path

from pydantic_settings import BaseSettings


def _data_base() -> Path:
    """数据根目录：
    - 开发/源码运行：仓库内 server/（db 与素材落在项目里，行为与以前一致）。
    - 打包运行（PyInstaller frozen）：用户可写的 app-data 目录（只读的 .app/安装目录不能写库）。
      mac → ~/Library/Application Support/TRPGPlayer；win → %APPDATA%/TRPGPlayer；
      其它 → ~/.local/share/TRPGPlayer。
    """
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support" / "TRPGPlayer"
        elif sys.platform == "win32":
            base = Path(os.environ.get("APPDATA") or Path.home()) / "TRPGPlayer"
        else:
            base = Path.home() / ".local" / "share" / "TRPGPlayer"
        base.mkdir(parents=True, exist_ok=True)
        return base
    return Path(__file__).parent.parent


_BASE = _data_base()


class Settings(BaseSettings):
    # AI 密钥/地址的唯一真源是设置页（ai_settings.json 的激活 profile）；此处不再放 AI 配置，
    # 也不再从 .env 读取（旧的 DEEPSEEK_API_KEY/BASE_URL 回退已移除）。
    db_path: Path = _BASE / "trpg.db"
    debug: bool = True
    # SQL 回显独立于 debug：默认关闭，避免每次请求把整串 SELECT/INSERT 刷屏。
    # 真要调 SQL 时在 .env 设 SQL_ECHO=true。
    sql_echo: bool = False

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
