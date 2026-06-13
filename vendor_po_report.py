#!/usr/bin/env python3
"""
JIT4You Vendor Open PO Report
==============================
Fetches all non-cancelled Purchase Orders from Vtiger CRM,
checks receipt notes for received quantities, computes open items,
and emails each vendor an interactive HTML form where they can
update expected availability dates and notes per item.

Vendor responses are emailed to customersupport@jit4you.com and ETAs updated in Vtiger directly from the browser form.

Usage:
  python vendor_po_report.py                # Normal run — email all vendors
  python vendor_po_report.py --no-email     # Generate HTML files only
  python vendor_po_report.py --dry-run      # Preview counts, no reports
  python vendor_po_report.py --vendor "ALDX"  # Only send to specific vendor

  # Process vendor ETA form submissions → update PO line items in Vtiger:
  python vendor_po_report.py --process-updates --json '{"vendor_name":"...","items":[...]}'
  python vendor_po_report.py --process-updates --file submission.json
  python vendor_po_report.py --process-updates --json '...' --dry-run
"""

import json, base64, time, urllib.parse, urllib.request, ssl, os, sys, argparse
from datetime import datetime
from collections import defaultdict

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
CONFIG = {
    "vtiger_rest_base":  "https://jit4youinc.od2.vtiger.com/restapi/v1/vtiger/default",
    "vtiger_user":       "customersupport@jit4you.com",
    "vtiger_accesskey":  "fIPkOulq0BaA5y2s",

    # Resend — vendor PO outbound email
    "resend_api_key":  "re_qWiD9N4f_BbwZXDFFATjDyjZ9BSXZ4f6r",
    "resend_from":     "JIT4Labs Purchasing <customersupport@jit4you.com>",

    # GitHub Pages hosting for vendor forms
    "github_repo":   "JIT4Labs1/vendor-po-updates",
    "github_token":  "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",
    "github_pages_base": "https://JIT4Labs1.github.io/vendor-po-updates",

    # Custom fields on PO line items for vendor ETA and notes
    "po_lineitem_eta_field": "cf_purchaseorder_eta",
    "po_lineitem_notes_field": "cf_purchaseorder_notes",

    # BCC — always send a copy to this address
    "bcc_email": "customersupport@jit4you.com",

    # Vendors to exclude from reports
    "exclude_vendors": ["Conmed"],

    # Rate limiting
    "delay_between_calls": 0.3,

    # Output directory
    "output_dir": os.path.dirname(os.path.abspath(__file__)),
}

# Allow GITHUB_TOKEN from environment
if not CONFIG["github_token"]:
    CONFIG["github_token"] = os.environ.get("GITHUB_TOKEN", "")

VTIGER_BASE = "https://jit4youinc.od2.vtiger.com"
SKIP_ITEMS = ['shipping', 'tax', 'ca sales tax']
ctx = ssl.create_default_context()

# Embedded logo as base64 data URI (always shows in emails)
LOGO_DATA_URI = "data:image/jpeg;base64,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"


# ─────────────────────────────────────────────
# UTILITY
# ─────────────────────────────────────────────
def http_request(url, method="GET", headers=None, data=None, json_body=None):
    if headers is None:
        headers = {}
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif data and isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode("utf-8")
    elif data and isinstance(data, str):
        data = data.encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        log(f"  HTTP {e.code} error: {error_body[:300]}")
        raise


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


# ─────────────────────────────────────────────
# VTIGER REST API (Basic Auth)
# ─────────────────────────────────────────────
class VtigerAPI:
    def __init__(self, rest_base, user, accesskey):
        self.rest_base = rest_base.rstrip("/")
        creds = base64.b64encode(f"{user}:{accesskey}".encode()).decode()
        self.auth_headers = {"Authorization": f"Basic {creds}"}

    def login(self):
        try:
            self.query("SELECT purchaseorder_no FROM PurchaseOrder LIMIT 0, 1;")
            log("Vtiger REST API: Connected successfully")
        except Exception as e:
            raise Exception(f"Vtiger REST API connection failed: {e}")

    def query(self, sql):
        url = f"{self.rest_base}/query?query={urllib.parse.quote(sql)}"
        resp = http_request(url, headers=dict(self.auth_headers))
        if not resp.get("success"):
            raise Exception(f"Vtiger query failed: {resp}")
        return resp["result"]

    def query_all(self, sql_template, delay=0.3):
        all_results = []
        offset = 0
        while True:
            sql = f"{sql_template} LIMIT {offset}, 100;"
            results = self.query(sql)
            if not results:
                break
            all_results.extend(results)
            if len(results) < 100:
                break
            offset += 100
            time.sleep(delay)
        return all_results

    def retrieve(self, record_id):
        url = f"{self.rest_base}/retrieve?id={urllib.parse.quote(record_id)}"
        resp = http_request(url, headers=dict(self.auth_headers))
        if not resp.get("success"):
            raise Exception(f"Vtiger retrieve failed for {record_id}: {resp}")
        return resp["result"]

    def create(self, element_type, data):
        url = f"{self.rest_base}/create"
        payload = {
            "elementType": element_type,
            "element": json.dumps(data),
        }
        resp = http_request(url, method="POST", headers=dict(self.auth_headers), data=payload)
        if not resp.get("success"):
            raise Exception(f"Vtiger create failed: {resp}")
        return resp["result"]

    def update(self, data):
        url = f"{self.rest_base}/revise"
        payload = {
            "element": json.dumps(data),
        }
        resp = http_request(url, method="POST", headers=dict(self.auth_headers), data=payload)
        if not resp.get("success"):
            raise Exception(f"Vtiger update failed: {resp}")
        return resp["result"]


