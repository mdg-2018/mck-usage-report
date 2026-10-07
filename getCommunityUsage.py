#!/usr/bin/env python3
"""Report pod counts and declared container memory for MongoDBCommunity objects."""

import argparse
import json
import re
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


QUANTITY_PATTERN = re.compile(
	r"^(?P<number>[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)"
	r"(?P<suffix>Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|K|M|G|T|P|E)?$"
)
# Kubernetes accepts decimal SI suffixes (such as M) as well as powers of two
# (such as Mi); keep their scales separate so quantities are converted correctly.
DECIMAL_SUFFIXES = {
	"": 0,
	"n": -9,
	"u": -6,
	"m": -3,
	"k": 3,
	"K": 3,
	"M": 6,
	"G": 9,
	"T": 12,
	"P": 15,
	"E": 18,
}
BINARY_SUFFIXES = {
	"Ki": 10,
	"Mi": 20,
	"Gi": 30,
	"Ti": 40,
	"Pi": 50,
	"Ei": 60,
}
MEBIBYTE = Decimal(1024**2)


def parse_quantity_bytes(value: str | None) -> Decimal:
	"""Convert a Kubernetes resource quantity to bytes."""
	if value is None:
		return Decimal(0)

	match = QUANTITY_PATTERN.fullmatch(value)
	if match is None:
		raise ValueError(f"Unsupported Kubernetes memory quantity: {value!r}")

	# Decimal avoids introducing floating-point rounding while summing container resources.
	try:
		number = Decimal(match.group("number"))
	except InvalidOperation as error:
		raise ValueError(f"Invalid Kubernetes memory quantity: {value!r}") from error

	suffix = match.group("suffix") or ""
	if suffix in BINARY_SUFFIXES:
		return number * (Decimal(2) ** BINARY_SUFFIXES[suffix])
	return number * (Decimal(10) ** DECIMAL_SUFFIXES[suffix])


def parse_namespaces(values: list[str]) -> list[str]:
	namespaces = []
	for value in values:
		# Accept both normal argparse values and the README's shell-style [mongodb] example.
		cleaned = value.strip().strip("[]")
		namespaces.extend(part.strip() for part in cleaned.split(",") if part.strip())
	if not namespaces:
		raise ValueError("Provide at least one namespace with --namespaces")
	return list(dict.fromkeys(namespaces))


def get_namespace_resources(
	namespace: str, kubeconfig: Path, context: str | None
) -> list[dict[str, Any]]:
	command = [
		"kubectl",
		"--kubeconfig",
		str(kubeconfig),
	]
	if context:
		command.extend(["--context", context])
	command.extend(
		[
			"get",
			"mongodbcommunity,statefulsets,pods",
			"--namespace",
			namespace,
			"--output",
			"json",
		]
	)
	# Fetch all three resource kinds together so ownership can be followed locally by UID.
	result = subprocess.run(command, check=True, capture_output=True, text=True)
	return json.loads(result.stdout).get("items", [])


def owner_references(resource: dict[str, Any]) -> list[dict[str, Any]]:
	return resource.get("metadata", {}).get("ownerReferences", [])


