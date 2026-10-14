"""Fixed LibreOffice adapter. Its whole process group is supervised externally."""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

from sandbox import restrict_child

os.umask(0o077)
restrict_child()
request = json.loads(Path(sys.argv[1]).read_text())
source = base64.b64decode(request["data_base64"], validate=True)
if len(source) > 6 * 1024 * 1024:
    raise ValueError("source too large")
extension = Path(request["filename"]).suffix.lower()
if extension not in {".xlsx", ".xls", ".docx"}:
    raise ValueError("unsupported Office format")
root = Path(sys.argv[1]).parent
original = root / f"source{extension}"
original.write_bytes(source)
profile = root / "profile"
(profile / "user").mkdir(parents=True)
(
    profile / "user" / "registrymodifications.xcu"
).write_text("""<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting">
<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>
<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>
</item>
</oor:items>""")
# Child inherits the already-denied INET socket filter and its supervisor group.
completed = subprocess.run(
    [
        "/usr/bin/libreoffice",
        f"-env:UserInstallation={profile.as_uri()}",
        "--headless",
        "--invisible",
        "--nologo",
        "--nodefault",
        "--nofirststartwizard",
        "--norestore",
        "--convert-to",
        "pdf",
        "--outdir",
        str(root),
        str(original),
    ],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    check=False,
)
pdf = root / "source.pdf"
if completed.returncode or not pdf.exists():
    raise RuntimeError("Office conversion failed")
if pdf.stat().st_size > 64 * 1024 * 1024:
    raise ValueError("output too large")
Path(sys.argv[2]).write_text(
    json.dumps({"rendered_pdf": base64.b64encode(pdf.read_bytes()).decode()})
)
