#!/usr/bin/env python3
"""Convert the offline tests' OTLP/JSON exports to Weaver v0.28 live-check samples.

Usage: weaver_samples.py /tmp/otlp/*.json > /tmp/samples.json
This adapter covers this exporter's signal shapes; it is not a general OTLP converter.
"""

import json
import sys


def any_value(value):
    """Decode the scalar and nested AnyValue shapes emitted by our Python SDK."""
    for field, convert in (("stringValue", str), ("boolValue", bool), ("intValue", int), ("doubleValue", float)):
        if field in value:
            return convert(value[field])
    if "kvlistValue" in value:
        return {item["key"]: any_value(item["value"]) for item in value["kvlistValue"].get("values", [])}
    if "arrayValue" in value:
        return [any_value(item) for item in value["arrayValue"].get("values", [])]
    if not value:
        return None
    raise ValueError(f"unsupported OTLP AnyValue shape: {list(value)}")


def attributes(items):
    types = {
        "stringValue": ("string", str),
        "boolValue": ("boolean", bool),
        "intValue": ("int", int),
        "doubleValue": ("double", float),
    }
    result = []
    for item in items:
        value = item["value"]
        field = next(iter(value), None)
        sample = {"name": item["key"], "value": any_value(value)}
        if field in types:
            sample["type"] = types[field][0]
        # Weaver's file format has no explicit map type. Preserve the native value,
        # leaving type inference unset for maps and heterogeneous/complex arrays.
        result.append(sample)
    return result


def samples(payload):
    for resource in payload.get("resourceSpans", []):
        yield {"resource": {"attributes": attributes(resource["resource"]["attributes"])}}
        for scope in resource["scopeSpans"]:
            for span in scope["spans"]:
                yield {
                    "span": {
                        "name": span["name"],
                        "kind": {
                            1: "internal",
                            2: "server",
                            3: "client",
                            "SPAN_KIND_INTERNAL": "internal",
                            "SPAN_KIND_SERVER": "server",
                            "SPAN_KIND_CLIENT": "client",
                        }[span["kind"]],
                        "attributes": attributes(span.get("attributes", [])),
                    }
                }
    for resource in payload.get("resourceLogs", []):
        yield {"resource": {"attributes": attributes(resource["resource"]["attributes"])}}
        for scope in resource["scopeLogs"]:
            for record in scope["logRecords"]:
                severity = record["severityNumber"]
                if isinstance(severity, str):
                    severity = {
                        "SEVERITY_NUMBER_INFO": 9,
                        "SEVERITY_NUMBER_WARN": 13,
                        "SEVERITY_NUMBER_ERROR": 17,
                        "SEVERITY_NUMBER_FATAL": 21,
                    }[severity]
                yield {
                    "log": {
                        "event_name": record.get("eventName", ""),
                        "severity_number": severity,
                        "severity_text": record.get("severityText", ""),
                        "body": record.get("body", {}).get("stringValue", ""),
                        "attributes": attributes(record.get("attributes", [])),
                        "trace_id": record.get("traceId"),
                        "span_id": record.get("spanId"),
                    }
                }
    for resource in payload.get("resourceMetrics", []):
        yield {"resource": {"attributes": attributes(resource["resource"]["attributes"])}}
        for scope in resource["scopeMetrics"]:
            for metric in scope["metrics"]:
                kind = next(k for k in ("sum", "gauge", "histogram") if k in metric)
                instrument = kind
                if kind == "sum":
                    instrument = "counter" if metric[kind]["isMonotonic"] else "updowncounter"
                points = []
                for point in metric[kind]["dataPoints"]:
                    output = {"attributes": attributes(point.get("attributes", []))}
                    if kind == "histogram":
                        output.update(
                            count=int(point["count"]),
                            sum=point["sum"],
                            min=point["min"],
                            max=point["max"],
                            bucket_counts=[int(x) for x in point["bucketCounts"]],
                        )
                    else:
                        output["value"] = point["asDouble"] if "asDouble" in point else int(point["asInt"])
                    points.append(output)
                yield {
                    "metric": {
                        "name": metric["name"],
                        "unit": metric["unit"],
                        "instrument": instrument,
                        "data_points": points,
                    }
                }


def main():
    result = []
    for path in sys.argv[1:]:
        with open(path) as source:
            result.extend(samples(json.load(source)))
    if not result:
        sys.exit("no telemetry samples")
    json.dump(result, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
