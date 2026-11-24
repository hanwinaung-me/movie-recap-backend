FROM python:3.11-slim

WORKDIR /app

# FFmpeg နဲ့ fontconfig ကို install လုပ်ခြင်း
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# မြန်မာဖောင့်ကို system font cache ထဲ ထည့်သွင်းခြင်း
RUN mkdir -p /usr/share/fonts/truetype/myanmar \
    && cp -f fonts/*.ttf /usr/share/fonts/truetype/myanmar/ 2>/dev/null || true \
    && fc-cache -f -v

EXPOSE 7860

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