# ─────────────────────────────────────────────
# DATA EXTRACTION
# ─────────────────────────────────────────────
def extract_open_pos(vt, dry_run=False, vendor_filter=None):
    """Start from 2026 open SOs, find their linked POs, check receipts, group by vendor."""

    # STEP 1: Fetch 2026 non-cancelled Sales Orders (same as open_orders_report)
    log("Step 1: Fetching 2026 Sales Orders...")
    all_sos_raw = vt.query_all(
        "SELECT id, salesorder_no, sostatus, createdtime, account_id "
        "FROM SalesOrder"
    )
    all_sos = [s for s in all_sos_raw if "2026" in str(s.get("createdtime", ""))]
    non_cancelled = [s for s in all_sos if s.get("sostatus") != "Cancelled"]
    log(f"  Found {len(all_sos_raw)} total SOs, {len(all_sos)} in 2026, {len(non_cancelled)} non-cancelled")

    # Build SO record ID -> SO number map + SO -> account_id map
    so_id_to_num = {s["id"]: s.get("salesorder_no", "") for s in non_cancelled}
    so_id_to_acct = {s["id"]: s.get("account_id", "") for s in non_cancelled}
    so_ids_set = set(so_id_to_num.keys())

    # Resolve customer names from account IDs
    log("  Resolving customer names...")
    acct_ids = set(v for v in so_id_to_acct.values() if v)
    acct_names = {}
    for acct_id in acct_ids:
        try:
            acct = vt.retrieve(acct_id)
            acct_names[acct_id] = acct.get("accountname", "Unknown")
        except Exception:
            acct_names[acct_id] = "Unknown"
        time.sleep(CONFIG["delay_between_calls"])
    # Build SO ID -> customer name
    so_id_to_customer = {sid: acct_names.get(so_id_to_acct.get(sid, ""), "Unknown") for sid in so_ids_set}
    log(f"  Resolved {len(acct_names)} customer names")

    # STEP 2: Fetch all POs and filter to those linked to our 2026 SOs
    log("Step 2: Fetching Purchase Orders linked to 2026 SOs...")
    all_pos_raw = vt.query_all(
        "SELECT id, purchaseorder_no, postatus, vendor_id, createdtime, salesorder_id "
        "FROM PurchaseOrder"
    )

    # Filter: exclude only cancelled POs; all other statuses stay (receipt notes determine what's open)
    linked_pos = [p for p in all_pos_raw
                  if p.get("postatus", "") != "Cancelled"
                  and p.get("salesorder_id", "") in so_ids_set]
    log(f"  Found {len(all_pos_raw)} total POs, {len(linked_pos)} linked to 2026 SOs (non-cancelled)")

    if not linked_pos:
        log("No linked POs found")
        return {}

    # STEP 3: Resolve vendor names and emails
    log("Step 3: Resolving vendor info...")
    vendor_ids = set(p.get("vendor_id", "") for p in linked_pos if p.get("vendor_id"))
    vendor_info = {}  # vendor_id -> {name, email}
    for vid in vendor_ids:
        try:
            vendor = vt.retrieve(vid)
            _first = (vendor.get("firstname", "") or "").strip()
            _last = (vendor.get("lastname", "") or "").strip()
            _contact = (_first + " " + _last).strip()
            vendor_info[vid] = {
                "name": vendor.get("vendorname", vendor.get("label", "Unknown")),
                "email": vendor.get("email", ""),
                "contact_name": _contact,
            }
        except Exception:
            vendor_info[vid] = {"name": "Unknown", "email": "", "contact_name": ""}
        time.sleep(CONFIG["delay_between_calls"])
    log(f"  Resolved {len(vendor_info)} vendors: {[v['name'] for v in vendor_info.values()]}")

    # Apply vendor filter if specified
    if vendor_filter:
        vendor_filter_lower = vendor_filter.lower()
        matching_vids = [vid for vid, info in vendor_info.items()
                         if vendor_filter_lower in info["name"].lower()]
        linked_pos = [p for p in linked_pos if p.get("vendor_id") in matching_vids]
        log(f"  Filtered to vendor '{vendor_filter}': {len(linked_pos)} POs")

    if dry_run:
        by_vendor = defaultdict(int)
        for po in linked_pos:
            vid = po.get("vendor_id", "")
            vname = vendor_info.get(vid, {}).get("name", "Unknown")
            by_vendor[vname] += 1
        for vname, count in sorted(by_vendor.items()):
            vemail = ""
            for vid, info in vendor_info.items():
                if info["name"] == vname:
                    vemail = info["email"]
                    break
            log(f"    {vname}: {count} active POs (email: {vemail or 'N/A'})")
        return {}

    # STEP 4: Retrieve PO details with line items
    log("Step 4: Retrieving PO details + line items...")
    po_details = {}  # po_num -> detail
    all_product_ids = set()
    for po in linked_pos:
        try:
            detail = vt.retrieve(po["id"])
            po_num = detail.get("purchaseorder_no", po.get("purchaseorder_no", ""))
            po_details[po_num] = detail
            line_items = detail.get("LineItems", detail.get("lineItems", []))
            if isinstance(line_items, list):
                for li in line_items:
                    pid = li.get("productid", "")
                    if pid:
                        all_product_ids.add(pid)
        except Exception as e:
            log(f"  Warning: Failed to retrieve PO {po.get('id')}: {e}")
        time.sleep(CONFIG["delay_between_calls"])
    log(f"  Retrieved {len(po_details)} PO details, {len(all_product_ids)} unique products")

    # STEP 5: Resolve product names
    log("Step 5: Resolving product names...")
    product_names = {}
    for pid in all_product_ids:
        try:
            prod = vt.retrieve(pid)
            product_names[pid] = prod.get("productname", prod.get("label", ""))
        except Exception:
            product_names[pid] = ""
        time.sleep(CONFIG["delay_between_calls"])
    log(f"  Resolved {len(product_names)} product names")

    # STEP 6: Fetch ReceiptNotes — only count items with receiptnote_status = "Received"
    log("Step 6: Fetching Receipt Notes (module: ReceiptNotes)...")
    receipt_map = defaultdict(lambda: defaultdict(float))  # po_num -> product_id -> received_qty

    # Build PO ID -> PO num map
    po_id_to_num = {po["id"]: po.get("purchaseorder_no", "") for po in linked_pos}

    receipts_raw = vt.query_all(
        "SELECT id, related_to, receiptnote_status FROM ReceiptNotes"
    )
    log(f"  Found {len(receipts_raw)} total receipt notes")

    # Only process receipts linked to our POs AND with status "Received"
    received_count = 0
    skipped_count = 0
    for receipt in receipts_raw:
        related_po_id = receipt.get("related_to", "")
        if related_po_id not in po_id_to_num:
            continue

        # Only count receipt notes with status "Received"
        status = receipt.get("receiptnote_status", "")
        if status.lower() != "received":
            skipped_count += 1
            continue

        po_num = po_id_to_num[related_po_id]
        try:
            detail = vt.retrieve(receipt["id"])
            line_items = detail.get("LineItems", detail.get("lineItems", []))
            if isinstance(line_items, list):
                for li in line_items:
                    pid = li.get("productid", "")
                    qty = float(li.get("quantity", li.get("qty", 0)))
                    if pid and qty > 0:
                        receipt_map[po_num][pid] += qty
                        received_count += 1
        except Exception:
            pass
        time.sleep(CONFIG["delay_between_calls"])
    log(f"  Processed: {received_count} received items counted, {skipped_count} non-received skipped")
    log(f"  Built receipt map for {len(receipt_map)} POs")

    # STEP 7: Compute open items per vendor
    log("Step 7: Computing open items per vendor...")
    vendor_items = defaultdict(list)

    for po_num, detail in po_details.items():
        vid = detail.get("vendor_id", "")
        vinfo = vendor_info.get(vid, {"name": "Unknown", "email": ""})
        vendor_name = vinfo["name"]
        vendor_email = vinfo["email"]
        vendor_contact = vinfo.get("contact_name", "")
        po_status = detail.get("postatus", "")
        po_date = detail.get("createdtime", "").split(" ")[0] if detail.get("createdtime") else ""

        # Get linked SO number and customer name
        so_ref = detail.get("salesorder_id", "")
        linked_so = so_id_to_num.get(so_ref, "")
        customer_name = so_id_to_customer.get(so_ref, "Unknown")

        line_items = detail.get("LineItems", detail.get("lineItems", []))
        if not isinstance(line_items, list):
            continue

        for li in line_items:
            pid = li.get("productid", "")
            if not pid:
                continue

            product_name = product_names.get(pid, "") or li.get("productid_display", "")
            if not product_name:
                continue

            pname_lower = product_name.lower()
            if any(skip in pname_lower for skip in SKIP_ITEMS):
                continue

            ordered_qty = float(li.get("quantity", li.get("qty", 0)))

            # Only count as received if covered by a receipt note with status "Received"
            received_qty = receipt_map.get(po_num, {}).get(pid, 0)

            open_qty = ordered_qty - received_qty
            if open_qty <= 0:
                continue

            unit_price = float(li.get("listprice", li.get("price", 0)))

            vendor_items[vendor_name].append({
                "vendor_name": vendor_name,
                "vendor_email": vendor_email,
                "vendor_contact_name": vendor_contact,
                "po_num": po_num,
                "po_id": detail.get("id", ""),
                "po_date": po_date,
                "customer": customer_name,
                "linked_so": linked_so,
                "product": product_name,
                "product_id": pid,
                "ordered_qty": ordered_qty,
                "received_qty": received_qty,
                "open_qty": open_qty,
                "unit_price": unit_price,
                "eta": (li.get(CONFIG["po_lineitem_eta_field"], "") or "").strip(),
            })

    # Sort items within each vendor by PO date ascending
    for vendor_name in vendor_items:
        vendor_items[vendor_name].sort(key=lambda r: r["po_date"])

    total_items = sum(len(items) for items in vendor_items.values())
    log(f"  {total_items} open items across {len(vendor_items)} vendors")
    return dict(vendor_items)


