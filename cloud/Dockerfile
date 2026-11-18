# クラウドのアプリ（ADR-0067 2e）。ビルドの文脈はリポジトリの直下:
#   docker build -f cloud/Dockerfile -t pokerapp-cloud .
# 動かす（手元の確認。本番は Cloud Run が PORT と環境変数・Secret Manager の値を渡す）:
#   docker run --rm -p 8080:8080 -e CLOUD_DEV_MEMORY=1 pokerapp-cloud
# Docker Hub の回数制限にかかるときは --build-arg BASE=mirror.gcr.io/library/python:3.12-slim
ARG BASE=python:3.12-slim
FROM ${BASE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 依存は pyproject.toml の [cloud] だけ（店舗 PC の音声認識・RFID は入れない）。版の範囲は pyproject.toml が正本
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['optional-dependencies']['cloud']))" > /tmp/cloud-requirements.txt \
    && pip install -r /tmp/cloud-requirements.txt \
    && rm /tmp/cloud-requirements.txt

# クラウドが読むコード・契約の schema（レコードの検査）・お客さん向け画面のクラウド用ビルド・migration
COPY core ./core
COPY api ./api
COPY cloud ./cloud
COPY docs/contracts/schemas ./docs/contracts/schemas

# root で動かさない
RUN useradd --system --uid 10001 --no-create-home app
USER 10001

EXPOSE 8080
CMD ["python", "-m", "cloud"]
