#!/usr/bin/python3
"""Small synthetic MHL portal for the Project 100 assessment scenario.

The IDOR and report-query weaknesses are intentional, scoped to synthetic
records, and documented in docs/project-100/SCENARIO-DESIGN.md.
"""
import json
import os
import re
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

import psycopg2


def shipment_query(customer_id, shipment_id):
    # Legacy handler trusts the upstream tenant check and only filters by ID.
    return "SELECT shipment_id, customer_id, customer_name, destination, status, invoice_total, dispatch_note FROM shipments WHERE shipment_id = %s", (shipment_id,)


def report_query(status):
    # The warehouse report endpoint retained string interpolation from v1.
    return "SELECT shipment_id, customer_name, destination, status, invoice_total FROM shipments WHERE status = '" + status + "'"


def get_connection():
    return psycopg2.connect(
        host=os.environ["MHL_DB_HOST"],
        dbname="mhl_ops",
        user="mhl_runtime",
        password=os.environ["MHL_DB_PASSWORD"],
        connect_timeout=3,
    )


def response(start_response, status, payload):
    body = json.dumps(payload, sort_keys=True, default=str).encode()
    start_response(status, [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
    return [body]


def home(start_response):
    body = b"""<!doctype html>
<html lang=\"en\"><head><meta charset=\"utf-8\"><title>MHL Shipment Desk</title></head>
<body><header><h1>Meridian Harbor Logistics</h1><p>Shipment Desk / Acquired Warehouse Operations</p></header>
<main><h2>Customer shipment lookup</h2><form action=\"/api/shipments/61001\" method=\"get\">
<label>Customer account <input name=\"customer_id\" value=\"4101\"></label>
<button>View shipment</button></form><h2>Warehouse status report</h2>
<form action=\"/api/reports\" method=\"get\"><label>Status <input name=\"status\" value=\"delayed\"></label>
<button>Run report</button></form><p><a href=\"/healthz\">Service health</a></p></main></body></html>"""
    start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body)))])
    return [body]


def application(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    if path == "/":
        return home(start_response)
    if path == "/healthz":
        try:
            with get_connection() as conn, conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            return response(start_response, "200 OK", {"service": "mhl-portal", "database": "ok"})
        except Exception:
            return response(start_response, "503 Service Unavailable", {"service": "mhl-portal", "database": "unavailable"})

    query = environ.get("QUERY_STRING", "")
    params = {key: values[0] for key, values in parse_qs(query, keep_blank_values=True).items()}
    try:
        with get_connection() as conn, conn.cursor() as cursor:
            if path.startswith("/api/shipments/"):
                suffix = path.rsplit("/", 1)[-1]
                if not suffix.isdecimal():
                    return response(start_response, "400 Bad Request", {"error": "shipment id must be numeric"})
                sql, values = shipment_query(params.get("customer_id"), int(suffix))
                cursor.execute(sql, values)
            elif path == "/api/reports":
                status = params.get("status", "delayed")
                if len(status) > 120 or not re.fullmatch(r"[A-Za-z0-9_ '\-;=()]+", status):
                    return response(start_response, "400 Bad Request", {"error": "unsupported report filter"})
                cursor.execute(report_query(status))
            else:
                return response(start_response, "404 Not Found", {"error": "not found"})
            rows = cursor.fetchall()
            columns = [item[0] for item in cursor.description]
            return response(start_response, "200 OK", [dict(zip(columns, row)) for row in rows])
    except Exception:
        return response(start_response, "503 Service Unavailable", {"error": "operations data unavailable"})


if __name__ == "__main__":
    make_server("0.0.0.0", 8080, application).serve_forever()
