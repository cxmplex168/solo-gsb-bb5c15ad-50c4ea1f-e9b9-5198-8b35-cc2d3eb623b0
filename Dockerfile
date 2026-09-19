FROM python:3.12-slim

WORKDIR /app

# 依赖随镜像构建完成安装,容器启动即可服务
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
