# Fixed LibreOffice 26.8.0.3 and Noto CJK from the official image. Every
# conversion has its own profile and externally supervised process group.
FROM gotenberg/gotenberg:8.37.0@sha256:f29984bd1e226bf1b93ba90af06000afa8b315853e99d27b9aaa41b93f15c769
USER root
COPY tools/ai-document-converter /opt/dlr-converter
ENV DLR_DOCUMENT_ROLE=office PORT=5002 PYTHONDONTWRITEBYTECODE=1
USER gotenberg
WORKDIR /opt/dlr-converter
CMD ["python3", "server.py"]
