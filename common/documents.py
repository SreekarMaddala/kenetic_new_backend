"""Document validation and signed storage access."""
from common.authz import AuthorizationError
import boto3
from common.dynamo import get_table
import os
import uuid
from common.workflow_helpers import number


def files(identity, method, path, body, pk, org, pid, resource, rid):
    bucket = os.environ.get("DOCUMENTS_BUCKET")
    if not bucket:
        raise ValueError("Document storage is not configured. Deploy the updated backend template.")
    s3 = boto3.client("s3")
    if method == "POST" and rid == "upload":
        size = number(body.get("size"), "File size", True)
        if size > 20 * 1024 * 1024:
            raise ValueError("Files must be at most 20 MB")
        filename = str(body.get("name", "")).strip()
        if not filename:
            raise ValueError("Filename is required")
        content_type = str(body.get("contentType") or "application/octet-stream")
        key = f"{org}/{pid}/{uuid.uuid4().hex}"
        post = s3.generate_presigned_post(bucket, key, Fields={"Content-Type": content_type},
            Conditions=[["content-length-range", 1, 20 * 1024 * 1024], {"Content-Type": content_type}], ExpiresIn=300)
        return {"upload": post, "storageKey": key}
    if method == "POST" and path.endswith("/extract"):
        record = get_table("DOCUMENT_CONTROL_TABLE").get_item(Key={"PK": pk, "SK": f"{resource.upper()}#{rid}"}, ConsistentRead=True).get("Item")
        if not record or not str(record.get("storageKey", "")).startswith(f"{org}/{pid}/"):
            raise ValueError("Upload the invoice to this project first")
        metadata = s3.head_object(Bucket=bucket, Key=record["storageKey"])
        if metadata.get("ContentLength", 0) > 5 * 1024 * 1024 or metadata.get("ContentType") not in {"image/jpeg", "image/png"}:
            raise ValueError("Invoice extraction accepts JPEG or PNG images up to 5 MB")
        analysis = boto3.client("textract").analyze_expense(Document={"S3Object": {"Bucket": bucket, "Name": record["storageKey"]}})
        documents = analysis.get("ExpenseDocuments", [])
        if not documents:
            raise ValueError("No invoice fields were detected. Enter the bill manually.")
        fields = {f.get("Type", {}).get("Text"): f.get("ValueDetection", {}).get("Text", "") for f in documents[0].get("SummaryFields", [])}
        return {"vendor": fields.get("VENDOR_NAME", ""), "invoice": fields.get("INVOICE_RECEIPT_ID", ""),
                "total": fields.get("TOTAL", ""), "date": fields.get("INVOICE_RECEIPT_DATE", ""), "tax": fields.get("TAX", ""),
                "documentId": rid}
    if method == "GET" and path.endswith("/download"):
        table = get_table("DOCUMENT_CONTROL_TABLE")
        record = table.get_item(Key={"PK": pk, "SK": f"{resource.upper()}#{rid}"}, ConsistentRead=True).get("Item")
        if not record or record.get("orgId") != org or not record.get("storageKey"):
            raise ValueError("No file is attached to this record")
        if not str(record["storageKey"]).startswith(f"{org}/{pid}/"):
            raise AuthorizationError("File is not in this project")
        return {"url": s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": record["storageKey"], "ResponseContentDisposition": "attachment"}, ExpiresIn=300)}
    if method == "POST" and not rid:
        key = body.get("storageKey", "")
        if not isinstance(key, str) or not key.startswith(f"{org}/{pid}/") or ".." in key:
            raise ValueError("Upload a file to this project first")
        s3.head_object(Bucket=bucket, Key=key)
    return None
