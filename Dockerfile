FROM python:3.12-slim

# 不写 .pyc、不缓冲 stdout，便于容器日志即时可见
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖，利用构建层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码并预编译（启动初始化前置到构建阶段）
COPY app ./app
RUN python -m compileall -q app

EXPOSE 8000

# 容器内完成依赖安装与导入初始化；启动后由 lifespan 输出就绪日志
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
