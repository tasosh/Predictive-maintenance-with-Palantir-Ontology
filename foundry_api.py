"""
foundry_api.py — response envelopes matching the Foundry v2 REST API.

This exists so the PoC can be pointed at a real stack by swapping one module.
Shapes follow palantir.com/docs/foundry/api/v2/ontologies-v2-resources/*, e.g.

    GET /api/v2/ontologies/{ontology}
    -> { "apiName", "displayName", "description", "rid" }

Endpoints modelled here: getOntology, listObjectTypes, getObjectType,
listOutgoingLinkTypes, listObjects, getObject, searchObjects, listLinkedObjects,
listActionTypes and applyAction.
"""

from __future__ import annotations

from typing import Any

from ontology import (LINK_TYPES, OBJECT_TYPES, ONTOLOGY_API_NAME, ONTOLOGY_DESCRIPTION,
                      ONTOLOGY_DISPLAY_NAME, ONTOLOGY_RID, OntologyStore, outgoing_links,
                      rid_for)

API_BASE = "/api/v2/ontologies"


def get_ontology() -> dict:
    """GET /api/v2/ontologies/{ontology} — scope api:ontologies-read."""
    return {
        "apiName": ONTOLOGY_API_NAME,
        "displayName": ONTOLOGY_DISPLAY_NAME,
        "description": ONTOLOGY_DESCRIPTION,
        "rid": ONTOLOGY_RID,
    }


def _object_type_v2(api_name: str, meta: dict) -> dict:
    return {
        "apiName": api_name,
        "displayName": meta["displayName"],
        "status": "ACTIVE",
        "description": meta["description"],
        "pluralDisplayName": meta["pluralDisplayName"],
        "icon": {"type": "blueprint", "name": meta["icon"], "color": "#4C90F0"},
        "primaryKey": meta["primaryKey"],
        "titleProperty": meta["titleProperty"],
        "rid": f"ri.ontology.main.object-type.{api_name}",
        "visibility": "NORMAL",
        "properties": {
            name: {"description": p.get("description", ""),
                   "dataType": p["dataType"],
                   "rid": f"ri.ontology.main.property.{api_name}.{name}"}
            for name, p in meta["properties"].items()
        },
        "x-sourceSystem": meta["sourceSystem"],
    }


def list_object_types(page_size: int | None = None) -> dict:
    """GET /api/v2/ontologies/{ontology}/objectTypes"""
    return {"data": [_object_type_v2(k, v) for k, v in OBJECT_TYPES.items()],
            "nextPageToken": None}


def get_object_type(api_name: str) -> dict:
    """GET /api/v2/ontologies/{ontology}/objectTypes/{objectType}"""
    if api_name not in OBJECT_TYPES:
        return _error("ObjectTypeNotFound", {"objectType": api_name}, 404)
    return _object_type_v2(api_name, OBJECT_TYPES[api_name])


def list_outgoing_link_types(api_name: str) -> dict:
    """GET /api/v2/ontologies/{ontology}/objectTypes/{objectType}/outgoingLinkTypes"""
    data = []
    for link, (src, tgt, card, fk, inverse) in outgoing_links(api_name).items():
        data.append({
            "apiName": link,
            "displayName": link,
            "status": "ACTIVE",
            "objectTypeApiName": tgt,
            "cardinality": card,
            "foreignKeyPropertyApiName": fk,
            "linkTypeRid": f"ri.ontology.main.link-type.{api_name}.{link}",
        })
    return {"data": data, "nextPageToken": None}


def _to_object_v2(obj: dict) -> dict:
    out = {k: v for k, v in obj.items() if not k.startswith("__")}
    out["__apiName"] = obj["__apiName"]
    out["__primaryKey"] = obj["__primaryKey"]
    out["__rid"] = obj["__rid"]
    return out


def list_objects(store: OntologyStore, object_type: str, page_size: int = 50,
                 branch: str = "master") -> dict:
    """GET /api/v2/ontologies/{ontology}/objects/{objectType}"""
    rows = store.all(object_type, branch)[:page_size]
    return {"data": [_to_object_v2(r) for r in rows], "nextPageToken": None}