# ─────────────────────────────────────────────
# EMAIL BODY (read-only summary for the email)
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# ETA helpers (vendor-facing: confirm / update / overdue)
# ─────────────────────────────────────────────
def _eta_info(eta_str):
    """Parse an ETA string and classify it as future / past / missing / unknown.
    Returns a dict with display text, status tag, day-delta, and raw iso date."""
    if not eta_str:
        return {"display": "Not set", "status": "missing", "days": 0, "raw": ""}
    try:
        d = datetime.strptime(eta_str[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return {"display": eta_str, "status": "unknown", "days": 0, "raw": ""}
    today = datetime.now().date()
    delta = (d - today).days
    display = d.strftime("%b %d, %Y")
    if delta >= 0:
        return {"display": display, "status": "future", "days": delta, "raw": eta_str[:10]}
    return {"display": display, "status": "past", "days": -delta, "raw": eta_str[:10]}


def _eta_badge(info):
    """Return an HTML chip for the current ETA, with styling based on status."""
    s = info["status"]
    if s == "past":
        return (
            f'<span style="display:inline-block;padding:4px 10px;background:#fee;'
            f'border:1px solid #c0392b;color:#c0392b;border-radius:4px;font-size:11px;font-weight:700;white-space:nowrap;">'
            f'⚠ OVERDUE &middot; {info["display"]}</span>'
        )
    if s == "future":
        return (
            f'<span style="display:inline-block;padding:4px 10px;background:#e8f5e8;'
            f'border:1px solid #27ae60;color:#1e7e34;border-radius:4px;font-size:11px;font-weight:600;white-space:nowrap;">'
            f'{info["display"]}</span>'
        )
    if s == "unknown":
        return (
            f'<span style="display:inline-block;padding:4px 10px;background:#f5f5f5;'
            f'border:1px solid #ccc;color:#666;border-radius:4px;font-size:11px;white-space:nowrap;">'
            f'{info["display"]}</span>'
        )
    return '<span style="color:#999;font-style:italic;font-size:11px;">Not set</span>'


def generate_email_body(vendor_name, items, form_url=None, contact_name=""):
    """Branded vendor email — matches customer-order-status report design.
    Personal greeting uses vendor's primary contact first name when available."""
    logo_uri = LOGO_DATA_URI
    report_date = datetime.now().strftime("%B %d, %Y")
    total_items = len(items)
    total_pos = len(set(i["po_num"] for i in items))
    overdue_count = sum(1 for it in items if _eta_info(it.get("eta", ""))["status"] == "past")

    # Personalized greeting
    first = (contact_name or "").strip().split()[0] if contact_name and contact_name.strip() else ""
    if first:
        greeting = f"Hi {first},"
    else:
        greeting = f"Hi {vendor_name} team,"

    # Third summary card (Overdue) — red accent if >0, teal otherwise
    overdue_color = "#c0392b" if overdue_count else "#008080"
    overdue_value_color = "#c0392b" if overdue_count else "#101E3E"

    # Table rows
    rows_parts = []
    for idx, item in enumerate(items):
        info = _eta_info(item.get("eta", ""))
        bg = "#f8f9fa" if idx % 2 == 0 else "#ffffff"
        rows_parts.append(
            f'<tr style="background:{bg};border-bottom:1px solid #e9ecef;">'
            f'<td style="padding:10px 12px;font-size:13px;color:#101E3E;font-weight:600;">{item["po_num"]}</td>'
            f'<td style="padding:10px 12px;font-size:13px;color:rgba(16,30,62,0.75);white-space:nowrap;">{item["po_date"]}</td>'
            f'<td style="padding:10px 12px;font-size:13px;color:rgba(16,30,62,0.75);">{item.get("customer", "")}</td>'
            f'<td style="padding:10px 12px;font-size:13px;color:rgba(16,30,62,0.75);">{item["product"]}</td>'
            f'<td style="padding:10px 12px;font-size:13px;text-align:center;color:#101E3E;font-weight:600;">{item["open_qty"]:g}</td>'
            f'<td style="padding:10px 12px;font-size:13px;white-space:nowrap;">{_eta_badge(info)}</td>'
            f'</tr>'
        )
    table_rows = "\n".join(rows_parts)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Open Purchase Orders &mdash; {vendor_name} | JIT4Labs</title>
<link href="https://fonts.googleapis.com/css2?family=Open+Sans:wght@400;600;700;800&display=swap" rel="stylesheet">
</head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:'Open Sans',Arial,sans-serif;color:rgba(16,30,62,0.75);">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f0f2f5;padding:24px 0;">
<tr><td align="center">
<table role="presentation" width="760" cellpadding="0" cellspacing="0" style="max-width:760px;width:100%;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 1px 6px rgba(0,0,0,0.06);">

<!-- HEADER: white, logo left, title right, teal 3px border bottom -->
<tr><td style="background:#ffffff;padding:24px 32px;border-bottom:3px solid #008080;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr>
<td style="vertical-align:middle;">
<img src="{logo_uri}" alt="JIT4Labs" width="140" style="display:block;height:auto;">
</td>
<td style="text-align:right;vertical-align:middle;">
<p style="margin:0;font-size:20px;font-weight:700;color:#101E3E;letter-spacing:-0.3px;">Open Purchase Orders</p>
<p style="margin:4px 0 0 0;font-size:13px;color:rgba(16,30,62,0.55);">{vendor_name}</p>
<p style="margin:2px 0 0 0;font-size:11px;color:rgba(16,30,62,0.4);">{report_date}</p>
</td>
</tr>
</table>
</td></tr>

<!-- SUMMARY CARDS -->
<tr><td style="padding:28px 32px 12px 32px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr>
<td width="33%" style="padding:0 6px 0 0;">
<div style="background:#ffffff;border-radius:12px;padding:18px 20px;text-align:center;border-top:3px solid #008080;box-shadow:0 1px 4px rgba(0,0,0,0.06);">
<div style="font-size:28px;font-weight:800;color:#101E3E;line-height:1;">{total_pos}</div>
<div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-top:8px;font-weight:600;">Open POs</div>
</div>
</td>
<td width="33%" style="padding:0 3px;">
<div style="background:#ffffff;border-radius:12px;padding:18px 20px;text-align:center;border-top:3px solid #008080;box-shadow:0 1px 4px rgba(0,0,0,0.06);">
<div style="font-size:28px;font-weight:800;color:#101E3E;line-height:1;">{total_items}</div>
<div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-top:8px;font-weight:600;">Open Items</div>
</div>
</td>
<td width="33%" style="padding:0 0 0 6px;">
<div style="background:#ffffff;border-radius:12px;padding:18px 20px;text-align:center;border-top:3px solid {overdue_color};box-shadow:0 1px 4px rgba(0,0,0,0.06);">
<div style="font-size:28px;font-weight:800;color:{overdue_value_color};line-height:1;">{overdue_count}</div>
<div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-top:8px;font-weight:600;">Overdue</div>
</div>
</td>
</tr>
</table>
</td></tr>

<!-- GREETING + MESSAGE + CTA + SIGNATURE -->
<tr><td style="padding:20px 32px 16px 32px;">
<p style="margin:0 0 16px 0;font-size:16px;color:#101E3E;line-height:1.6;font-weight:600;">{greeting}</p>
<p style="margin:0 0 20px 0;font-size:15px;color:rgba(16,30,62,0.75);line-height:1.7;">Please find below the open purchase orders we have on file with {vendor_name}, along with the latest ETA you provided.</p>
<p style="margin:0 0 20px 0;font-size:15px;color:rgba(16,30,62,0.75);line-height:1.7;">Click the button below to update.</p>
<div style="text-align:left;margin-bottom:24px;">
<a href="{form_url or '#'}" style="display:inline-block;background:#008080;color:#ffffff !important;text-decoration:none;padding:12px 28px;border-radius:8px;font-size:14px;font-weight:600;letter-spacing:0.3px;">Open Update Form</a>
</div>
<p style="margin:0 0 4px 0;font-size:15px;color:rgba(16,30,62,0.75);line-height:1.6;">Thank you for your support,</p>
<p style="margin:12px 0 2px 0;font-size:15px;color:#101E3E;font-weight:700;line-height:1.4;">JIT4Labs</p>
<p style="margin:0;font-size:13px;color:rgba(16,30,62,0.55);line-height:1.4;">Irvine, CA 92620</p>
</td></tr>

<!-- DATA TABLE -->
<tr><td style="padding:12px 32px 24px 32px;">
<div style="border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.06);border:1px solid #e6e6ea;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
<thead>
<tr style="background:#101E3E;">
<th style="padding:12px;color:#ffffff;text-align:left;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;">PO #</th>
<th style="padding:12px;color:#ffffff;text-align:left;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;">PO Date</th>
<th style="padding:12px;color:#ffffff;text-align:left;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;">Customer</th>
<th style="padding:12px;color:#ffffff;text-align:left;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;">Product</th>
<th style="padding:12px;color:#ffffff;text-align:center;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;">Open</th>
<th style="padding:12px;color:#ffffff;text-align:left;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;">Current ETA</th>
</tr>
</thead>
<tbody>
{table_rows}
</tbody>
</table>
</div>
</td></tr>

<!-- CONTACT SECTION -->
<tr><td style="padding:0 32px 28px 32px;">
<div style="background:#f7f7f9;border-radius:12px;padding:16px 20px;">
<p style="margin:0 0 8px 0;font-size:13px;color:rgba(16,30,62,0.75);line-height:1.6;">Questions? Reach out any time:</p>
<p style="margin:0;font-size:13px;">
<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#008080" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:middle;margin-right:6px;"><rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/></svg>
<a href="mailto:customersupport@jit4you.com" style="color:#008080;text-decoration:none;font-weight:600;">customersupport@jit4you.com</a>
<span style="color:rgba(16,30,62,0.3);margin:0 10px;">|</span>
<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#008080" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:middle;margin-right:6px;"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"/></svg>
<a href="tel:+19493969194" style="color:#008080;text-decoration:none;font-weight:600;">(949) 396-9194</a>
</p>
</div>
</td></tr>

<!-- FOOTER -->
<tr><td style="background:#101E3E;padding:18px;text-align:center;">
<p style="color:rgba(255,255,255,0.7);font-size:11px;margin:0;">JIT4Labs &mdash; Your Backend Supply Chain, Simplified.</p>
</td></tr>

</table>
</td></tr></table>
</body></html>"""


# ─────────────────────────────────────────────
# INTERACTIVE HTML FORM (saved as attachment)
# ─────────────────────────────────────────────
def generate_vendor_form(vendor_name, items):
    """Standalone HTML form vendors open to update ETAs. Matches customer-order-status branding."""
    logo_uri = LOGO_DATA_URI
    github_token = CONFIG["github_token"]
    github_repo = CONFIG["github_repo"]
    report_date = datetime.now().strftime("%B %d, %Y")
    total_items = len(items)
    total_pos = len(set(i["po_num"] for i in items))
    overdue_count = sum(1 for it in items if _eta_info(it.get("eta", ""))["status"] == "past")
    overdue_color = "#c0392b" if overdue_count else "#008080"
    overdue_value_color = "#c0392b" if overdue_count else "#101E3E"

    # Hidden fields for JS submission
    hidden_fields_parts = []
    for idx, item in enumerate(items):
        item_id = f"{item['po_num']}_{item['product_id']}".replace("x", "").replace(" ", "")
        hidden_fields_parts.append(f'<input type="hidden" name="item_{idx}_po" value="{item["po_num"]}">')
        hidden_fields_parts.append(f'<input type="hidden" name="item_{idx}_po_id" value="{item.get("po_id", "")}">')
        hidden_fields_parts.append(f'<input type="hidden" name="item_{idx}_customer" value="{item.get("customer", "")}">')
        hidden_fields_parts.append(f'<input type="hidden" name="item_{idx}_product" value="{item["product"]}">')
        hidden_fields_parts.append(f'<input type="hidden" name="item_{idx}_product_id" value="{item["product_id"]}">')
        hidden_fields_parts.append(f'<input type="hidden" name="item_{idx}_open_qty" value="{item["open_qty"]:g}">')
        hidden_fields_parts.append(f'<input type="hidden" name="item_{idx}_unit_price" value="{item.get("unit_price", 0)}">')
        hidden_fields_parts.append(f'<input type="hidden" name="item_{idx}_id" value="{item_id}">')
    hidden_fields = "\n".join(hidden_fields_parts)

    # Table rows with ETA + inputs
    row_parts = []
    for idx, item in enumerate(items):
        bg = "#f8f9fa" if idx % 2 == 0 else "#ffffff"
        item_id = f"{item['po_num']}_{item['product_id']}".replace("x", "").replace(" ", "")
        info = _eta_info(item.get("eta", ""))
        prefill_date = info["raw"] if info["status"] == "future" else ""
        prefill_note = "Please update ETA" if info["status"] == "past" else ""

        row_parts.append(
            f'<tr style="background:{bg};border-bottom:1px solid #e9ecef;">'
            f'<td style="padding:10px 12px;font-size:13px;color:#101E3E;font-weight:600;">{item["po_num"]}</td>'
            f'<td style="padding:10px 12px;font-size:13px;color:rgba(16,30,62,0.75);white-space:nowrap;">{item["po_date"]}</td>'
            f'<td style="padding:10px 12px;font-size:13px;color:rgba(16,30,62,0.75);">{item.get("customer", "")}</td>'
            f'<td style="padding:10px 12px;font-size:13px;color:rgba(16,30,62,0.75);">{item["product"]}</td>'
            f'<td style="padding:10px 12px;font-size:13px;text-align:center;color:#101E3E;font-weight:600;">{item["open_qty"]:g}</td>'
            f'<td style="padding:10px 12px;font-size:13px;white-space:nowrap;">{_eta_badge(info)}</td>'
            f'<td style="padding:10px 12px;font-size:13px;">'
            f'<input type="date" name="eta_{item_id}" value="{prefill_date}" '
            f'style="width:148px;padding:6px 8px;border:1px solid #c4c4c4;border-radius:6px;'
            f"font-size:13px;font-family:'Open Sans',Arial,sans-serif;\">"
            f'</td>'
            f'<td style="padding:10px 12px;font-size:13px;">'
            f'<input type="text" name="note_{item_id}" value="{prefill_note}" placeholder="Add note..." '
            f'style="width:220px;padding:6px 8px;border:1px solid #c4c4c4;border-radius:6px;'
            f"font-size:13px;font-family:'Open Sans',Arial,sans-serif;\">"
            f'</td>'
            f'</tr>'
        )
    table_rows = "\n".join(row_parts)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JIT4Labs &mdash; Update Purchase Order ETAs &mdash; {vendor_name}</title>
<link href="https://fonts.googleapis.com/css2?family=Open+Sans:wght@400;600;700;800&display=swap" rel="stylesheet">
</head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:'Open Sans',Arial,sans-serif;color:rgba(16,30,62,0.75);">
<form id="vendorForm">
<input type="hidden" name="vendor_name" value="{vendor_name}">
<input type="hidden" name="item_count" value="{total_items}">
{hidden_fields}

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f0f2f5;padding:24px 0;">
<tr><td align="center">
<table role="presentation" width="1100" cellpadding="0" cellspacing="0" style="max-width:1100px;width:100%;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 1px 6px rgba(0,0,0,0.06);">

<!-- HEADER -->
<tr><td style="background:#ffffff;padding:24px 32px;border-bottom:3px solid #008080;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr>
<td style="vertical-align:middle;">
<img src="{logo_uri}" alt="JIT4Labs" width="140" style="display:block;height:auto;">
</td>
<td style="text-align:right;vertical-align:middle;">
<p style="margin:0;font-size:20px;font-weight:700;color:#101E3E;letter-spacing:-0.3px;">Open Purchase Orders &mdash; Update ETAs</p>
<p style="margin:4px 0 0 0;font-size:13px;color:rgba(16,30,62,0.55);">{vendor_name}</p>
<p style="margin:2px 0 0 0;font-size:11px;color:rgba(16,30,62,0.4);">{report_date}</p>
</td>
</tr>
</table>
</td></tr>

<!-- SUMMARY CARDS -->
<tr><td style="padding:28px 32px 12px 32px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr>
<td width="33%" style="padding:0 6px 0 0;">
<div style="background:#ffffff;border-radius:12px;padding:18px 20px;text-align:center;border-top:3px solid #008080;box-shadow:0 1px 4px rgba(0,0,0,0.06);">
<div style="font-size:28px;font-weight:800;color:#101E3E;line-height:1;">{total_pos}</div>
<div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-top:8px;font-weight:600;">Open POs</div>
</div>
</td>
<td width="33%" style="padding:0 3px;">
<div style="background:#ffffff;border-radius:12px;padding:18px 20px;text-align:center;border-top:3px solid #008080;box-shadow:0 1px 4px rgba(0,0,0,0.06);">
<div style="font-size:28px;font-weight:800;color:#101E3E;line-height:1;">{total_items}</div>
<div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-top:8px;font-weight:600;">Open Items</div>
</div>
</td>
<td width="33%" style="padding:0 0 0 6px;">
<div style="background:#ffffff;border-radius:12px;padding:18px 20px;text-align:center;border-top:3px solid {overdue_color};box-shadow:0 1px 4px rgba(0,0,0,0.06);">
<div style="font-size:28px;font-weight:800;color:{overdue_value_color};line-height:1;">{overdue_count}</div>
<div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-top:8px;font-weight:600;">Overdue</div>
</div>
</td>
</tr>
</table>
</td></tr>

<!-- INSTRUCTIONS -->
<tr><td style="padding:20px 32px 0 32px;">
<div style="background:#f7f7f9;border-radius:12px;padding:20px 24px;font-size:14px;color:rgba(16,30,62,0.85);line-height:1.7;">
<p style="margin:0 0 14px 0;font-size:15px;font-weight:700;color:#101E3E;">Please confirm or update each item's ETA.</p>
<ul style="margin:0 0 14px 0;padding-left:24px;line-height:1.9;">
<li><strong>Still valid?</strong> The Expected Date is pre-filled &mdash; just submit and we'll keep it.</li>
<li><strong>Needs updating?</strong> Change the date and (optionally) add a note.</li>
<li><strong style="color:#c0392b;">&#9888; OVERDUE items ({overdue_count}):</strong> the ETA has already passed. Please enter a new realistic date.</li>
</ul>
<p style="margin:0;">Click <strong>Submit Updates</strong> at the bottom when done.</p>
</div>
</td></tr>

<!-- TABLE -->
<tr><td style="padding:20px 32px 24px 32px;">
<div style="border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.06);border:1px solid #e6e6ea;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
<thead>
<tr style="background:#101E3E;">
<th style="padding:12px;color:#ffffff;text-align:left;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;">PO #</th>
<th style="padding:12px;color:#ffffff;text-align:left;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;">PO Date</th>
<th style="padding:12px;color:#ffffff;text-align:left;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;">Customer</th>
<th style="padding:12px;color:#ffffff;text-align:left;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;">Product</th>
<th style="padding:12px;color:#ffffff;text-align:center;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;">Open</th>
<th style="padding:12px;color:#ffffff;text-align:left;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;">Current ETA</th>
<th style="padding:12px;color:#ffffff;text-align:left;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;">Updated ETA</th>
<th style="padding:12px;color:#ffffff;text-align:left;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;">Notes</th>
</tr>
</thead>
<tbody>
{table_rows}
</tbody>
</table>
</div>
</td></tr>

<!-- SUBMIT -->
<tr><td style="padding:12px 32px 32px 32px;text-align:center;">
<button type="button" onclick="submitForm()" style="background:#008080;color:#ffffff;border:none;padding:14px 40px;font-size:15px;font-weight:700;border-radius:8px;cursor:pointer;letter-spacing:0.3px;font-family:'Open Sans',Arial,sans-serif;">
Submit Updates
</button>
<div id="statusMsg" style="margin-top:14px;font-size:13px;color:rgba(16,30,62,0.65);"></div>
</td></tr>

<!-- FOOTER -->
<tr><td style="background:#101E3E;padding:18px;text-align:center;">
<p style="color:rgba(255,255,255,0.7);font-size:11px;margin:0;">JIT4Labs &mdash; Your Backend Supply Chain, Simplified.</p>
</td></tr>

</table>
</td></tr></table>
</form>

<script>
function submitForm() {{
    var form = document.getElementById('vendorForm');
    var formData = new FormData(form);
    var vendor = formData.get('vendor_name');
    var itemCount = parseInt(formData.get('item_count'));

    var updatedItems = [];

    for (var i = 0; i < itemCount; i++) {{
        var po = formData.get('item_' + i + '_po');
        var poId = formData.get('item_' + i + '_po_id');
        var customer = formData.get('item_' + i + '_customer');
        var product = formData.get('item_' + i + '_product');
        var productId = formData.get('item_' + i + '_product_id');
        var openQty = formData.get('item_' + i + '_open_qty');
        var unitPrice = formData.get('item_' + i + '_unit_price');
        var itemId = formData.get('item_' + i + '_id');
        var eta = formData.get('eta_' + itemId) || '';
        var note = formData.get('note_' + itemId) || '';

        if (eta || note) {{
            updatedItems.push({{
                "po_num": po,
                "po_id": poId,
                "customer": customer,
                "product": product,
                "product_id": productId,
                "open_qty": openQty,
                "unit_price": unitPrice,
                "eta": eta,
                "note": note
            }});
        }}
    }}

    if (updatedItems.length === 0) {{
        document.getElementById('statusMsg').innerHTML =
            '<span style="color:#c0392b;">Please fill in at least one expected date or note before submitting.</span>';
        return;
    }}

    document.getElementById('statusMsg').innerHTML = '<span style="color:#008080;">Submitting updates...</span>';
    var btn = document.querySelector('button[onclick]');
    btn.disabled = true;
    btn.style.background = '#999';

    // Submit via GitHub API — creates a JSON file in the repo.
    // A GitHub Action automatically processes it (updates Vtiger + sends email notification).
    var GH_TOKEN = '{github_token}';
    var GH_REPO = '{github_repo}';
    var timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    var safeVendor = vendor.replace(/[^a-zA-Z0-9]/g, '_');
    var filename = 'submissions/' + safeVendor + '_' + timestamp + '.json';

    var submission = {{
        "vendor_name": vendor,
        "submitted_at": new Date().toISOString(),
        "items": updatedItems
    }};

    var contentB64 = btoa(unescape(encodeURIComponent(JSON.stringify(submission, null, 2))));

    fetch('https://api.github.com/repos/' + GH_REPO + '/contents/' + filename, {{
        method: 'PUT',
        headers: {{
            'Authorization': 'token ' + GH_TOKEN,
            'Accept': 'application/vnd.github.v3+json',
            'Content-Type': 'application/json'
        }},
        body: JSON.stringify({{
            message: 'Vendor ETA update from ' + vendor,
            content: contentB64
        }})
    }}).then(function(r) {{
        if (r.ok || r.status === 201) {{
            document.getElementById('statusMsg').innerHTML =
                '<span style="color:#1e7e34;font-weight:600;">&#10003; Updates submitted successfully! ' +
                updatedItems.length + ' item(s) sent. ETAs will be updated shortly. Thank you.</span>';
        }} else {{
            return r.json().then(function(err) {{
                document.getElementById('statusMsg').innerHTML =
                    '<span style="color:#c0392b;font-weight:600;">Error submitting updates. Please email customersupport@jit4you.com with your updates.</span>';
                console.error('GitHub push error:', err);
            }});
        }}
    }}).catch(function(e) {{
        document.getElementById('statusMsg').innerHTML =
            '<span style="color:#c0392b;font-weight:600;">Error submitting updates. Please email customersupport@jit4you.com with your updates.</span>';
        console.error('Submit error:', e);
    }});
}}
</script>
</body>
</html>"""


# ─────────────────────────────────────────────
# PUSH TO GITHUB & SEND EMAIL
# ─────────────────────────────────────────────
def push_to_github(filename, content):
    """Push an HTML file to the GitHub repo via the Contents API. Returns the Pages URL."""
    token = CONFIG.get("github_token", "")
    repo = CONFIG.get("github_repo", "")
    if not token or not repo:
        log("  GitHub token or repo not configured — skipping push")
        return None

    log(f"  GitHub push: repo={repo}, file={filename}, token={token[:12]}..., content_len={len(content)}")
    api_url = f"https://api.github.com/repos/{repo}/contents/{filename}"
    content_b64 = base64.b64encode(content.encode("utf-8")).decode("utf-8")

    # Check if file already exists (to get its sha for update)
    sha = None
    try:
        existing = http_request(api_url, headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        })
        sha = existing.get("sha")
        if sha:
            log(f"  Found existing file on GitHub (sha: {sha[:8]}...)")
    except Exception as e:
        log(f"  File not found on GitHub (expected for new files): {e}")

    payload = {
        "message": f"Update {filename}",
        "content": content_b64,
    }
    if sha:
        payload["sha"] = sha

    try:
        result = http_request(api_url, method="PUT", headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }, json_body=payload)
        pages_url = f"{CONFIG['github_pages_base']}/{filename}"
        log(f"  SUCCESS — Pushed to GitHub: {pages_url}")
        return pages_url
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8") if e.fp else ""
        except Exception:
            pass
        log(f"  FAILED to push {filename} to GitHub: HTTP {e.code}")
        log(f"  Error detail: {error_body[:500]}")
        return None
    except Exception as e:
        log(f"  FAILED to push {filename} to GitHub: {type(e).__name__}: {e}")
        return None


