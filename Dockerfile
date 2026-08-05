# 必须 3.10–3.13:pydantic-core 依赖的 PyO3 目前最高支持 3.13,
# 换成 3.14 镜像会在构建时倒在 Rust 编译错误上。
FROM python:3.13-slim

# ffmpeg 供 Phase 2 抽音轨/关键帧使用
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/app/data/douyin.db

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "main:app", "--app-dir", "backend", \
     "--host", "0.0.0.0", "--port", "8000"]
