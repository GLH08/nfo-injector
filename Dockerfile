# 使用 linuxserver/ffmpeg 作为基础镜像，包含 ffprobe
FROM python:3.12-slim

# 安装 ffmpeg（含 ffprobe）
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 工作目录
WORKDIR /app

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制后端代码
COPY backend/ ./backend/

# 复制前端静态文件
COPY frontend/ ./frontend/

# 创建数据目录
RUN mkdir -p /app/data

# 暴露端口
EXPOSE 18880

# 启动
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "18880", "--workers", "1"]
