FROM __BASE_IMAGE__
USER root
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
__INSTALLS__
USER vscode
