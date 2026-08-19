# The gated write connector. BUILD THIS MULTI-ARCH, deliberately: a cluster node
# and the CI runner that builds the image are frequently different architectures
# (arm64 node, x86 builder is the common pairing). A single-arch image builds
# clean, passes CI, and then fails to pull on the node with "no matching
# manifest" -- which reads as a registry problem rather than a build one.
#
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -t <your-registry>/sre-bot-k8s-write-mcp:v1 --push examples/sre-bot/connectors/k8s-write
FROM python:3.12-slim

# uid 65532 (nonroot) matches the other connectors, so the same securityContext
# and the same secret-file group ownership work without a special case.
RUN useradd --uid 65532 --create-home --shell /usr/sbin/nologin nonroot

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .

USER 65532:65532
EXPOSE 8000
ENTRYPOINT ["python", "-u", "server.py"]
