"""集中配置。所有机密只从环境变量 / .env 读取,绝不硬编码。"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 仓库根目录(backend/ 的上一级)
ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── 抖音登录态 ──────────────────────────────
    douyin_cookie: str = ""
    # 采集「点赞」列表时需要:你自己的主页 URL(用于解析 sec_user_id)
    douyin_profile_url: str = ""

    # ── AI(Phase 2 起需要)──────────────────────
    dashscope_api_key: str = ""

    # ── 采集行为 ────────────────────────────────
    collect_max_items: int = 0        # 0 = 不限
    collect_page_size: int = 20
    # f2 把这个值同时用作 HTTP 超时与翻页间隔,所以它就是限速旋钮。
    # 调大更安全(降低风控概率),调小更快。
    collect_page_delay: int = 8

    # ── 存储 ────────────────────────────────────
    db_path: str = "data/douyin.db"

    @property
    def db_file(self) -> Path:
        """绝对化后的库文件路径,并确保父目录存在。"""
        p = Path(self.db_path)
        if not p.is_absolute():
            p = ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def has_cookie(self) -> bool:
        return bool(self.douyin_cookie.strip())

    @property
    def max_items(self) -> int | None:
        """0 / 负数视为不限,转成 None 交给 f2。"""
        return self.collect_max_items if self.collect_max_items > 0 else None


settings = Settings()