def aggregate_namespace(
	namespace: str, resources: list[dict[str, Any]]
) -> list[dict[str, Any]]:
	# UIDs identify the actual Kubernetes objects and avoid relying on naming conventions.
	communities = {
		resource["metadata"]["uid"]: resource["metadata"]["name"]
		for resource in resources
		if resource.get("kind") == "MongoDBCommunity"
	}
	usage = {
		name: {
			"namespace": namespace,
			"community": name,
			"pods": 0,
			"requests": Decimal(0),
			"limits": Decimal(0),
		}
		for name in communities.values()
	}

	statefulset_owners = {}
	for resource in resources:
		if resource.get("kind") != "StatefulSet":
			continue
		community_uid = next(
			(
				owner.get("uid")
				for owner in owner_references(resource)
				if owner.get("kind") == "MongoDBCommunity"
				and owner.get("uid") in communities
			),
			None,
		)
		if community_uid is not None:
			# MongoDBCommunity owns StatefulSets; retain the link for each pod's owner lookup.
			statefulset_owners[resource["metadata"]["uid"]] = communities[
				community_uid
			]

	for pod in resources:
		if pod.get("kind") != "Pod":
			continue
		community_name = next(
			(
				statefulset_owners.get(owner.get("uid"))
				for owner in owner_references(pod)
				if owner.get("kind") == "StatefulSet"
				and owner.get("uid") in statefulset_owners
			),
			None,
		)
		if community_name is None:
			continue

		community_usage = usage[community_name]
		community_usage["pods"] += 1
		# Sum declared requests and limits for each regular container in the owned pod.
		for container in pod.get("spec", {}).get("containers", []):
			resources = container.get("resources", {})
			community_usage["requests"] += parse_quantity_bytes(
				resources.get("requests", {}).get("memory")
			)
			community_usage["limits"] += parse_quantity_bytes(
				resources.get("limits", {}).get("memory")
			)

	return [usage[name] for name in sorted(usage)]


def format_memory(byte_count: Decimal) -> str:
	# Display totals in MiB (1024 * 1024 bytes), regardless of input suffix.
	mebibytes = (byte_count / MEBIBYTE).quantize(Decimal("0.01"))
	return f"{mebibytes} MiB"


def print_report(rows: list[dict[str, Any]]) -> None:
	headers = ["Namespace", "MongoDBCommunity", "Pods", "Memory requests", "Memory limits"]
	table = [headers]

	for namespace in dict.fromkeys(row["namespace"] for row in rows):
		namespace_rows = [row for row in rows if row["namespace"] == namespace]
		table.extend(
			[
				row["namespace"],
				row["community"],
				str(row["pods"]),
				format_memory(row["requests"]),
				format_memory(row["limits"]),
			]
			for row in namespace_rows
		)
		table.append(
			[
				namespace,
				"NAMESPACE TOTAL",
				str(sum(row["pods"] for row in namespace_rows)),
				format_memory(sum((row["requests"] for row in namespace_rows), Decimal(0))),
				format_memory(sum((row["limits"] for row in namespace_rows), Decimal(0))),
			]
		)

	if len(rows) > 0:
		# Include an overall total in addition to each namespace's subtotal.
		table.append(
			[
				"ALL NAMESPACES",
				"TOTAL",
				str(sum(row["pods"] for row in rows)),
				format_memory(sum((row["requests"] for row in rows), Decimal(0))),
				format_memory(sum((row["limits"] for row in rows), Decimal(0))),
			]
		)

	widths = [max(len(row[index]) for row in table) for index in range(len(headers))]
	# Pad columns based on their longest value to keep the report readable in a terminal.
	for index, row in enumerate(table):
		print("  ".join(value.ljust(widths[column]) for column, value in enumerate(row)))
		if index == 0:
			print("  ".join("-" * width for width in widths))


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description=(
			"Aggregate pod counts and declared container memory requests and limits "
			"for MongoDBCommunity objects."
		)
	)
	parser.add_argument(
		"--namespaces",
		nargs="+",
		default=["mongodb"],
		help="Namespaces to scan (space- or comma-separated; defaults to mongodb).",
	)
	parser.add_argument("--context", help="Kubernetes context to use.")
	parser.add_argument(
		"--kubeconfig",
		type=Path,
		default=Path(__file__).resolve().with_name("kube.yaml"),
		help="Kubeconfig path (defaults to kube.yaml next to this script).",
	)
	return parser.parse_args()


def main() -> int:
	args = parse_args()
	try:
		namespaces = parse_namespaces(args.namespaces)
		rows = []
		for namespace in namespaces:
			# Process each namespace independently, then print one combined report.
			resources = get_namespace_resources(namespace, args.kubeconfig, args.context)
			rows.extend(aggregate_namespace(namespace, resources))
		print_report(rows)
	except (OSError, subprocess.CalledProcessError, json.JSONDecodeError, ValueError) as error:
		detail = getattr(error, "stderr", None)
		message = detail.strip() if detail else str(error)
		print(f"Error: {message}", file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
