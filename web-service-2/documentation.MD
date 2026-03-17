# BPMN → ODRL API — Quick Reference

The service converts BPMN 2.0 XML process models into ODRL deontic policies (obligations, permissions, prohibitions) in JSON-LD format.

Base URL: `http://localhost:48241`

---

## Endpoints

### `GET /`
Health check. Returns service status.

### `POST /convert`
Upload a BPMN file and get the ODRL policy back in the response body.

**Request:** `multipart/form-data`

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | file | ✅ | `.bpmn` or `.xml` file |
| `process_label` | string | ❌ | Label for the process (default: `"Process"`) |
| `verbose` | boolean | ❌ | Server-side debug logging (default: `false`) |

**Response:** ODRL policy in JSON-LD (`application/ld+json`)

---

### `POST /convert/download`
Same as `/convert` but returns the policy as a downloadable `.jsonld` file attachment instead of inline JSON.

---

### `GET /docs`
Interactive Swagger UI — upload a file and test the API directly in the browser.

---

## Examples

### curl
```bash
curl -X POST http://localhost:48241/convert \
  -F "file=@restaurant.bpmn" \
  -F "process_label=Restaurant"
```

### HTML form
```html
<form action="http://localhost:48241/convert" method="post" enctype="multipart/form-data">
  <input type="file" name="file" accept=".bpmn,.xml" />
  <input type="text" name="process_label" value="MyProcess" />
  <button type="submit">Convert</button>
</form>
```

### JavaScript (fetch)
```javascript
const formData = new FormData();
formData.append("file", fileInput.files[0]);
formData.append("process_label", "MyProcess");

const response = await fetch("http://localhost:48241/convert", {
  method: "POST",
  body: formData,
});

const policy = await response.json();
console.log(policy);
```

---

## Response structure

The response is an ODRL Set policy in JSON-LD. A `_meta` field is included with summary counts:

```json
{
  "@context": { ... },
  "@type": "odrl:Set",
  "@id": "ex:myProcessPolicy",
  "odrl:obligation": [ ... ],
  "odrl:permission": [ ... ],
  "odrl:prohibition": [ ... ],
  "_meta": {
    "duties": 5,
    "constrained_duties": 2,
    "permissions": 3,
    "wait_prohibitions": 1,
    "role_prohibitions": 2,
    "cyclic_sccs": 0
  }
}
```

---

## Starting the service

```bash
chmod +x run.sh
./run.sh
```

Requires: `pip install fastapi uvicorn python-multipart`