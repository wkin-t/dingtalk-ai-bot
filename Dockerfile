# 使用官方 Python 运行时作为父镜像
FROM python:3.14-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
# 防止 Python 生成 .pyc 文件
ENV PYTHONDONTWRITEBYTECODE=1
# 禁用输出缓冲，让日志实时打印
ENV PYTHONUNBUFFERED=1
# 设置时区为东八区，并指定 dws 持久化配置与加密凭据目录（通过 ./data-dws:/app/data-dws 跨容器共享）
ENV TZ=Asia/Shanghai \
    DWS_CONFIG_DIR=/app/data-dws/config \
    DWS_KEYCHAIN_DIR=/app/data-dws/keychain

# 安装系统依赖、CA 根证书（供 Go 二进制访问钉钉 HTTPS 接口）和时区数据
RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone && \
    rm -rf /var/lib/apt/lists/*

# 安装官方 DingTalk Workspace CLI (dws v1.0.67，带 SHA256 强校验，前置以命中 Docker 层缓存)
COPY scripts/install_dws.py /tmp/install_dws.py
RUN python /tmp/install_dws.py && rm -f /tmp/install_dws.py

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
# 使用清华源加速
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制项目代码
COPY . .

# 启动命令
# 使用 gunicorn 启动 Flask 应用，同时启动钉钉 Stream 客户端 (在 main.py 中通过线程启动)
# --worker-class gthread --threads 10: 使用线程模式，支持并发
# --timeout 120: 防止长连接超时
# 使用环境变量 FLASK_PORT，默认 35000
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${FLASK_PORT:-35000} --worker-class gthread --threads 10 --timeout 120 main:app"]