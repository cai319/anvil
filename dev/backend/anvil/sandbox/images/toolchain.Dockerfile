# Anvil v1.60 严格档工具链镜像（M2a）。
# 首次使用由后端本地构建并缓存；作业阶段默认 --network none。
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential clang lld cmake make ninja-build python3 python3-dev \
       gdb ca-certificates pkg-config git curl \
    && rm -rf /var/lib/apt/lists/*

# 以非 root uid 运行；bind 挂载的 scratch/out 在宿主侧设为 0777。
RUN (useradd -m -u 1000 anvil || true)

ENV ASAN_OPTIONS=abort_on_error=1:symbolize=1
ENV UBSAN_OPTIONS=print_stacktrace=1
WORKDIR /work
