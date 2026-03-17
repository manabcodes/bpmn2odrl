"""
main.py  —  FastAPI service for BPMN → ODRL conversion (bpmn2odrl v9)

Endpoints
---------
GET  /                   health check + API info
POST /convert            upload BPMN file → ODRL JSON-LD (JSON response)
POST /convert/download   upload BPMN file → ODRL JSON-LD (file download)
GET  /docs               auto-generated Swagger UI (FastAPI built-in)
"""

import json
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response

# Import the pipeline
from bpmn2odrl9 import run_pipeline_in_memory

# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="BPMN → ODRL Policy Extraction API",
    description=(
        "Automatically extracts ODRL deontic policies (obligations, permissions, "
        "prohibitions) from BPMN 2.0 XML process models. Based on bpmn2odrl v9."
    ),
    version="0.9.0",
    contact={"name": "OEG – Universidad Politécnica de Madrid"},
    license_info={"name": "Apache 2.0"},
)


# ─────────────────────────────────────────────────────────────────────────────
# Health / root
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", summary="Health check", tags=["meta"])
def root():
    return {
        "service": "bpmn2odrl",
        "version": "0.9.0",
        "status": "ok",
        "endpoints": {
            "POST /convert":          "Upload BPMN → receive ODRL JSON-LD in response body",
            "POST /convert/download": "Upload BPMN → download ODRL .jsonld file",
            "GET  /docs":             "Interactive Swagger UI",
            "GET  /openapi.json":     "OpenAPI schema",
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Core conversion endpoint  (returns JSON)
# ─────────────────────────────────────────────────────────────────────────────

@app.post(
    "/convert",
    summary="Convert BPMN to ODRL (JSON response)",
    tags=["conversion"],
    response_description="ODRL Set policy in JSON-LD format",
)
async def convert(
    file: Annotated[UploadFile, File(description="BPMN 2.0 XML file (.bpmn or .xml)")],
    process_label: Annotated[
        str,
        Form(description="Human-readable label for the process (used as policy @id prefix)"),
    ] = "Process",
    verbose: Annotated[
        bool,
        Form(description="Enable verbose internal logging (server-side only)"),
    ] = False,
):
    """
    Upload a BPMN 2.0 XML file and receive back a fully formed ODRL Set policy
    in JSON-LD.

    The response body is the ODRL policy object directly. A `_meta` field is
    included with summary counts (duties, permissions, prohibitions, etc.).
    """
    _validate_upload(file)
    xml_bytes = await file.read()

    try:
        policy = run_pipeline_in_memory(
            xml_bytes,
            process_label=process_label,
            verbose=verbose,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Pipeline error: {exc}") from exc

    return JSONResponse(content=policy, media_type="application/ld+json")


# ─────────────────────────────────────────────────────────────────────────────
# Download endpoint  (returns .jsonld file attachment)
# ─────────────────────────────────────────────────────────────────────────────

@app.post(
    "/convert/download",
    summary="Convert BPMN to ODRL (file download)",
    tags=["conversion"],
    response_description="ODRL .jsonld file attachment",
)
async def convert_download(
    file: Annotated[UploadFile, File(description="BPMN 2.0 XML file (.bpmn or .xml)")],
    process_label: Annotated[str, Form()] = "Process",
    verbose: Annotated[bool, Form()] = False,
):
    """
    Same as `/convert` but returns the ODRL policy as a downloadable
    `.jsonld` file attachment.
    """
    _validate_upload(file)
    xml_bytes = await file.read()

    original_stem = file.filename.rsplit(".", 1)[0] if file.filename else "policy"
    output_filename = f"{original_stem}.odrl.jsonld"

    try:
        policy = run_pipeline_in_memory(
            xml_bytes,
            process_label=process_label,
            verbose=verbose,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Pipeline error: {exc}") from exc

    json_bytes = json.dumps(policy, indent=2, ensure_ascii=False).encode("utf-8")

    return Response(
        content=json_bytes,
        media_type="application/ld+json",
        headers={
            "Content-Disposition": f'attachment; filename="{output_filename}"',
            "Content-Length": str(len(json_bytes)),
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────

def _validate_upload(file: UploadFile):
    if file.filename and not file.filename.lower().endswith((".bpmn", ".xml")):
        raise HTTPException(
            status_code=415,
            detail="Only .bpmn or .xml files are accepted.",
        )
    if file.content_type and file.content_type not in (
        "application/xml",
        "text/xml",
        "application/octet-stream",
        "application/bpmn+xml",
    ):
        # Don't hard-reject — browsers/curl send varied content types
        pass
