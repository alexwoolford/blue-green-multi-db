ARG NEO4J_TAG=2026.10.1
FROM neo4j:${NEO4J_TAG}-enterprise

USER root
# Debian/Ubuntu tags use apt-get. UBI tags use microdnf. The neo4j user must be uid 7474
# because compose runs the toolbox as 7474 so store files are readable by neo4j-local.
RUN if command -v apt-get >/dev/null 2>&1; then \
      apt-get update \
      && apt-get install -y --no-install-recommends python3 python3-venv python3-pip ca-certificates \
      && rm -rf /var/lib/apt/lists/*; \
    elif command -v microdnf >/dev/null 2>&1; then \
      microdnf install -y python3 python3-pip ca-certificates \
      && microdnf clean all; \
    else \
      echo "toolbox base image has neither apt-get nor microdnf" >&2; \
      exit 1; \
    fi \
    && test "$(id -u neo4j)" = "7474"

COPY orchestrator /opt/orchestrator
COPY toolbox/conf /opt/toolbox/conf
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -c /opt/orchestrator/requirements.lock "/opt/orchestrator[dev]" \
    && mkdir -p /local-data /build-data /work \
    && chown -R 7474:7474 /local-data /build-data /work /opt/venv

ENV PATH="/opt/venv/bin:/var/lib/neo4j/bin:${PATH}" \
    TOOLBOX_CONF_DIR=/opt/toolbox/conf \
    SCHEMA_PATH=/opt/orchestrator/schema/schema.cypher
WORKDIR /opt/orchestrator
USER 7474:7474
ENTRYPOINT ["bg"]
