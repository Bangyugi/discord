FROM python:3.11-slim

# Thiết lập thư mục làm việc trong container
WORKDIR /app

# Ngăn Python ghi đè file .pyc và bật log realtime
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Cài đặt các công cụ biên dịch tối thiểu phòng trường hợp dependency cần build
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Tạo thư mục lưu trữ database để mount volume
RUN mkdir -p /app/data

# Sao chép file requirements.txt và cài đặt thư viện
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Sao chép toàn bộ mã nguồn bot vào container
COPY . .

# Chạy bot
CMD ["python", "main.py"]
