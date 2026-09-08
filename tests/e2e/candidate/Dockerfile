# 构建 context 为仓库根；源码仅由 git archive 写入 .runtime/e2e/build。
FROM oven/bun:1.3.14 AS bun
FROM python:3.11.13-slim-bookworm
ARG GIT_VERSION=2.49.0
ARG GH_VERSION=2.76.2
ARG OPENCODE_VERSION=1.17.13
COPY --from=bun /usr/local/bin/bun /usr/local/bin/bun
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl make gcc libc6-dev libssl-dev libcurl4-openssl-dev \
    libexpat1-dev zlib1g-dev gettext xz-utils tini && \
    curl -fsSL "https://www.kernel.org/pub/software/scm/git/git-${GIT_VERSION}.tar.xz" -o /tmp/git.tar.xz && \
    tar -xJf /tmp/git.tar.xz -C /tmp && \
    make -C "/tmp/git-${GIT_VERSION}" -j2 prefix=/usr/local NO_TCLTK=YesPlease install && \
    arch="$(dpkg --print-architecture)" && \
    curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.tar.gz" -o /tmp/gh.tar.gz && \
    tar -xzf /tmp/gh.tar.gz -C /tmp && \
    cp "/tmp/gh_${GH_VERSION}_linux_${arch}/bin/gh" /usr/local/bin/gh && \
    rm -rf /var/lib/apt/lists/* /tmp/git* /tmp/gh*
ENV BUN_INSTALL=/opt/bun
ENV PATH=/opt/bun/bin:/usr/local/bin:/usr/bin:/bin
RUN bun install --global "opencode-ai@${OPENCODE_VERSION}"
COPY .runtime/e2e/build/fleet /opt/fleet
COPY .runtime/e2e/build/agent-runtime /opt/agent-runtime
# 冻结源码没有 Bun lock；只在容器构建输入副本生成 lock，留存实际解析依赖。
RUN mkdir -p /opt/candidate && cd /opt/agent-runtime && bun install && \
    python -m pip install --no-cache-dir /opt/fleet PyYAML==6.0.2 && \
    python -m pip freeze > /opt/candidate/python-packages.txt && \
    mv /opt/agent-runtime/profiles /opt/candidate/original-profiles && \
    ln -s /state/config/runtime-profiles /opt/agent-runtime/profiles && \
    chmod +x /opt/agent-runtime/bin/agent-run /opt/agent-runtime/bin/agent-session && \
    /usr/sbin/useradd --uid 10001 --create-home --home-dir /home/candidate candidate && \
    mkdir -p /state /workspace && chown -R candidate:candidate /state /workspace /opt/bun
COPY tests/e2e/candidate /opt/candidate
COPY .runtime/e2e/build/source-manifest.json /opt/candidate/source-manifest.json
COPY tests/e2e/services/bootstrap_runtime_bus.ts /opt/e2e/bootstrap_runtime_bus.ts
# 宿主归档目录与 manifest 可能来自 umask 077；保证运行用户只读访问。
RUN chmod -R a+rX /opt/fleet /opt/agent-runtime /opt/candidate /opt/e2e
ENV HOME=/home/candidate
ENV AGENT_RUNTIME_SECRETS_FILE=/dev/null
ENV AGENT_RUNTIME_ROUTE_HEALTH_PATH=/state/runtime-route-health.json
ENV AGENT_BUS_TOKEN_FILE=/run/secrets/agent_bus_token
ENV AGENT_BUS_URL=http://agent-bus:7470
ENV OPENCODE_DISABLE_AUTOUPDATE=1
ENV OPENCODE_DISABLE_MODELS_FETCH=1
USER candidate
WORKDIR /workspace
EXPOSE 15611
HEALTHCHECK --interval=10s --timeout=8s --start-period=30s --retries=6 \
  CMD python /opt/candidate/healthcheck.py
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "python", "/opt/candidate/entrypoint.py"]
