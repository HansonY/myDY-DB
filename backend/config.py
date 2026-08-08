"""集中配置。所有机密只从环境变量 / .env 读取,绝不硬编码。"""

import os
from pathlib import Path

# ⚠️ 这一句必须在**任何**会拉起 huggingface_hub 的 import 之前。
# huggingface_hub 在自己被 import 时就把 HF_ENDPOINT 读进 constants.ENDPOINT,
# 之后再改环境变量**完全没有效果** —— 实测在函数里 setdefault 之后
# `constants.ENDPOINT` 仍是 huggingface.co,然后下载报 SSL EOF。
# 放在 config.py 是因为它是所有模块最先导入的那个:knowledge/embed.py 拉
# sentence-transformers、knowledge/asr.py 拉 faster-whisper,两条路都会连带
# 导入 huggingface_hub,谁先谁后不好保证,只有堵在最上游才可靠。
# 用 setdefault:已经显式配过 HF_ENDPOINT 的环境不覆盖。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
# 关掉 Xet(HF 2025 年换的 CAS 内容寻址后端)。镜像站只代理了经典的
# resolve/download 路径,不支持 Xet —— 走 Xet 会绕回 us.aws.cdn.hf.co,
# 于是又撞回被墙的官方 CDN,报 `CAS Client Error: error sending request`。
# 实测症状很迷惑:小文件(config/tokenizer)能下,唯独最大的 model.bin 失败,
# 看起来像「网络不稳」,其实是这一个文件走了不同的通道。
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

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
    # 采集「点赞」「我的作品」需要自己的 sec_user_id。
    # 由 `cli.py whoami` 自动解析写入,一般不用手填。
    douyin_sec_user_id: str = ""
    # 备选:自己的主页 URL。注意 `user/self` 这种别名解析不出 ID,得是含
    # MS4wLjAB… 的真实地址。优先用 whoami 自动解析。
    douyin_profile_url: str = ""

    # ── AI(Phase 2 起需要)──────────────────────
    dashscope_api_key: str = ""

    # ── 采集行为 ────────────────────────────────
    collect_max_items: int = 0        # 0 = 不限
    collect_page_size: int = 20
    # f2 把这个值同时用作 HTTP 超时与翻页间隔,所以它就是限速旋钮。
    # 调大更安全(降低风控概率),调小更快。
    collect_page_delay: int = 8

    # ── 知识库检索 ──────────────────────────────
    # 嵌入模型。默认 bge-m3 —— 四个候选实测下来只有它真能中英互通
    # (中英问同一话题命中同一条视频 4/8,其余 0–1/8)。详见 docs/SEARCH.md。
    # 换模型是安全的:向量是派生数据,全量重建 53 秒;而且 vec_meta 记着
    # 模型名,换了不重建会拒绝检索,不会静默返回垃圾。
    embed_model: str = "BAAI/bge-m3"
    # local = 本机跑(收藏不出机器) | dashscope = 云端(需 api key)
    embed_backend: str = "local"
    # 相似度三档。低于 maybe 直接不返回;之间的标「可能相关」并露出分数。
    #
    # good=0.64 是扫出来的拐点,不是拍的:8 个查询、24 条 good 结果人工标注,
    #   0.62 → 21 对 3 错(88%)
    #   0.64 → 18 对 1 错(95%)   ← 拐点
    #   0.68 → 9 对 0 错(100%,但丢掉一半真结果)
    # 阈值样本量就这么点,而且我给「库里没有」打的标注本身错过一次
    # (mortgage rate 其实找对了)—— 所以这两个数必须可调,
    # 而且分数一律露给人看,不做静默过滤。
    search_good: float = 0.64
    search_maybe: float = 0.52

    # ── 语音转写 ──
    # 抖音不给字幕文本(raw 里逐字段翻过两遍:只有 is_subtitled 标记,
    # 既无字幕文本也无字幕 URL),而平台自己的内容总结只覆盖 34% ——
    # 剩下 66% 只有营销文案。逐字稿是补齐它们的唯一办法。
    # 选型实测(M5 Pro,int8,同 3 条素材):
    #   small(480MB) 9.4× 实时 —— 英文干净,**中文基本不能用**:
    #     「打开雷达刺控管 OK 雷达一起动 Passie 你的零食一碗在设备盘了」
    #     (原视频讲微波炉)。日常量只有每 3 天 11 分钟音频,
    #     用 small 换来的那点速度毫无意义 —— 该换准确度。
    #   large-v3-turbo(1.6GB) 接近 large-v3 的准确度,比它快 8 倍。
    asr_model: str = "large-v3-turbo"

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


def reload() -> Settings:
    """从 .env 重新读取配置。

    必要性:`cli.py qrlogin` / `login` 会在服务运行期间改写 .env。
    配置是 import 时读一次的单例,不重载的话服务里还是旧的空 cookie ——
    网页上点采集会一直报「未配置」,得重启服务才好,这是很烂的体验。

    这里**原地更新**同一个对象而不是换新对象:各模块都 `from config import
    settings` 持有了引用,换对象它们看不到。
    """
    fresh = Settings()
    settings.__dict__.update(fresh.__dict__)
    return settings
