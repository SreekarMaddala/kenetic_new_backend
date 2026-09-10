# Kinetic Backend Scripts

Utility scripts for setting up and managing the Kinetic DynamoDB backend.

---

## `create_tables.py` — DynamoDB Table Setup

Creates all **22 DynamoDB tables** required by the Kinetic ERP backend.
Reads the same table schema defined in `template.yaml`.

### Prerequisites

```bash
pip install boto3
```

Make sure you have AWS credentials configured:
```bash
aws configure        # sets up ~/.aws/credentials
# OR set env vars:
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=ap-south-1
```

---

### Usage

#### ✅ Create all tables on AWS (Mumbai region)
```bash
python scripts/create_tables.py
```

#### ✅ Create against DynamoDB Local (port 8000)
```bash
python scripts/create_tables.py --local
```

#### ✅ Custom endpoint (e.g. Docker DynamoDB Local on different port)
```bash
python scripts/create_tables.py --endpoint http://localhost:8001
```

#### 📋 List all existing Kinetic tables and their status
```bash
python scripts/create_tables.py --list
```

#### ♻️ Delete and recreate all tables (fresh start — destroys data!)
```bash
python scripts/create_tables.py --recreate
```

#### ☁️ Different AWS region
```bash
python scripts/create_tables.py --region us-east-1
```

---

### All 22 Tables Created

| # | Table | PK | SK |
|---|---|---|---|
| 1 | `kinetic-organizations` | `orgId` | — |
| 2 | `kinetic-employees` | `employeeId` | — |
| 3 | `kinetic-supervisor-attendance` | `supervisorId` | `date` |
| 4 | `kinetic-labour-attendance` | `projectId` | `recordKey` |
| 5 | `kinetic-daily-progress-reports` | `projectId` | `dprId` |
| 6 | `kinetic-site-issues` | `projectId` | `issueId` |
| 7 | `kinetic-material-logs` | `projectId` | `logId` |
| 8 | `kinetic-logistics-trips` | `projectId` | `tripId` |
| 9 | `kinetic-supervisor-expenses` | `supervisorId` | `expenseId` |
| 10 | `kinetic-projects` | `projectId` | — |
| 11 | `kinetic-project-milestones` | `projectId` | `milestoneId` |
| 12 | `kinetic-boq` | `projectId` | `boqId` |
| 13 | `kinetic-subcontractors` | `projectId` | `subcontractorId` |
| 14 | `kinetic-drawings` | `projectId` | `drawingId` |
| 15 | `kinetic-documents` | `projectId` | `documentId` |
| 16 | `kinetic-bills` | `projectId` | `billId` |
| 17 | `kinetic-vendor-payments` | `paymentId` | — |
| 18 | `kinetic-inspections` | `projectId` | `inspectionId` |
| 19 | `kinetic-equipment` | `projectId` | `equipmentId` |
| 20 | `kinetic-vendors` | `vendorId` | — |
| 21 | `kinetic-inventory` | `itemId` | — |
| 22 | `kinetic-warehouse-logs` | `id` | — |
| 23 | `kinetic-payroll-disbursements` | `projectId` | `cycleId` |
| 24 | `kinetic-settings` | `settingKey` | — |

> All tables use `PAY_PER_REQUEST` billing — no capacity provisioning needed.

---

### Running DynamoDB Local (Docker)

If you want to test without AWS credentials:

```bash
docker run -p 8000:8000 amazon/dynamodb-local
python scripts/create_tables.py --local
```
