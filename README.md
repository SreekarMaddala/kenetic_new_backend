# Kinetic ERP - Complete AWS SAM Micro-Lambdas Backend

Enterprise Serverless Construction ERP backend built with **AWS SAM (Serverless Application Model)**, **Python 3.13**, and **Amazon DynamoDB**.

Every endpoint method is implemented as an independent, single-responsibility AWS Lambda micro-function, covering all 3 core ERP roles:
1. **Software Provider (Super Admin)**
2. **Site Supervisor (Field Operations)**
3. **Operations Admin & Project Manager (Central Control & Governance)**

---

## 🏛️ Comprehensive Micro-Lambda Catalog (73 Micro-Functions)

### 1. 🏢 Software Provider (Super Admin) Endpoints

| Method | Endpoint | Lambda Handler File | Description |
|---|---|---|---|
| `POST` | `/super-admin/organizations` | `handlers/super_admin/create_organization.py` | Onboard new client organization / developer |
| `GET` | `/super-admin/organizations` | `handlers/super_admin/list_organizations.py` | List organizations with keyword & status filters |
| `GET` | `/super-admin/organizations/{orgId}` | `handlers/super_admin/get_organization.py` | Fetch organization details and limits |
| `PUT` | `/super-admin/organizations/{orgId}` | `handlers/super_admin/update_organization.py` | Update organization details & subscription plan |
| `PATCH` | `/super-admin/organizations/{orgId}/status` | `handlers/super_admin/update_organization_status.py` | Suspend, activate, or archive organization |
| `POST` | `/super-admin/employees` | `handlers/super_admin/create_employee.py` | Create new platform employee / role assignment |
| `GET` | `/super-admin/employees` | `handlers/super_admin/list_employees.py` | List platform employees by department/role/status |
| `GET` | `/super-admin/employees/{employeeId}` | `handlers/super_admin/get_employee.py` | Get employee profile details |
| `PUT` | `/super-admin/employees/{employeeId}` | `handlers/super_admin/update_employee.py` | Update employee information |
| `DELETE` | `/super-admin/employees/{employeeId}` | `handlers/super_admin/delete_employee.py` | Deactivate/delete employee profile |
| `GET` | `/super-admin/metrics` | `handlers/super_admin/get_metrics.py` | Aggregated platform dashboard analytics |

---

### 2. 👷 Site Supervisor (Field Operations) Endpoints

| Method | Endpoint | Lambda Handler File | Description |
|---|---|---|---|
| `POST` | `/supervisor/attendance/check-in` | `handlers/supervisor/check_in.py` | Geofenced GPS check-in (Haversine radius validation) |
| `POST` | `/supervisor/attendance/check-out` | `handlers/supervisor/check_out.py` | Clock-out & automatic shift duration calculation |
| `GET` | `/supervisor/attendance/history` | `handlers/supervisor/get_attendance_history.py` | Query attendance logs for supervisor or project |
| `POST` | `/supervisor/labour/attendance` | `handlers/supervisor/create_labour_attendance.py` | Record daily contractor trade headcount & OT |
| `GET` | `/supervisor/labour/attendance` | `handlers/supervisor/get_labour_attendance.py` | Query daily labour logs and costs |
| `POST` | `/supervisor/dpr` | `handlers/supervisor/create_dpr.py` | Submit Daily Progress Report & achieved quantities |
| `GET` | `/supervisor/dpr` | `handlers/supervisor/list_dpr.py` | List DPR submissions for a project |
| `GET` | `/supervisor/dpr/{dprId}` | `handlers/supervisor/get_dpr.py` | Retrieve detailed DPR with photos & notes |
| `POST` | `/supervisor/issues` | `handlers/supervisor/create_issue.py` | Log site snag/defect with blueprint X/Y pin coords |
| `GET` | `/supervisor/issues` | `handlers/supervisor/list_issues.py` | Query site defects with severity/priority filtering |
| `PATCH` | `/supervisor/issues/{issueId}` | `handlers/supervisor/update_issue_status.py` | Update issue status (Open $\rightarrow$ In Review $\rightarrow$ Resolved $\rightarrow$ Closed) |
| `POST` | `/supervisor/materials/grn` | `handlers/supervisor/create_material_grn.py` | Record site gate intake Goods Received Note (GRN) |
| `POST` | `/supervisor/materials/indents` | `handlers/supervisor/create_material_indent.py` | Raise urgent requisition for central stock |
| `GET` | `/supervisor/materials/stock` | `handlers/supervisor/get_material_stock.py` | View site material stock alerts & intake history |
| `POST` | `/supervisor/logistics/trips` | `handlers/supervisor/create_logistics_trip.py` | Log carrier trip, load tonnage, distance & rental cost |
| `GET` | `/supervisor/logistics/trips` | `handlers/supervisor/list_logistics_trips.py` | Query logistics trips for a project |
| `POST` | `/supervisor/expenses` | `handlers/supervisor/create_expense.py` | Submit field expense claim (food/mess/travel) |
| `GET` | `/supervisor/expenses` | `handlers/supervisor/list_expenses.py` | Query submitted expense claims & approval status |

---

