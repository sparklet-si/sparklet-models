#!/usr/bin/env python3
"""Validate a published model catalog. Standard library only; never downloads weights."""
import argparse
import hashlib
import json
import math
import re
import subprocess
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FEED = "catalog/v2"
OBJECT_LIMIT = 256 * 1024
INDEX_LIMIT = 256 * 1024
TOTAL_LIMIT = 64 * 1024 * 1024
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
ID = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*\Z")
SCHEMA_KEYWORDS = {"$schema", "title", "description", "type", "properties", "required", "additionalProperties", "items",
                   "minItems", "maxItems", "minLength", "maxLength", "pattern", "enum", "const", "minimum", "maximum"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def strict_json(data, where):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, f"{where}: duplicate key {key}")
            result[key] = value
        return result
    return json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                      parse_constant=lambda value: require(False, f"{where}: invalid number {value}"))


def shape(value, schema, where):
    """The deliberately small JSON Schema subset shared with the website."""
    require(not set(schema) - SCHEMA_KEYWORDS, f"{where}: unsupported schema keyword")
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    checks = {"object": isinstance(value, dict), "array": isinstance(value, list), "string": isinstance(value, str),
              "number": type(value) in (int, float) and math.isfinite(value), "integer": type(value) is int,
              "boolean": type(value) is bool, "null": value is None}
    require(not types or any(checks.get(kind, False) for kind in types), f"{where}: expected {types}")
    if "enum" in schema:
        require(value in schema["enum"], f"{where}: invalid value")
    if "const" in schema:
        require(value == schema["const"], f"{where}: wrong constant")
    if isinstance(value, dict):
        require(set(schema.get("required", [])) <= value.keys(), f"{where}: missing required fields")
        props = schema.get("properties", {})
        require(schema.get("additionalProperties", True) or not value.keys() - props.keys(), f"{where}: unknown fields")
        for key, item in value.items():
            if key in props:
                shape(item, props[key], f"{where}.{key}")
    if isinstance(value, list):
        require(schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 10000), f"{where}: array size")
        for number, item in enumerate(value):
            shape(item, schema.get("items", {}), f"{where}[{number}]")
    if isinstance(value, str):
        require(schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", OBJECT_LIMIT), f"{where}: text length")
        require("pattern" not in schema or re.search(schema["pattern"], value), f"{where}: invalid text")
    if type(value) in (int, float):
        require(schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf), f"{where}: number out of range")


def check_model(model, reports):
    """Rules a schema cannot express. The two tiers are defined here for the public record."""
    where = f"model {model['id']}"
    require(model.get("tier") in ("compatible", "recommended"), f"{where}: published models must be compatible or recommended")
    require(HEX40.fullmatch(model["source"]["revision"]), f"{where}: revision must be a complete commit")
    tested = [item for item in model["hardware"] if item["status"] == "tested"]
    require(tested, f"{where}: compatible means tested; no hardware has a passing report")
    for item in model["hardware"]:
        selected = [reports.get(report_id) for report_id in item["reportIds"]]
        require(all(report and report["modelId"] == model["id"] and report["hardwareId"] == item["id"] and
                    report["revision"] == model["source"]["revision"] for report in selected), f"{where}: bad report reference on {item['id']}")
        outcomes = {sample["outcome"] for report in selected for sample in report["samples"]}
        require(item["status"] != "tested" or "pass" in outcomes, f"{where}: tested needs a passing report on {item['id']}")
        require(item["status"] != "failed" or outcomes & {"oom", "error"}, f"{where}: failed needs a failing report on {item['id']}")
    hardware = {item["id"] for item in model["hardware"]}
    rows = 0
    for profile in model["profiles"]:
        require(profile["hardwareId"] in hardware, f"{where}: profile {profile['id']} points at unknown hardware")
        context = profile["context"]
        require(context["default"] <= context["max"] and all(tokens <= context["max"] for tokens in context["presets"]),
                f"{where}: profile {profile['id']} context exceeds its maximum")
        for row in profile["memory"]:
            rows += row["gpuGiB"] is not None or row["systemGiB"] is not None
            if row["basis"] == "measured":
                require(row.get("reportId") in reports and reports[row["reportId"]]["modelId"] == model["id"],
                        f"{where}: a measured memory row must name its report")
    require(rows, f"{where}: needs at least one memory row")
    for links in model["downloads"].values():
        for link in links:
            require(link["url"].startswith("https://"), f"{where}: downloads must use HTTPS")
            require(link["revision"] == model["source"]["revision"], f"{where}: download revision differs from the model")
            if link["verification"] == "sha256-manifest":
                require(HEX64.fullmatch(link["manifestSha256"] or ""), f"{where}: a mirror needs a file manifest hash")
    curation = model.get("curation")
    if curation:
        require(sum(item["bytes"] for item in curation["files"]) == model["requirements"]["diskBytes"], f"{where}: file sizes differ from diskBytes")


