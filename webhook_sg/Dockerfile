FROM python:3.13-slim

WORKDIR /app

# 设置 Python 环境变量
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 复制依赖并安装 (使用清华源)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制脚本
COPY webhook_sg.py .

# 暴露端口
EXPOSE 35555

# 使用 gunicorn 生产部署
CMD ["gunicorn", "--bind", "0.0.0.0:35555", "--workers", "2", "--timeout", "30", "webhook_sg:app"]
