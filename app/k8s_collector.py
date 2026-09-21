from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from kubernetes import client

DEFAULT_EXCLUDED_NAMESPACES = {
    "kube-system",
    "kube-public",
    "kube-node-lease",
    "gatekeeper-system",
    "calico-system",
    "tigera-operator",
    "ai-policy-system",
}
EXCLUDED_NAMESPACES = {
    namespace.strip()
    for namespace in os.getenv(
        "AI_ANALYZER_EXCLUDED_NAMESPACES",
        ",".join(DEFAULT_EXCLUDED_NAMESPACES),
    ).split(",")
    if namespace.strip()
}
_REPLICASET_HASH_SUFFIX = re.compile(r"-[a-f0-9]{8,10}$")


@dataclass
class WorkloadEvidence:
    namespace: str
    workload: str
    kind: str
    images: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    env_var_names: list[str] = field(default_factory=list)
    ports: list[int] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    gpu_requested: int = 0
    replica_count: int = 0

    def to_agent_payload(self) -> dict[str, Any]:
        """Facts only; never includes env var values or secret contents."""
        return {
            "namespace": self.namespace,
            "workload": self.workload,
            "kind": self.kind,
            "images": self.images,
            "labels": self.labels,
            "env_var_names": self.env_var_names,
            "ports": self.ports,
            "command": self.command,
            "args": self.args,
            "gpu_requested": self.gpu_requested,
            "replica_count": self.replica_count,
        }


def _owner_deployment_map(apps_api: client.AppsV1Api) -> dict[str, str]:
    """Maps ReplicaSet name -> owning Deployment name."""
    mapping: dict[str, str] = {}
    for replica_set in apps_api.list_replica_set_for_all_namespaces().items:
        owners = replica_set.metadata.owner_references or []
        deployment_owner = next((o for o in owners if o.kind == "Deployment"), None)
        name = deployment_owner.name if deployment_owner else _strip_hash_suffix(
            replica_set.metadata.name
        )
        mapping[replica_set.metadata.name] = name
    return mapping


def _strip_hash_suffix(name: str) -> str:
    return _REPLICASET_HASH_SUFFIX.sub("", name)


def _resolve_owner(
    pod: Any,
    replicaset_to_deployment: dict[str, str],
) -> tuple[str, str]:
    owners = pod.metadata.owner_references or []
    if not owners:
        return pod.metadata.name, "Pod"

    owner = owners[0]
    if owner.kind == "ReplicaSet":
        deployment_name = replicaset_to_deployment.get(
            owner.name, _strip_hash_suffix(owner.name)
        )
        return deployment_name, "Deployment"
    return owner.name, owner.kind


def _gpu_count(pod: Any) -> int:
    total = 0
    for container in pod.spec.containers or []:
        resources = container.resources
        if resources is None:
            continue
        for bucket in (resources.requests, resources.limits):
            if not bucket:
                continue
            for key, value in bucket.items():
                if "gpu" in key.lower():
                    try:
                        total += int(value)
                    except (TypeError, ValueError):
                        pass
    return total


def collect_workload_evidence() -> list[WorkloadEvidence]:
    """Reads only Kubernetes API objects; never inspects secret values."""
    core_api = client.CoreV1Api()
    apps_api = client.AppsV1Api()
    replicaset_to_deployment = _owner_deployment_map(apps_api)

    grouped: dict[tuple[str, str, str], WorkloadEvidence] = {}
    for pod in core_api.list_pod_for_all_namespaces().items:
        namespace = pod.metadata.namespace
        if namespace in EXCLUDED_NAMESPACES:
            continue

        workload_name, kind = _resolve_owner(pod, replicaset_to_deployment)
        key = (namespace, workload_name, kind)
        evidence = grouped.get(key)
        if evidence is None:
            evidence = WorkloadEvidence(namespace=namespace, workload=workload_name, kind=kind)
            grouped[key] = evidence

        evidence.replica_count += 1
        evidence.labels.update(pod.metadata.labels or {})
        evidence.gpu_requested = max(evidence.gpu_requested, _gpu_count(pod))

        for container in pod.spec.containers or []:
            if container.image not in evidence.images:
                evidence.images.append(container.image)
            for env in container.env or []:
                if env.name not in evidence.env_var_names:
                    evidence.env_var_names.append(env.name)
            for port in container.ports or []:
                if port.container_port not in evidence.ports:
                    evidence.ports.append(port.container_port)
            if container.command:
                evidence.command = list(container.command)
            if container.args:
                evidence.args = list(container.args)

    return list(grouped.values())