def send_vendor_email(vendor_name, vendor_email, email_body, form_html, override_to=None, form_url=None):
    """Send the vendor PO email via Resend API. Form submissions use direct
    Resend + Vtiger API calls from the browser (no webhook needed)."""
    api_key = CONFIG.get("resend_api_key", "")
    from_addr = CONFIG.get("resend_from", "JIT4Labs Purchasing <customersupport@jit4you.com>")
    if not api_key or not api_key.startswith("re_"):
        log(f"  Resend API key not configured — skipping email for {vendor_name}")
        return False

    recipient = override_to or vendor_email
    if not recipient:
        log(f"  No email address for {vendor_name} — skipping")
        return False

    subject = f"JIT4You — Open Purchase Orders Update Request — {datetime.now().strftime('%B %d, %Y')}"
    bcc = CONFIG.get("bcc_email", "")
    payload = {
        "from": from_addr,
        "to": [recipient],
        "subject": subject,
        "html": email_body,
    }
    if bcc and bcc.lower() != recipient.lower():
        payload["bcc"] = [bcc]

    data = json.dumps(payload).encode("utf-8")
    try:
        req = urllib.request.Request("https://api.resend.com/emails", data=data, method="POST")
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Content-Type", "application/json")
        # Cloudflare in front of Resend blocks the default Python urllib UA.
        req.add_header("User-Agent", "JIT4Labs vendor-po-report/1.0")
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            log(f"  Email sent via Resend to {vendor_name} ({recipient}) — id={result.get('id', 'unknown')}")
            return True
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode()[:300]
        except Exception:
            body = ""
        log(f"  Resend error for {vendor_name}: HTTP {e.code} {body}")
        return False
    except Exception as e:
        log(f"  Failed to send email to {vendor_name}: {e}")
        return False


