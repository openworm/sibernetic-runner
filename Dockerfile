# Cloud Run image that builds Sibernetic from source and serves a FastAPI
# wrapper that runs its OpenCL solver on a GPU over HTTP.

FROM nvidia/cuda:12.1.0-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV LANG=C.UTF-8

# System deps for: building Sibernetic (g++, make, OpenCL ICD + headers,
# X11/GLU/freeglut for the GL link even in headless mode), embedded
# Python (python3-dev), numpy headers (python3-numpy puts them where
# our makefile expects), and the FastAPI app.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        clinfo \
        cmake \
        curl \
        ffmpeg \
        freeglut3-dev \
        git \
        libegl1 \
        libgl1 \
        libglew-dev \
        libglu1-mesa-dev \
        libpython3.11 \
        libx11-6 \
        ocl-icd-libopencl1 \
        ocl-icd-opencl-dev \
        opencl-headers \
        pocl-opencl-icd \
        python3.11 \
        python3.11-dev \
        python3.11-venv \
        python3-numpy \
        python3-pip \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && rm -rf /var/lib/apt/lists/*

# Make NVIDIA's OpenCL ICD discoverable for sibernetic's runtime device probe.
# (On a Cloud Run instance with --gpu, /usr/local/nvidia/lib64 is mounted by
# the runtime; the libcuda+libnvidia-opencl symlinks live there. Pre-creating
# the etc/OpenCL/vendors entry means the ICD loader picks up the real driver
# automatically when the container is run on a GPU instance.)
RUN mkdir -p /etc/OpenCL/vendors \
    && echo "libnvidia-opencl.so.1" > /etc/OpenCL/vendors/nvidia.icd

WORKDIR /app

# Python deps for the FastAPI runner. Install before copying app code so
# they cache across rebuilds.
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Build Sibernetic. Ref is parameterized so we can deploy a feature
# branch without touching the Dockerfile. Defaults to Sibernetic's default
# branch.
ARG SIBERNETIC_REPO=https://github.com/openworm/sibernetic.git
ARG SIBERNETIC_REF=development
RUN git clone "${SIBERNETIC_REPO}" /app/sibernetic \
    && cd /app/sibernetic \
    && git checkout "${SIBERNETIC_REF}" \
    && git rev-parse HEAD > /app/sibernetic-commit.txt
WORKDIR /app/sibernetic
# On Linux, Sibernetic includes <CL/cl.hpp>, which Ubuntu's OpenCL packages no
# longer ship (its own CI gets it from the AMD SDK). Sibernetic bundles the same
# Khronos header, so install that copy. Refs built at request time use it too.
RUN install -D inc/OpenCL/cl.hpp /usr/local/include/CL/cl.hpp
RUN make all

# Copy the runner app last so iterating on it doesn't bust the
# Sibernetic-build cache layer.
WORKDIR /app
COPY main.py runner.py render_movie.py ./
COPY templates/ ./templates/

EXPOSE 8080

CMD ["python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