### 3. 🏗️ Operations Admin & Project Manager Endpoints

#### Projects Portfolio
| Method | Endpoint | Lambda Handler File | Description |
|---|---|---|---|
| `POST` | `/projects` | `handlers/operations_admin/projects/create_project.py` | Setup new construction site & budget |
| `GET` | `/projects` | `handlers/operations_admin/projects/list_projects.py` | List portfolio projects & progress |
| `GET` | `/projects/{projectId}` | `handlers/operations_admin/projects/get_project.py` | Project overview & cashflow |
| `PUT` | `/projects/{projectId}` | `handlers/operations_admin/projects/update_project.py` | Update project parameters & supervisor |
| `DELETE` | `/projects/{projectId}` | `handlers/operations_admin/projects/delete_project.py` | Archive project |

#### Bill of Quantities (BOQ)
| Method | Endpoint | Lambda Handler File | Description |
|---|---|---|---|
| `POST` | `/projects/{projectId}/boq` | `handlers/operations_admin/boq/create_boq_item.py` | Add BOQ line item |
| `GET` | `/projects/{projectId}/boq` | `handlers/operations_admin/boq/list_boq_items.py` | List BOQ line items vs budget spent |
| `PUT` | `/projects/{projectId}/boq/{boqId}` | `handlers/operations_admin/boq/update_boq_item.py` | Update BOQ quantities and rates |
| `DELETE` | `/projects/{projectId}/boq/{boqId}` | `handlers/operations_admin/boq/delete_boq_item.py` | Remove BOQ item |

#### Subcontractors
| Method | Endpoint | Lambda Handler File | Description |
|---|---|---|---|
| `POST` | `/projects/{projectId}/subcontractors` | `handlers/operations_admin/subcontractors/create_subcontractor.py` | Onboard trade contractor |
| `GET` | `/projects/{projectId}/subcontractors` | `handlers/operations_admin/subcontractors/list_subcontractors.py` | List contractors, billing & quality rating |
| `GET` | `/projects/{projectId}/subcontractors/{subcontractorId}` | `handlers/operations_admin/subcontractors/get_subcontractor.py` | Subcontractor profile |
| `PUT` | `/projects/{projectId}/subcontractors/{subcontractorId}` | `handlers/operations_admin/subcontractors/update_subcontractor.py` | Update contract terms & retentions |
| `DELETE` | `/projects/{projectId}/subcontractors/{subcontractorId}` | `handlers/operations_admin/subcontractors/delete_subcontractor.py` | Remove contractor |

#### Drawings & Documents
| Method | Endpoint | Lambda Handler File | Description |
|---|---|---|---|
| `POST` | `/projects/{projectId}/drawings` | `handlers/operations_admin/drawings/create_drawing.py` | Register CAD/DWG blueprint drawing sheet |
| `GET` | `/projects/{projectId}/drawings` | `handlers/operations_admin/drawings/list_drawings.py` | Query blueprints by discipline (Structural/MEP) |
| `DELETE` | `/projects/{projectId}/drawings/{drawingId}` | `handlers/operations_admin/drawings/delete_drawing.py` | Archive drawing sheet |
| `POST` | `/projects/{projectId}/documents` | `handlers/operations_admin/documents/create_document.py` | Register compliance certificate/NOC/contract |
| `GET` | `/projects/{projectId}/documents` | `handlers/operations_admin/documents/list_documents.py` | List documents by category |
| `DELETE` | `/projects/{projectId}/documents/{documentId}` | `handlers/operations_admin/documents/delete_document.py` | Delete document record |

#### Finance & Billing
| Method | Endpoint | Lambda Handler File | Description |
|---|---|---|---|
| `POST` | `/projects/{projectId}/bills` | `handlers/operations_admin/finance/create_bill.py` | Generate Running Account (RA) milestone bill |
| `GET` | `/projects/{projectId}/bills` | `handlers/operations_admin/finance/list_bills.py` | List bills with approval status |
| `GET` | `/projects/{projectId}/bills/{billId}` | `handlers/operations_admin/finance/get_bill.py` | RA bill details & structural certifications |
| `PATCH` | `/projects/{projectId}/bills/{billId}/status` | `handlers/operations_admin/finance/update_bill_status.py` | Authorize / hold bill payout |
| `POST` | `/payments` | `handlers/operations_admin/finance/create_vendor_payment.py` | Schedule 3-way matched vendor invoice payment |
| `GET` | `/payments` | `handlers/operations_admin/finance/list_vendor_payments.py` | List scheduled, released & overdue payments |
| `PATCH` | `/payments/{paymentId}/status` | `handlers/operations_admin/finance/update_payment_status.py` | Release / clear vendor payment |
| `PATCH` | `/projects/{projectId}/expenses/{expenseId}/status` | `handlers/operations_admin/finance/approve_field_expense.py` | PM audit and approval of field claims |