def check_report(report):
    where = f"report {report['id']}"
    require(date.fromisoformat(report["testedAt"]).isoformat() == report["testedAt"], f"{where}: testedAt must be YYYY-MM-DD")
    env = report["environment"]
    if env["memoryArchitecture"] == "windows-uma":
        require(all(env[key] is not None for key in ("installedRamGiB", "systemVisibleRamGiB", "gpuDedicatedGiB", "sharedLimitGiB", "commitLimitGiB")),
                f"{where}: Windows UMA needs its separate capacities")
        require(env["sharedLimitGiB"] <= env["systemVisibleRamGiB"] <= env["installedRamGiB"], f"{where}: shared memory is part of visible system memory")
    for sample in report["samples"]:
        require(sample["promptTokens"] + (sample["outputTokens"] or 0) <= sample["contextTokens"], f"{where}: sample exceeds its context")
        require(sample["outcome"] == "pass" or sample["decodeTps"] is None, f"{where}: a failed sample cannot report throughput")


def read_object(root, ref, where):
    """Every object is named by its content; a mismatch is never accepted."""
    require(HEX64.fullmatch(ref["sha256"]) and type(ref["bytes"]) is int and 0 < ref["bytes"] <= OBJECT_LIMIT, f"{where}: bad reference")
    path = root / FEED / "objects" / f"{ref['sha256']}.json"
    require(path.is_file() and not path.is_symlink(), f"{where}: missing object")
    data = path.read_bytes()
    require(len(data) == ref["bytes"] and hashlib.sha256(data).hexdigest() == ref["sha256"], f"{where}: object does not match its reference")
    return data


def validate(root):
    """Check the current feed and return its index. Raises ValueError on the first problem."""
    root = Path(root)
    path = root / FEED / "index.json"
    require(path.is_file(), "catalog/v2/index.json is missing")
    data = path.read_bytes()
    require(len(data) <= INDEX_LIMIT, "index is too large")
    index = strict_json(data, "index")
    require(index.get("schemaVersion") == 2 and index.get("kind") == "index", "not a version 2 index")
    require(type(index.get("sequence")) is int and 0 < index["sequence"] < 2 ** 53, "invalid sequence")
    require(date.fromisoformat(index["updatedAt"]).isoformat() == index["updatedAt"], "updatedAt must be YYYY-MM-DD")
    require(isinstance(index.get("models"), list) and index["models"], "the catalog must list at least one model")
    require(isinstance(index.get("reports"), list), "reports must be a list")
    require(sum(ref["bytes"] for ref in index["models"] + index["reports"]) <= TOTAL_LIMIT, "catalog is too large")
    schemas = {kind: strict_json((root / f"schemas/{kind}.schema.json").read_bytes(), kind) for kind in ("model", "report")}
    reports = {}
    for ref in index["reports"]:
        raw = read_object(root, ref, f"report {ref['id']}")
        report = strict_json(raw, ref["id"])
        shape(report, schemas["report"], ref["id"])
        require(report["id"] == ref["id"] and report["modelId"] == ref["modelId"] and report["id"] not in reports, f"report {ref['id']}: reference mismatch")
        check_report(report)
        require((root / f"catalog/reports/{report['modelId']}/{report['id']}.json").read_bytes() == raw, f"report {report['id']}: readable copy differs from the published object")
        reports[report["id"]] = report
    seen = set()
    for ref in index["models"]:
        raw = read_object(root, ref, f"model {ref['id']}")
        model = strict_json(raw, ref["id"])
        shape(model, schemas["model"], ref["id"])
        require(ID.fullmatch(model["id"]) and model["id"] == ref["id"] and model["id"] not in seen, f"model {ref['id']}: reference mismatch")
        require(model["tier"] == ref["tier"], f"model {ref['id']}: tier differs from the index")
        check_model(model, reports)
        require((root / f"catalog/models/{model['id']}.json").read_bytes() == raw, f"model {model['id']}: readable copy differs from the published object")
        seen.add(model["id"])
    listed = {path.stem for path in (root / "catalog/models").glob("*.json")}
    require(listed == seen, f"model files differ from the index: {sorted(listed ^ seen)}")
    return index


def check_history(root, base):
    """A changed catalog needs a larger sequence, and published objects never change."""
    require(HEX40.fullmatch(base), "base must be a complete Git commit")
    old = subprocess.run(["git", "show", f"{base}:{FEED}/index.json"], cwd=root, capture_output=True)
    if old.returncode != 0:
        return
    previous = json.loads(old.stdout)
    current = json.loads((Path(root) / FEED / "index.json").read_bytes())
    require(previous == current or current["sequence"] > previous["sequence"], "a changed catalog needs a larger sequence")
    changes = subprocess.check_output(["git", "diff", "--name-status", base, "--", f"{FEED}/objects"], cwd=root, text=True)
    require(all(line.startswith("A\t") for line in changes.splitlines()), "published objects cannot be edited or removed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--base", help="Git commit to compare the sequence and immutable objects against")
    args = parser.parse_args()
    index = validate(args.root)
    if args.base and args.base != "0" * 40:
        check_history(args.root, args.base)
    tiers = [model["tier"] for model in index["models"]]
    print(f"Catalog {index['sequence']} valid: {tiers.count('recommended')} recommended, {tiers.count('compatible')} compatible, {len(index['reports'])} reports")


if __name__ == "__main__":
    main()
