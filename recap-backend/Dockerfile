FROM python:3.11-slim

WORKDIR /app

# FFmpeg, fontconfig နဲ့ Noto fonts (မြန်မာဖောင့် ပါဝင်) ကို အလိုအလျောက် install လုပ်ခြင်း
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fontconfig \
    fonts-noto-core \
    && fc-cache -f -v \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 7860

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