#### Quality Inspections & Heavy Equipment
| Method | Endpoint | Lambda Handler File | Description |
|---|---|---|---|
| `POST` | `/projects/{projectId}/inspections` | `handlers/operations_admin/site_ops/create_inspection.py` | Schedule QA/QC inspection checklist |
| `GET` | `/projects/{projectId}/inspections` | `handlers/operations_admin/site_ops/list_inspections.py` | List quality inspections & scores |
| `PATCH` | `/projects/{projectId}/inspections/{inspectionId}` | `handlers/operations_admin/site_ops/update_inspection.py` | Complete inspection & sign-off |
| `POST` | `/projects/{projectId}/equipment` | `handlers/operations_admin/site_ops/create_equipment.py` | Register heavy machinery (Cranes, Pumps, JCBs) |
| `GET` | `/projects/{projectId}/equipment` | `handlers/operations_admin/site_ops/list_equipment.py` | List active, idle & maintenance machinery |
| `PUT` | `/projects/{projectId}/equipment/{equipmentId}` | `handlers/operations_admin/site_ops/update_equipment.py` | Update equipment operating hours & rental rates |

#### Supply Chain: Master Vendors & Central Inventory
| Method | Endpoint | Lambda Handler File | Description |
|---|---|---|---|
| `POST` | `/vendors` | `handlers/operations_admin/supply_chain/create_vendor.py` | Add supplier to central Party Library |
| `GET` | `/vendors` | `handlers/operations_admin/supply_chain/list_vendors.py` | List vendors with rating and credit limits |
| `GET` | `/vendors/{vendorId}` | `handlers/operations_admin/supply_chain/get_vendor.py` | Vendor profile & transaction history |
| `PUT` | `/vendors/{vendorId}` | `handlers/operations_admin/supply_chain/update_vendor.py` | Update vendor terms |
| `POST` | `/inventory` | `handlers/operations_admin/supply_chain/create_inventory_item.py` | Register master stock SKU item |
| `GET` | `/inventory` | `handlers/operations_admin/supply_chain/list_inventory_items.py` | List warehouse stock levels & alerts |
| `PUT` | `/inventory/{itemId}` | `handlers/operations_admin/supply_chain/update_inventory_item.py` | Update stock counts & reorder thresholds |

#### Governance: Payroll, Analytics & Global Settings
| Method | Endpoint | Lambda Handler File | Description |
|---|---|---|---|
| `POST` | `/projects/{projectId}/payroll/disburse` | `handlers/operations_admin/governance/disburse_payroll.py` | Process attendance-backed wage disbursement |
| `GET` | `/projects/{projectId}/payroll` | `handlers/operations_admin/governance/list_payroll_records.py` | List payroll cycles & wage totals |
| `GET` | `/dashboard/analytics` | `handlers/operations_admin/governance/get_dashboard_analytics.py` | Cross-project portfolio KPIs & health |
| `GET` | `/reports/executive` | `handlers/operations_admin/governance/get_executive_reports.py` | Executive financial summary & vendor scores |
| `GET` | `/settings` | `handlers/operations_admin/governance/get_settings.py` | Retrieve global tax slabs & platform config |
| `PUT` | `/settings` | `handlers/operations_admin/governance/update_settings.py` | Update platform settings |

---

## 🗄️ DynamoDB Table Schemas (22 Tables)

1. `kinetic-organizations` (PK: `orgId`)
2. `kinetic-employees` (PK: `employeeId`)
3. `kinetic-supervisor-attendance` (PK: `supervisorId`, SK: `date`)
4. `kinetic-labour-attendance` (PK: `projectId`, SK: `recordKey`)
5. `kinetic-daily-progress-reports` (PK: `projectId`, SK: `dprId`)
6. `kinetic-site-issues` (PK: `projectId`, SK: `issueId`)
7. `kinetic-material-logs` (PK: `projectId`, SK: `logId`)
8. `kinetic-logistics-trips` (PK: `projectId`, SK: `tripId`)
9. `kinetic-supervisor-expenses` (PK: `supervisorId`, SK: `expenseId`)
10. `kinetic-projects` (PK: `projectId`)
11. `kinetic-boq` (PK: `projectId`, SK: `boqId`)
12. `kinetic-subcontractors` (PK: `projectId`, SK: `subcontractorId`)
13. `kinetic-drawings` (PK: `projectId`, SK: `drawingId`)
14. `kinetic-documents` (PK: `projectId`, SK: `documentId`)
15. `kinetic-bills` (PK: `projectId`, SK: `billId`)
16. `kinetic-vendor-payments` (PK: `paymentId`)
17. `kinetic-inspections` (PK: `projectId`, SK: `inspectionId`)
18. `kinetic-equipment` (PK: `projectId`, SK: `equipmentId`)
19. `kinetic-vendors` (PK: `vendorId`)
20. `kinetic-inventory` (PK: `itemId`)
21. `kinetic-payroll-disbursements` (PK: `projectId`, SK: `cycleId`)
22. `kinetic-settings` (PK: `settingKey`)

---

## 🚀 Quickstart & Development

### 1. Run Automated Unit Tests (with Mock DynamoDB)
```bash
cd backend
pytest tests/ -v
```

### 2. Build SAM Application
```bash
sam build
```

### 3. Local API Emulation
```bash
sam local start-api --port 3001
```

### 4. Deploy to AWS
```bash
sam deploy --config-env prod
```
