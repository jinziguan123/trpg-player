from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    db_path: Path = Path(__file__).parent.parent / "trpg.db"
    # 用户上传的地图素材（独立 PNG）存放目录
    assets_dir: Path = Path(__file__).parent.parent / "data" / "assets"
    debug: bool = True

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
