# OpsManager 依赖构建镜像（只用于 CI，不进生产运行时）
#
# 用途：在与生产机完全一致的 OS/Python 基线上做两件事
#   1. make lock   —— pip-compile 解算 requirements.lock
#   2. make wheels —— pip wheel 预编所有依赖为 wheelhouse/
#
# 为什么必须和生产同基线：requirements 里唯一需要编译的 mysqlclient 会链接
# 宿主的 MySQL 客户端库。Ubuntu 的 default-libmysqlclient-dev 指向 MySQL
# （libmysqlclient.so.21），Debian 的指向 MariaDB（libmariadb.so.3）——
# 用 python:3.14-slim（Debian）编出来的轮子放到 Ubuntu 生产机上会在
# import MySQLdb 时报找不到 libmariadb.so.3，且只在运行期才暴露。
#
# 构建：docker build -f deploy/docker/Dockerfile.builder \
#           --build-arg UBUNTU_VERSION=24.04 -t ops-manager-builder:24.04 .
ARG UBUNTU_VERSION=24.04
FROM ubuntu:${UBUNTU_VERSION}

ARG PYTHON_VERSION=3.14
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# deadsnakes 提供 noble/jammy 的 python3.14；python3.14-dev 带 Python.h，
# 编 mysqlclient / cryptography 的 C 扩展需要。
#
# 若所选 UBUNTU_VERSION 上 deadsnakes 还没有对应的 python 版本（apt 报
# "Unable to locate package python3.14"），两条出路：
#   1) 换成生产机实际能装到的版本，同时改 Makefile 的 PYTHON_VERSION；
#   2) 在本镜像里源码编译该 Python 并装到 /usr/local，生产机也用同样办法装
#      —— 两边都必须是同一主次版本，否则轮子的 ABI tag（cp314）对不上。
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates gnupg software-properties-common \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
        python${PYTHON_VERSION} \
        python${PYTHON_VERSION}-dev \
        python${PYTHON_VERSION}-venv \
        build-essential pkg-config \
        default-libmysqlclient-dev \
        libffi-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# 用 venv 而非系统 python：Ubuntu 的 python3.x 带 EXTERNALLY-MANAGED 标记，
# 直接 pip install 会被 PEP 668 拒绝
RUN python${PYTHON_VERSION} -m venv /opt/build \
    && /opt/build/bin/pip install -U pip setuptools wheel pip-tools
ENV PATH=/opt/build/bin:$PATH

WORKDIR /src
