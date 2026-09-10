"""Run-specific live inspection, composed from narrow service adapters."""

from __future__ import annotations

import base64
import io
import re
import zipfile
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

import ecr_client
import preflight
import s3_client
from preflight import check, summarize


def _definitions(value: str, main: str) -> dict[str, str]:
    if value.startswith("https://"):
        payload = ecr_client.read_aws_metadata(value)
    else:
        try:
            payload = base64.b64decode(value, validate=True)
        except (ValueError, TypeError):
            payload = value.encode("utf-8")
    if len(payload) > 2_000_000:
        raise ValueError("Definition exceeds inspection limit.")
    if not zipfile.is_zipfile(io.BytesIO(payload)):
        return {main: payload.decode("utf-8")}
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = [info for info in archive.infolist() if info.filename.endswith(".wdl")]
        if len(entries) > 100 or sum(info.file_size for info in entries) > 2_000_000:
            raise ValueError("Workflow archive exceeds inspection limit.")
        names = [_local_name(info.filename) for info in entries]
        if len(set(names)) != len(names):
            raise ValueError("Duplicate workflow archive paths.")
        return {name: archive.read(info).decode("utf-8") for name, info in zip(names, entries)}


def _local_name(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or any(c in value for c in (":", "\\", "%", "?", "#")):
        raise ValueError("Unsafe workflow import path.")
    return str(path)


def _load_wdl(definitions: dict[str, str], main: str):
    import WDL

    async def read_source(uri, path, importer):
        name = _local_name(uri)
        if importer is not None:
            name = _local_name(str(PurePosixPath(importer.pos.abspath).parent / name))
        return WDL.Tree.ReadSourceResult(definitions[name], name)

    return WDL.load(_local_name(main), read_source=read_source, import_max_depth=10)


def workflow_file_inputs(workflow: dict[str, Any], params: dict[str, Any]) -> tuple[set[str], bool]:
    """Resolve supplied/default File leaves using archive-local WDL types only."""
    import json
    import WDL
    files: set[str] = set()
    try:
        main = workflow.get("main") or "main.wdl"
        doc = _load_wdl(_definitions(workflow["definition"], main), main)
        target = doc.workflow or doc.tasks[0]
        stdlib = WDL.StdLib.Base(doc.effective_wdl_version)
        env = WDL.Env.Bindings()
        complete = True
        for binding in target.available_inputs:
            name, decl = binding.name, binding.value
            value = params.get(name, params.get(target.name + "." + name))
            if value is not None:
                if not isinstance(decl.type, (WDL.Type.String, WDL.Type.File)):
                    value = json.loads(value) if isinstance(value, str) else value
                resolved = WDL.Value.from_json(decl.type, value)
            elif decl.expr is not None:
                # Only literals here: no defaults that perform I/O or execute functions.
                if not isinstance(decl.expr, (WDL.Expr.String, WDL.Expr.Int, WDL.Expr.Boolean)):
                    complete = False
                    continue
                if any(isinstance(c, WDL.Expr.Placeholder) for c in decl.expr.children):
                    complete = False
                    continue
                resolved = decl.expr.eval(env, stdlib=stdlib).coerce(decl.type)
            elif decl.type.optional:
                continue
            else:
                complete = False
                continue
            env = env.bind(name, resolved)
            def collect(v):
                if isinstance(v, WDL.Value.File):
                    files.add(v.value)
                for child in v.children:
                    collect(child)
            collect(resolved)
        return files, complete
    except Exception:
        return files, False


def _wdl_images(definitions: dict[str, str], main: str, params: dict[str, Any]) -> tuple[set[str], bool]:
    import WDL
    doc = _load_wdl(definitions, main)
    stdlib = WDL.StdLib.Base(doc.effective_wdl_version)
    images: set[str] = set()
    complete = True
    tasks_seen = 0

    def evaluate(expr, env):
        # Restrict the AST before miniwdl evaluation; no stdlib I/O or task execution.
        def safe(node):
            if isinstance(node, WDL.Expr.Apply) and node.function_name not in {"_add", "_interpolation_add"}:
                raise ValueError("Unsupported runtime expression.")
            for child in node.children:
                safe(child)
        safe(expr)
        return expr.eval(env, stdlib=stdlib)

    def visit(target, supplied, depth=0):
        nonlocal complete, tasks_seen
        if depth > 10:
            complete = False
            return
        env = WDL.Env.Bindings()
        declarations = list(target.inputs or []) + list(getattr(target, "postinputs", []))
        if isinstance(target, WDL.Tree.Workflow):
            declarations += [node for node in target.body if isinstance(node, WDL.Tree.Decl)]
        pending = []
        for decl in declarations:
            if decl.name in supplied:
                try:
                    value = supplied[decl.name]
                    if not isinstance(value, WDL.Value.Base):
                        value = WDL.Value.from_json(decl.type, value)
                    env = env.bind(decl.name, value.coerce(decl.type))
                except Exception:
                    # An invalid override must not fall back to the default.
                    complete = False
            elif decl.expr is not None:
                pending.append(decl)
        for _ in range(len(pending)):
            unresolved = []
            for decl in pending:
                try:
                    env = env.bind(decl.name, evaluate(decl.expr, env).coerce(decl.type))
                except Exception:
                    unresolved.append(decl)
            if len(unresolved) == len(pending):
                break
            pending = unresolved
        if isinstance(target, WDL.Tree.Task):
            tasks_seen += 1
            try:
                expr = target.runtime.get("docker") or target.runtime.get("container")
                value = evaluate(expr, env)
                if not isinstance(value, WDL.Value.String) or not value.value:
                    raise ValueError("Unresolved container.")
                images.add(value.value)
            except Exception:
                complete = False
            return
        for node in target.body:
            if isinstance(node, WDL.Tree.Call):
                bindings = {key[len(node.name) + 1:]: value for key, value in supplied.items()
                            if key.startswith(node.name + ".")}
                for key, expr in node.inputs.items():
                    try:
                        bindings[key] = evaluate(expr, env)
                    except Exception:
                        complete = False
                        bindings[key] = None
                visit(node.callee, bindings, depth + 1)
            elif not isinstance(node, WDL.Tree.Decl):
                # Scatter/conditional call contexts aren't resolved by this subset.
                complete = False

    targets = [doc.workflow] if doc.workflow else doc.tasks
    for target in targets:
        supplied = {}
        for key, value in params.items():
            name = key.removeprefix(target.name + ".")
            if name in supplied and supplied[name] != value:
                return images, False
            supplied[name] = value
        visit(target, supplied)
    return images, complete and tasks_seen > 0


def _map_images(images: set[str], workflow: dict[str, Any]) -> tuple[set[str], bool]:
    mappings = workflow.get("containerRegistryMap") or {}
    if workflow.get("containerRegistryMapUri") and not mappings:
        return images, False
    exact: dict[str, str] = {}
    try:
        for entry in mappings.get("imageMappings", []):
            source, destination = entry["sourceImage"], entry["destinationImage"]
            if source in exact and exact[source] != destination:
                return images, False
            ecr_client.parse_image(destination)
            exact[source] = destination
        rules = []
        context = ecr_client.workflow_arn_parts(workflow.get("arn"))
        for entry in mappings.get("registryMappings", []):
            upstream, prefix = entry["upstreamRegistryUrl"], entry["ecrRepositoryPrefix"]
            # Limit prefix rewriting to explicit registries with no upstream namespace.
            if (not context or entry.get("upstreamRepositoryPrefix") or
                    not re.fullmatch(r"[a-z0-9.-]+\.[a-z]+", upstream)):
                return images, False
            partition, region, account = context
            account = entry.get("ecrAccountId") or account
            suffix = "amazonaws.com.cn" if partition == "aws-cn" else "amazonaws.com"
            destination = f"{account}.dkr.ecr.{region}.{suffix}/{prefix}"
            ecr_client.parse_image(destination + "/probe:latest")
            rules.append((upstream + "/", destination + "/"))
        result = set()
        for image in images:
            if image in exact:
                result.add(exact[image])
                continue
            candidates = {destination + image[len(source):] for source, destination in rules
                          if image.startswith(source)}
            if len(candidates) > 1:
                return images, False
            result.add(next(iter(candidates)) if candidates else image)
        return result, True
    except (KeyError, TypeError, ValueError, AttributeError):
        return images, False


def workflow_images(workflow: dict[str, Any], params: dict[str, Any]) -> tuple[list[str], bool]:
    """Resolve a bounded WDL subset using only exported sources and run inputs."""
    images = set(preflight.collect_container_images(params))
    complete = False
    if workflow.get("engine") == "WDL" and workflow.get("definition"):
        try:
            main = workflow.get("main") or "main.wdl"
            definitions = _definitions(workflow["definition"], main)
            resolved, complete = _wdl_images(definitions, main, params)
            images = resolved if complete else images | resolved
        except Exception:
            complete = False
    images, mapped = _map_images(images, workflow)
    return sorted(images), complete and mapped


def run_live_preflight(args: Any, *, omics: Any, ecr: Any, s3: Any) -> dict[str, Any]:
    local = preflight.run_preflight(args)
    checks = [c for c in local["checks"] if c["name"] not in {"profile", "container_images"}]
    allow_unknown = bool(getattr(args, "allow_unverified_readiness", False))
    if not local["ok"]:
        return summarize(checks)
    if not getattr(args, "start_run", None):
        checks.append(check("run_context", "UNKNOWN", "Live readiness requires --start-run and its run parameters."))
        return summarize(checks)
    params, _ = preflight.load_params_file(args.params)
    try:
        lookup = {"id": str(args.start_run), "type": args.workflow_type}
        workflow = omics.call("GetWorkflow", **lookup)
        source_arn = workflow.get("arn")
        if not ecr_client.workflow_arn_parts(source_arn):
            source_arn = None
        role = re.fullmatch(r"arn:(?:aws|aws-us-gov|aws-cn):iam::(\d{12}):role/.+", args.role_arn or "")
        source_account = role.group(1) if role else None
        if args.workflow_type != workflow.get("type"):
            checks.append(check("workflow_type", "FAIL", "Resolved workflow type differs from the requested type."))
        if args.workflow_version_name:
            version = omics.call("GetWorkflowVersion", workflowId=str(args.start_run),
                                 versionName=args.workflow_version_name)
            workflow = {**version, "type": args.workflow_type}
        checks.append(check("profile", "PASS", f"HealthOmics read succeeded using {args.profile or 'the default credential chain'}.", required=False))
        checks.append(check("workflow_active", "PASS" if workflow.get("status") == "ACTIVE" else "FAIL",
                            f"Workflow {args.start_run}: {workflow.get('status', 'UNKNOWN')}"))
    except Exception as exc:
        code = ecr_client.exception_code(exc)
        checks.append(check("workflow_lookup", "FAIL" if code == "ResourceNotFoundException" else "UNKNOWN",
                            f"{args.workflow_type} workflow {args.start_run}: {code}", code=code))
        return summarize(checks, allow_unknown=allow_unknown)
    template = workflow.get("parameterTemplate") or {}
    supplied = {k: v for k, v in params.items() if not k.startswith("_")}
    missing = [k for k, v in template.items() if not v.get("optional", False) and k not in supplied]
    unknown = sorted(set(supplied) - set(template))
    non_strings = [k for k, v in supplied.items() if not isinstance(v, str)]
    checks.append(check("workflow_parameters", "FAIL" if missing or unknown or non_strings else "PASS",
                        f"Missing required: {missing}; unknown keys: {unknown}; non-string values: {non_strings}."))
    if args.workflow_type == "PRIVATE":
        try:
            if args.workflow_version_name:
                exported = omics.call("GetWorkflowVersion", workflowId=str(args.start_run),
                                      versionName=args.workflow_version_name, export=["DEFINITION"])
            else:
                exported = omics.call("GetWorkflow", **lookup, export=["DEFINITION"])
            workflow.update(exported)
        except Exception as exc:
            checks.append(check("definition_export", "UNKNOWN", f"Definition unavailable ({ecr_client.exception_code(exc)})."))
        images, complete = workflow_images({**workflow, "arn": source_arn}, params)
        checks.append(check("container_inventory", "PASS" if complete else "UNKNOWN",
                            f"{len(images)} container(s) found. " + ("Supported WDL runtime expressions and local imports resolved." if complete else
                            "Incomplete: unresolved expressions, unsafe/missing imports, unsupported engine/mapping, or missing miniwdl/definition.")))
        for uri in images:
            checks.extend(ecr_client.inspect_image(client=ecr, uri=uri, region=args.region,
                                                   source_arn=source_arn, source_account=source_account))
    else:
        checks.append(check("container_inventory", "NOT_APPLICABLE", "Ready2Run containers are managed by AWS."))
    file_uris: set[str] = set()
    if workflow.get("engine") == "WDL" and args.workflow_type == "PRIVATE":
        file_uris, complete = workflow_file_inputs(workflow, params)
        checks.append(check("file_input_inventory", "PASS" if complete else "UNKNOWN",
                            f"{len(file_uris)} typed File input(s); resolution {'complete' if complete else 'incomplete'}."))
    checks.extend(s3_client.inspect_run_paths(client=s3, params=params, output_uri=args.output_uri,
                                            file_uris=file_uris))
    checks.append(check("effective_execution_permissions", "UNKNOWN",
                        "Read-only caller checks cannot prove execution-role, service-principal, SCP, KMS or network access at runtime.", required=False))
    result = summarize(checks, allow_unknown=allow_unknown)
    result["environment"] = {
        "schema_version": 1, "checked_at": datetime.now(timezone.utc).isoformat(),
        "region": args.region, "profile": args.profile, "workflow_id": args.start_run,
        "workflow_type": args.workflow_type, "workflow_version": args.workflow_version_name,
        "workflow_digest": workflow.get("digest"), "role_arn": args.role_arn,
        "output_uri": args.output_uri,
        "images": [{"uri": c["image_uri"], "digest": c["image_digest"]}
                   for c in checks if c["name"] == "image_exists" and c["status"] == "PASS"],
    }
    return result
