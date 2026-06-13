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
<img src="https://jit4labs1.github.io/vendor-po-updates/jit4you_inc__logo.jpeg" alt="JIT4Labs" width="140" style="display:block;height:auto;">
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
<img src="https://jit4labs1.github.io/vendor-po-updates/jit4you_inc__logo.jpeg" alt="JIT4Labs" width="140" style="display:block;height:auto;">
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

    var hasUpdates = false;
    var htmlBody = '<html><body style="font-family:Arial,sans-serif;">';
    htmlBody += '<h2 style="color:#1F4E79;">Vendor ETA Update: ' + vendor + '</h2>';
    htmlBody += '<p><strong>Submitted:</strong> ' + new Date().toLocaleString() + '</p>';
    htmlBody += '<table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;font-size:13px;">';
    htmlBody += '<tr style="background:#0D2B45;color:#fff;"><th>PO #</th><th>Customer</th><th>Product</th><th>Open Qty</th><th>Expected Date</th><th>Notes</th></tr>';

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
            hasUpdates = true;
            var bg = (i % 2 === 0) ? '#f8f9fa' : '#ffffff';
            htmlBody += '<tr style="background:' + bg + ';">';
            htmlBody += '<td style="font-weight:600;">' + po + '</td>';
            htmlBody += '<td>' + customer + '</td>';
            htmlBody += '<td>' + product + '</td>';
            htmlBody += '<td style="text-align:center;">' + openQty + '</td>';
            htmlBody += '<td style="font-weight:600;color:#1F4E79;">' + (eta || '-') + '</td>';
            htmlBody += '<td>' + (note || '-') + '</td>';
            htmlBody += '</tr>';

            updatedItems.push({{
                "po_num": po,
                "po_id": poId,
                "product": product,
                "product_id": productId,
                "open_qty": openQty,
                "unit_price": unitPrice,
                "eta": eta,
                "note": note
            }});
        }}
    }}
    htmlBody += '</table></body></html>';

    if (!hasUpdates) {{
        document.getElementById('statusMsg').innerHTML =
            '<span style="color:#c0392b;">Please fill in at least one expected date or note before submitting.</span>';
        return;
    }}

    document.getElementById('statusMsg').innerHTML = '<span style="color:#008080;">Sending updates...</span>';
    var btn = document.querySelector('button[onclick]');
    btn.disabled = true;
    btn.style.background = '#999';

    var RESEND_KEY = 're_qWiD9N4f_BbwZXDFFATjDyjZ9BSXZ4f6r';
    var VT_BASE = 'https://jit4youinc.od2.vtiger.com/restapi/v1/vtiger/default';
    var VT_AUTH = 'Basic ' + btoa('customersupport@jit4you.com:fIPkOulq0BaA5y2s');
    var ETA_FIELD = 'cf_purchaseorder_eta';
    var emailSubject = 'Vendor ETA Update from ' + vendor + ' - ' + new Date().toLocaleDateString();
    var errors = [];
    var vtSuccess = 0;

    // --- 1. Send notification email via Resend ---
    var emailPromise = fetch('https://api.resend.com/emails', {{
        method: 'POST',
        headers: {{ 'Authorization': 'Bearer ' + RESEND_KEY, 'Content-Type': 'application/json' }},
        body: JSON.stringify({{
            from: 'JIT4Labs Purchasing <customersupport@jit4you.com>',
            to: ['customersupport@jit4you.com'],
            subject: emailSubject,
            html: htmlBody
        }})
    }}).then(function(r) {{ if (!r.ok) errors.push('Email send failed'); return r.json(); }})
      .catch(function(e) {{ errors.push('Email error: ' + e.message); }});

    // --- 2. Update ETAs in Vtiger (batch by PO) ---
    var poGroups = {{}};
    updatedItems.forEach(function(it) {{
        if (!it.eta) return;
        if (!poGroups[it.po_id]) poGroups[it.po_id] = [];
        poGroups[it.po_id].push(it);
    }});

    var vtPromises = Object.keys(poGroups).map(function(poId) {{
        // Retrieve the PO to get current line items
        var retrieveUrl = VT_BASE + '/retrieve?id=' + encodeURIComponent(poId);
        return fetch(retrieveUrl, {{ headers: {{ 'Authorization': VT_AUTH }} }})
            .then(function(r) {{ return r.json(); }})
            .then(function(data) {{
                if (!data.success) {{ errors.push('Vtiger retrieve failed for ' + poId); return; }}
                var lineItems = data.result.LineItems || data.result.lineItems || [];
                var updatesByPid = {{}};
                poGroups[poId].forEach(function(it) {{ updatesByPid[it.product_id] = it; }});

                lineItems.forEach(function(li) {{
                    var pid = li.productid || '';
                    if (updatesByPid[pid]) {{
                        li[ETA_FIELD] = updatesByPid[pid].eta;
                    }}
                }});

                var revisePayload = {{ id: poId, LineItems: lineItems }};
                return fetch(VT_BASE + '/revise', {{
                    method: 'POST',
                    headers: {{ 'Authorization': VT_AUTH, 'Content-Type': 'application/x-www-form-urlencoded' }},
                    body: 'element=' + encodeURIComponent(JSON.stringify(revisePayload))
                }}).then(function(r) {{ return r.json(); }})
                  .then(function(res) {{
                      if (res.success) vtSuccess++;
                      else errors.push('Vtiger revise failed for ' + poId);
                  }});
            }})
            .catch(function(e) {{ errors.push('Vtiger error for ' + poId + ': ' + e.message); }});
    }});

    Promise.all([emailPromise].concat(vtPromises)).then(function() {{
        if (errors.length === 0) {{
            document.getElementById('statusMsg').innerHTML =
                '<span style="color:#1e7e34;font-weight:600;">&#10003; Updates submitted successfully! ' +
                vtSuccess + ' PO(s) updated in our system. Thank you.</span>';
        }} else {{
            document.getElementById('statusMsg').innerHTML =
                '<span style="color:#e67e22;font-weight:600;">&#10003; Submitted with ' + errors.length +
                ' warning(s). Please contact customersupport@jit4you.com if needed.</span>';
        }}
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
    except Exception:
        pass  # File doesn't exist yet — that's fine

    payload = {
        "message": f"Update {filename}",
        "content": content_b64,
    }
    if sha:
        payload["sha"] = sha

    try:
        http_request(api_url, method="PUT", headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }, json_body=payload)
        pages_url = f"{CONFIG['github_pages_base']}/{filename}"
        log(f"  Pushed to GitHub: {pages_url}")
        return pages_url
    except Exception as e:
        log(f"  Failed to push {filename} to GitHub: {e}")
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
