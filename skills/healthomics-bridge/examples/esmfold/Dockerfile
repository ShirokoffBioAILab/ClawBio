FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime@sha256:77f17f843507062875ce8be2a6f76aa6aa3df7f9ef1e31d9d7432f4b0f563dee
RUN pip install --no-cache-dir transformers==4.57.3 accelerate==1.12.0 biopython==1.85
COPY inference.py /opt/esmfold/inference.py
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
ENTRYPOINT []