# ─────────────────────────────────────────────
# PROCESS VENDOR ETA UPDATES
# ─────────────────────────────────────────────
def process_vendor_updates(vt, submission, dry_run=False):
    """
    Process a vendor form submission and update the ETA custom field directly
    on each Purchase Order line item in Vtiger.

    Groups items by po_id so we only retrieve+revise each PO once,
    even if the vendor updates multiple line items on the same PO.
    Only the relevant line items are modified; all others pass through untouched.
    """
    vendor_name = submission.get("vendor_name", "Unknown")
    items = submission.get("items", [])
    eta_field = CONFIG["po_lineitem_eta_field"]

    if not items:
        log("No items to process.")
        return {"updated_lines": 0, "updated_pos": 0, "errors": 0}

    log(f"Processing {len(items)} ETA updates from {vendor_name}...")
    log(f"Target line-item ETA field: {eta_field}")

    # Resolve any items that have po_num/product but are missing Vtiger IDs.
    # This supports manual CLI use where only human-readable identifiers are known.
    for item in items:
        po_id = item.get("po_id", "")
        product_id = item.get("product_id", "")
        po_num = (item.get("po_num", "") or item.get("po_number", "")).strip()
        product_ref = (item.get("product", "") or item.get("item_number", "")).strip()

        if not po_id and po_num:
            results = vt.query(f"SELECT id FROM PurchaseOrder WHERE purchaseorder_no = '{po_num}';")
            if results:
                item["po_id"] = results[0]["id"]
                log(f"  Resolved {po_num} → {item['po_id']}")
            else:
                log(f"  WARNING: Could not find PO {po_num} in Vtiger")

        if not product_id and product_ref:
            # Try matching by product code first, then by name
            results = vt.query(f"SELECT id FROM Products WHERE productcode = '{product_ref}';")
            if not results:
                results = vt.query(f"SELECT id FROM Products WHERE productname = '{product_ref}';")
            if results:
                item["product_id"] = results[0]["id"]
                log(f"  Resolved product {product_ref} → {item['product_id']}")
            else:
                log(f"  WARNING: Could not find product {product_ref} in Vtiger")

    # Group items by PO so we batch line-item updates per PO
    items_by_po = {}
    for item in items:
        po_id = item.get("po_id", "")
        eta = (item.get("eta", "") or "").strip()
        product_id = item.get("product_id", "")
        po_num = (item.get("po_num", "") or item.get("po_number", "")).strip()
        product_name = item.get("product", "")

        if not eta:
            log(f"  Skipping {po_num} / {product_name} — no ETA provided")
            continue
        if not po_id or not product_id:
            log(f"  Skipping {po_num} / {product_name} — missing PO ID or product ID")
            continue

        items_by_po.setdefault(po_id, []).append(item)

    updated_lines = 0
    updated_pos = 0
    errors = 0

    for po_id, po_items in items_by_po.items():
        po_num = po_items[0].get("po_num", "")
        log(f"\n  PO {po_num} ({po_id}) — {len(po_items)} line item(s) to update")

        try:
            # Retrieve the full PO record (we need the existing LineItems array)
            detail = vt.retrieve(po_id)
            line_items = detail.get("LineItems", detail.get("lineItems", []))

            if not isinstance(line_items, list) or not line_items:
                log(f"    ERROR: No line items found on PO {po_num}")
                errors += 1
                continue

            # Build a quick lookup of vendor updates by product_id
            updates_by_pid = {it.get("product_id"): it for it in po_items}
            applied = 0

            # IMPORTANT: Only touch line items that the vendor actually updated.
            # Unchanged line items are passed through exactly as retrieved.
            for li in line_items:
                pid = li.get("productid", "")
                if pid in updates_by_pid:
                    upd = updates_by_pid[pid]
                    new_eta = upd.get("eta", "")
                    note = (upd.get("note", "") or "").strip()
                    li[eta_field] = new_eta
                    log(f"    ✓ UPDATED line item {pid} — {eta_field}={new_eta}")
                    applied += 1
                else:
                    log(f"    – Skipped line item {pid} (not in vendor submission)")

            if applied == 0:
                log(f"    WARNING: None of the submitted product_ids matched line items on PO {po_num}")
                errors += 1
                continue

            # Send the full LineItems array back (required by Vtiger revise).
            # Only the matched items above have modified fields; all others are
            # identical to what we retrieved, so Vtiger treats them as no-ops.
            revise_payload = {
                "id": po_id,
                "LineItems": line_items,
            }

            if not dry_run:
                vt.update(revise_payload)
                log(f"    PO {po_num} updated — {applied}/{len(line_items)} line item(s) changed")
            else:
                log(f"    [DRY RUN] Would revise PO {po_num} — {applied}/{len(line_items)} line item(s) changed")

            updated_lines += applied
            updated_pos += 1

        except Exception as e:
            log(f"    ERROR processing PO {po_num}: {e}")
            errors += 1

        time.sleep(CONFIG["delay_between_calls"])

    log(f"\nDone! POs updated: {updated_pos}, line items updated: {updated_lines}, errors: {errors}")

    # Send notification email to customersupport@jit4you.com
    if not dry_run and updated_lines > 0:
        try:
            submitted_at = submission.get("submitted_at", datetime.now().isoformat())
            email_html = '<html><body style="font-family:Arial,sans-serif;">'
            email_html += f'<h2 style="color:#1F4E79;">Vendor ETA Update: {vendor_name}</h2>'
            email_html += f'<p><strong>Submitted:</strong> {submitted_at}</p>'
            email_html += f'<p><strong>Summary:</strong> {updated_lines} line item(s) updated across {updated_pos} PO(s)</p>'
            email_html += '<table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;font-size:13px;">'
            email_html += '<tr style="background:#0D2B45;color:#fff;"><th>PO #</th><th>Product</th><th>Expected Date</th><th>Notes</th></tr>'
            for idx, item in enumerate(items):
                eta = (item.get("eta", "") or "").strip()
                note = (item.get("note", "") or "").strip()
                if eta or note:
                    bg = '#f8f9fa' if idx % 2 == 0 else '#ffffff'
                    po_num = item.get("po_num", "")
                    product = item.get("product", "")
                    email_html += f'<tr style="background:{bg};">'
                    email_html += f'<td style="font-weight:600;">{po_num}</td>'
                    email_html += f'<td>{product}</td>'
                    email_html += f'<td style="font-weight:600;color:#1F4E79;">{eta or "-"}</td>'
                    email_html += f'<td>{note or "-"}</td>'
                    email_html += '</tr>'
            email_html += '</table></body></html>'

            subject = f"Vendor ETA Update from {vendor_name} — {datetime.now().strftime('%B %d, %Y')}"
            api_key = CONFIG.get("resend_api_key", "")
            from_addr = CONFIG.get("resend_from", "JIT4Labs Purchasing <customersupport@jit4you.com>")
            if api_key:
                http_request("https://api.resend.com/emails", method="POST", headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                }, json_body={
                    "from": from_addr,
                    "to": ["customersupport@jit4you.com"],
                    "subject": subject,
                    "html": email_html,
                })
                log(f"  Notification email sent to customersupport@jit4you.com")
        except Exception as e:
            log(f"  WARNING: Failed to send notification email: {e}")

    return {"updated_lines": updated_lines, "updated_pos": updated_pos, "errors": errors}


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="JIT4You Vendor Open PO Report")
    parser.add_argument("--no-email", action="store_true", help="Generate HTML files only")
    parser.add_argument("--dry-run", action="store_true", help="Preview counts only")
    parser.add_argument("--vendor", type=str, default=None, help="Filter to specific vendor name")
    parser.add_argument("--test-to", type=str, default=None, help="Override recipient email (for testing)")
    parser.add_argument("--process-updates", action="store_true", help="Process vendor ETA submissions (instead of generating reports)")
    parser.add_argument("--json", type=str, default=None, help="JSON string with vendor submission data (use with --process-updates)")
    parser.add_argument("--file", type=str, default=None, help="Path to JSON file with vendor submission (use with --process-updates)")
    args = parser.parse_args()

    vt = VtigerAPI(CONFIG["vtiger_rest_base"], CONFIG["vtiger_user"], CONFIG["vtiger_accesskey"])

    # ── MODE: Process vendor ETA updates ──
    if args.process_updates:
        log("=" * 60)
        log("JIT4You — Process Vendor ETA Updates")
        log("=" * 60)
        if not args.json and not args.file:
            parser.error("--process-updates requires --json or --file")
        if args.file:
            with open(args.file) as f:
                submission = json.load(f)
        else:
            submission = json.loads(args.json)
        vt.login()
        process_vendor_updates(vt, submission, dry_run=args.dry_run)
        return

    # ── MODE: Generate & send vendor PO reports ──
    log("=" * 60)
    log("JIT4You Vendor Open PO Report")
    log("=" * 60)

    vt.login()

    vendor_items = extract_open_pos(vt, dry_run=args.dry_run, vendor_filter=args.vendor)

    if args.dry_run:
        log("Dry run complete")
        return

    if not vendor_items:
        log("No open PO items found!")
        return

    log(f"\n{'=' * 60}")
    total = sum(len(items) for items in vendor_items.values())
    log(f"RESULTS: {total} open items across {len(vendor_items)} vendors")
    log(f"{'=' * 60}\n")

    output_dir = CONFIG["output_dir"]
    sent_count = 0

    exclude = [v.lower() for v in CONFIG.get("exclude_vendors", [])]

    for vendor_name, items in sorted(vendor_items.items()):
        if vendor_name.lower() in exclude:
            log(f"Skipping {vendor_name} (excluded)")
            continue
        vendor_email = items[0]["vendor_email"] if items else ""
        log(f"Generating report for {vendor_name} ({len(items)} items, email: {vendor_email or 'N/A'})...")

        # Generate interactive HTML form
        form_html = generate_vendor_form(vendor_name, items)

        # Save form HTML file locally
        safe_name = vendor_name.replace(" ", "_").replace("/", "_").replace(",", "")
        gh_filename = f"{safe_name}.html"
        form_path = os.path.join(output_dir, f"JIT4You_Open_POs_{safe_name}.html")
        with open(form_path, "w") as f:
            f.write(form_html)
        log(f"  Form saved: {form_path}")

        # Push form to GitHub Pages
        form_url = push_to_github(gh_filename, form_html)
        if not form_url:
            # Fallback URL if push failed
            form_url = f"{CONFIG['github_pages_base']}/{gh_filename}"
            log(f"  Using fallback URL: {form_url}")

        # Generate email body with link to online form
        vendor_contact = items[0].get("vendor_contact_name", "") if items else ""
        email_body = generate_email_body(vendor_name, items, form_url=form_url, contact_name=vendor_contact)

        # Send email with link
        if not args.no_email:
            if send_vendor_email(vendor_name, vendor_email, email_body, form_html, override_to=args.test_to, form_url=form_url):
                sent_count += 1
        else:
            log("  Skipping email (--no-email flag)")

    log(f"\nDone! Sent {sent_count}/{len(vendor_items)} vendor emails.")


if __name__ == "__main__":
    main()
