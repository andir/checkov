FROM python:3.8-slim-buster

RUN apt-get update && apt install -y \
    git \
    && rm -rf /var/lib/apt/lists/*

ADD . /code
RUN pip install /code/.

ENTRYPOINT ["checkov"]
