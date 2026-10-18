# Fixed official CPU models and dependency snapshot; conversion uses the SDK,
# never Docling Serve's in-process Local thread pool.
FROM ghcr.io/docling-project/docling-serve-cpu:v1.37.0@sha256:6eda9c524992147c1b0b06a1a9be4894bd68b1f43c86d92c32f996c4c60d18a3
USER 0
RUN python -m pip install --no-cache-dir pypdf==6.16.1 openpyxl==3.1.5 et-xmlfile==2.0.0 xlrd==2.0.2
COPY tools/ai-document-converter /opt/dlr-converter
ENV DLR_DOCUMENT_ROLE=docling \
    PORT=5001 \
    HF_HUB_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1 \
    OMP_NUM_THREADS=2 \
    DOCLING_NUM_THREADS=2 \
    PYTHONDONTWRITEBYTECODE=1
USER 1001
WORKDIR /opt/dlr-converter
CMD ["python", "server.py"]
