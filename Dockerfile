# Use a lightweight Python base image
FROM python:3.10-slim

# Prevent Python from writing pyc files and keep stdout unbuffered
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies: FFmpeg and curl/unzip (for Deno)
RUN apt-get update && \
    apt-get install -y ffmpeg curl unzip && \
    rm -rf /var/lib/apt/lists/*

# Download and install Deno locally for yt-dlp JavaScript challenges
RUN curl -fsSL https://deno.land/install.sh | sh

# Add Deno to the system PATH
ENV PATH="/root/.deno/bin:$PATH"

# Set the working directory inside the container
WORKDIR /app

# Install all the required Python libraries
RUN pip install --no-cache-dir pyrofork tgcrypto yt-dlp requests aiohttp motor pymongo

# Copy all your files into the container
COPY . .

# Create the downloads directory
RUN mkdir -p downloads

# Expose the Koyeb port
EXPOSE 8000

# Run the bot
CMD ["python3", "main.py"]
