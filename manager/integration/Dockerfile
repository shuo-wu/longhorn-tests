FROM python:3.7.4-stretch

ARG KUBECTL_VERSION=v1.17.0
ARG ARCH=amd64

RUN apt-get update && \
    apt-get install -y vim-tiny nfs-common xfsprogs

RUN wget -q https://storage.googleapis.com/kubernetes-release/release/$KUBECTL_VERSION/bin/linux/${ARCH}/kubectl && \
    mv kubectl /usr/local/bin/kubectl && \
    chmod +x /usr/local/bin/kubectl

ADD tests/requirements.txt .
RUN pip install -r requirements.txt

ADD . /integration
WORKDIR /integration/tests

ENTRYPOINT ["./run.sh"]