def get_object(store: OntologyStore, object_type: str, primary_key: str,
               branch: str = "master") -> dict:
    """GET /api/v2/ontologies/{ontology}/objects/{objectType}/{primaryKey}"""
    obj = store.get(object_type, primary_key, branch)
    if obj is None:
        return _error("ObjectNotFound", {"objectType": object_type, "primaryKey": primary_key}, 404)
    return _to_object_v2(obj)


def search_objects(store: OntologyStore, object_type: str, where: dict | None = None,
                   page_size: int = 50, branch: str = "master") -> dict:
    """POST /api/v2/ontologies/{ontology}/objects/{objectType}/search"""
    rows = store.search(object_type, where=where, limit=page_size, branch=branch)
    return {"data": [_to_object_v2(r) for r in rows], "nextPageToken": None,
            "totalCount": str(len(rows))}


def list_linked_objects(store: OntologyStore, object_type: str, primary_key: str,
                        link: str, branch: str = "master") -> dict:
    """GET /api/v2/ontologies/{ontology}/objects/{objectType}/{primaryKey}/links/{linkType}"""
    try:
        rows = store.linked(object_type, primary_key, link, branch)
    except KeyError as exc:
        return _error("LinkTypeNotFound", {"linkType": link, "detail": str(exc)}, 404)
    return {"data": [_to_object_v2(r) for r in rows], "nextPageToken": None}


def list_action_types(action_types: dict) -> dict:
    """GET /api/v2/ontologies/{ontology}/actionTypes"""
    data = []
    for name, at in action_types.items():
        data.append({
            "apiName": name,
            "displayName": at.display_name,
            "description": at.description,
            "status": "ACTIVE",
            "rid": at.rid(),
            "operations": [{"type": "modifyObject", "objectTypeApiName": t} for t in at.modifies],
            "parameters": {
                p.name: {"dataType": {"type": p.data_type}, "required": p.required,
                         "description": p.description}
                for p in at.parameters
            },
            "x-writebackTarget": at.writeback_target,
        })
    return {"data": data, "nextPageToken": None}


def apply_action_request(action_api_name: str, parameters: dict) -> dict:
    """POST /api/v2/ontologies/{ontology}/actions/{actionType}/apply — request body."""
    return {"parameters": parameters,
            "options": {"mode": "VALIDATE_AND_EXECUTE", "returnEdits": "ALL"}}


def apply_action_response(result) -> dict:
    """The corresponding SyncApplyActionResponseV2."""
    return {
        "validation": {
            "result": "VALID" if not result.validation_errors else "INVALID",
            "submissionCriteria": [],
            "parameters": {k: {"result": "VALID"} for k in result.parameters}
            if not result.validation_errors else
            {e.split(":")[0]: {"result": "INVALID", "evaluatedConstraints": [e]}
             for e in result.validation_errors},
        },
        "edits": {
            "type": "edits",
            "edits": [{"type": e["type"], "objectType": e["objectType"],
                       "primaryKey": e["primaryKey"],
                       "rid": rid_for(e["objectType"], e["primaryKey"])}
                      for e in result.edits],
            "addedObjectCount": sum(1 for e in result.edits if e["type"] == "createObject"),
            "modifiedObjectsCount": sum(1 for e in result.edits if e["type"] == "modifyObject"),
        },
        "x-governance": {"decision": result.decision, "auditEventId": result.audit_event_id},
    }


def curl_for(path: str, method: str = "GET", body: dict | None = None) -> str:
    cmd = [f'curl -X {method} \\', '  -H "Authorization: Bearer $TOKEN" \\']
    if body is not None:
        cmd.append('  -H "Content-Type: application/json" \\')
    cmd.append(f'  "https://$HOSTNAME{API_BASE}/{ONTOLOGY_API_NAME}{path}"')
    if body is not None:
        import json
        cmd.append(f" \\\n  -d '{json.dumps(body)}'")
    return "\n".join(cmd)


def _error(code: str, params: dict, status: int) -> dict:
    return {"errorCode": "NOT_FOUND" if status == 404 else "INVALID_ARGUMENT",
            "errorName": code, "errorInstanceId": "00000000-0000-0000-0000-000000000000",
            "parameters": params}
