# Base image with a version known to have prebuilt wheels for our
# dependencies (rasterio, geopandas, etc.) — see the requirements.txt
# history in this project for why pinning to a well-supported Python
# version matters (a too-new Python forces slow/broken source builds).
FROM python:3.11-slim

# GDAL and its dependencies are C libraries, not Python packages — pip
# alone can't provide them. This is the actual fix for the whole class of
# "fiona/rasterio won't install" problems seen when relying on whatever
# GDAL (if any) happens to be on someone's machine.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gdal-bin \
    libgdal-dev \
    g++ \
    && rm -rf /var/lib/apt/lists/*

ENV GDAL_CONFIG=/usr/bin/gdal-config

WORKDIR /app

# Install Python dependencies first (before copying the rest of the code)
# so Docker can cache this layer — rebuilding after a code-only change
# doesn't require reinstalling every package from scratch.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Now copy the actual application.
COPY . .

# uploads/, converted/, and the seed_images/training_data.geojson this app
# needs at runtime are expected to be mounted in via a Docker volume (see
# docker-compose.yml) — not baked into the image — so they survive
# container restarts and image rebuilds.

EXPOSE 5000

# --timeout is set generously (30 minutes) because classification on a
# large raster genuinely can take several minutes — gunicorn kills a
# worker that exceeds this, which would otherwise silently abort a
# still-running, legitimate classification job.
CMD ["gunicorn", "--chdir", "backend", "app:app", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "2", \
     "--threads", "4", \
     "--timeout", "1800"]
