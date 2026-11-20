FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
# 国内用阿里云源(与 N150 上首次安装时的写法完全一致,重装时可直接复用已下载的依赖)
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
COPY bycall ./bycall
COPY tools ./tools
ENV BYCALL_DATA=/data
CMD ["python", "-m", "bycall.main"]
