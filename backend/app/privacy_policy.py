"""Server-owned spreadsheet privacy policy and outbound dataset builder."""

from __future__ import annotations

import csv
import io
import json
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .security_scan import scan_for_unmasked_pii

PrivacyAction = Literal["share", "mask", "exclude"]
SENSITIVE_TYPES = {"PERSON", "EMAIL", "PHONE", "ADDRESS", "ID", "DOB", "IP"}


class ColumnPrivacyChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: PrivacyAction
    share_confirmed: bool = False


class PrivacyPolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    columns: dict[str, ColumnPrivacyChoice] = Field(min_length=1)


def recommendation(column_type: str | None) -> tuple[PrivacyAction, bool, str]:
    if column_type in SENSITIVE_TYPES:
        labels = {
            "PERSON": "May identify a person",
            "EMAIL": "Email address",
            "PHONE": "Phone number",
            "ADDRESS": "Location or street address",
            "ID": "Government or financial identifier",
            "DOB": "Date of birth",
            "IP": "IP address",
        }
        return "mask", True, labels.get(column_type, "Potential personal identifier")
    if column_type == "AMOUNT":
        return "share", False, "Numeric business measure; sharing preserves calculations"
    return "share", False, "No sensitive column pattern detected"


def validate_policy(
    column_names: list[str],
    column_types: dict[str, str | None],
    submitted: dict[str, dict] | None,
) -> dict[str, dict]:
    """Validate user choices against server-derived column names/classification."""
    if submitted is None:
        raise HTTPException(status_code=422, detail="A privacy choice is required for every column.")
    if set(submitted) != set(column_names):
        raise HTTPException(status_code=422, detail="Privacy choices must match the uploaded file columns exactly.")

    result: dict[str, dict] = {}
    for name in column_names:
        try:
            choice = ColumnPrivacyChoice.model_validate(submitted[name])
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Invalid privacy choice for column '{name}'.") from exc
        action, sensitive, reason = recommendation(column_types.get(name))
        if action == "mask" and choice.action == "share" and not choice.share_confirmed:
            raise HTTPException(
                status_code=422,
                detail=f"Explicit confirmation is required before sharing sensitive column '{name}'.",
            )
        if choice.action != "share" and choice.share_confirmed:
            raise HTTPException(status_code=422, detail=f"Share confirmation is not applicable to column '{name}'.")
        result[name] = {
            "action": choice.action,
            "share_confirmed": choice.share_confirmed,
            "sensitive": sensitive,
            "recommended_action": action,
            "reason": reason,
        }
    return result


def load_policy(
    policy_json: str | None,
    columns_json: str | None,
) -> tuple[dict[str, dict], bool]:
    """Read persisted policy; map legacy enabled flags to their old behavior."""
    try:
        policy = json.loads(policy_json or "")
        if isinstance(policy, dict) and isinstance(policy.get("columns"), dict):
            return policy["columns"], False
    except (ValueError, TypeError):
        pass
    try:
        columns = json.loads(columns_json or "[]")
    except (ValueError, TypeError):
        columns = []
    legacy = {}
    for item in columns:
        if isinstance(item, dict) and "name" in item:
            action = "mask" if item.get("enabled") else "share"
            legacy[str(item["name"])] = {
                "action": action,
                "share_confirmed": False,
                "sensitive": bool(item.get("type") in SENSITIVE_TYPES),
                "recommended_action": "mask" if item.get("type") in SENSITIVE_TYPES else "share",
                "reason": "Legacy file policy; preserved without changing existing behavior",
            }
    return legacy, True


def policy_json(policy: dict[str, dict]) -> str:
    return json.dumps({"version": 1, "columns": policy}, ensure_ascii=True, separators=(",", ":"))


def validate_outbound_text(
    text: str,
    *,
    confirmed_share_segments: list[str] | None = None,
) -> None:
    """Fail closed except for source text the user explicitly confirmed sharing."""
    checked_text = text
    for segment in confirmed_share_segments or []:
        if segment:
            checked_text = checked_text.replace(segment, " " * len(segment), 1)
    if scan_for_unmasked_pii(checked_text):
        raise HTTPException(
            status_code=422,
            detail="Outbound privacy validation found unmasked sensitive data.",
        )


def build_outbound_csv(masked_csv: str, policy: dict[str, dict], *, legacy: bool = False) -> str:
    """Project only Share/Mask columns and fail closed on unmasked PII.

    ``masked_csv`` contains original values for Share/Exclude and local tokens
    for Mask. Excluded values are never copied into this output.
    """
    try:
        reader = csv.DictReader(io.StringIO(masked_csv, newline=""))
        headers = reader.fieldnames or []
        if len(headers) != len(set(headers)):
            raise ValueError("duplicate headers")
        choices = policy
        if set(headers) != set(choices):
            raise ValueError("policy/header mismatch")
        if scan_for_unmasked_pii(",".join(headers)):
            raise HTTPException(status_code=422, detail="A column header contains sensitive data and cannot be sent to a provider.")
        outbound_headers = [h for h in headers if choices[h].get("action") != "exclude"]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=outbound_headers, lineterminator="\n")
        writer.writeheader()
        for row in reader:
            outgoing = {h: row.get(h) or "" for h in outbound_headers}
            for header in outbound_headers:
                choice = choices[header]
                text = outgoing[header]
                action = choice.get("action")
                if action == "mask":
                    findings = scan_for_unmasked_pii(text)
                    if findings:
                        raise HTTPException(
                            status_code=422,
                            detail="Outbound privacy validation found unmasked sensitive data in a masked column.",
                        )
                elif action == "share" and choice.get("sensitive") and not (choice.get("share_confirmed") or legacy):
                    raise HTTPException(status_code=422, detail="Sensitive Share choice is missing confirmation.")
            writer.writerow(outgoing)
        return output.getvalue()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Stored file privacy policy could not be validated.") from exc
